"""Bounded, read-only application, Xcode, and system metadata adapters.

These reports are intentionally not cleanup plans. A bundle, archive, or cache
location is not evidence that its contents may be removed.
"""

from __future__ import annotations

import errno
import hashlib
import os
import platform
import plistlib
import re
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Callable
from xml.parsers.expat import ExpatError

from .common import new_id, utc_now


_MAX_PLIST = 4 * 1024 * 1024
_MAX_ENTRIES = 100_000
_MAX_APPLICATION_ENTRIES = 20_000
_MAX_DEPTH = 64
_MAX_SECONDS = 10.0
_MAX_PROBE_OUTPUT = 1024 * 1024
_DATALESS = getattr(stat, "SF_DATALESS", 0x40000000)
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
_XCODE_CATEGORIES = (
    ("derived_data", "Xcode/DerivedData", "Build products and indexes; active-build state is unknown."),
    ("module_cache", "Xcode/DerivedData/ModuleCache.noindex", "Compiler module cache; overlaps DerivedData."),
    ("archives", "Xcode/Archives", "Archives may contain irreplaceable builds and dSYMs."),
    ("ios_device_support", "Xcode/iOS DeviceSupport", "Device support may be needed for connected devices."),
    ("macos_device_support", "Xcode/macOS DeviceSupport", "Device support eligibility is unverified."),
    ("device_logs", "Xcode/iOS Device Logs", "Diagnostics may be needed for investigations."),
    ("documentation_cache", "Xcode/DocumentationCache", "Documentation layout and regeneration are unverified."),
    ("simulator_caches", "CoreSimulator/Caches", "Simulator-owned cache; active state is unknown."),
    ("simulator_devices", "CoreSimulator/Devices", "Managed simulator devices may contain valuable app data."),
    ("simulator_runtimes", "CoreSimulator/Profiles/Runtimes", "Managed runtimes require version-specific supported tools."),
    ("runtime_images", "CoreSimulator/Images", "Runtime images and downloads are managed resources."),
)


def _report(adapter: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "id": new_id(adapter),
        "created_at": utc_now(),
        "adapter": adapter,
        "read_only": True,
        "status": "complete",
        "items": [],
        "errors": [],
        "coverage": [],
        "summary": {},
        "limitations": [],
    }


def _unsupported(adapter: str) -> dict[str, Any] | None:
    if platform.system() == "Darwin":
        return None
    report = _report(adapter)
    report["status"] = "unavailable"
    report["errors"].append({"code": "unsupported_platform", "reason": "This adapter supports macOS only."})
    return report


def _error(path: str, exc: OSError, *, operation: str = "inspect") -> dict[str, Any]:
    if exc.errno == errno.EACCES:
        code, reason = "permission_denied", "Filesystem permission denied; this location was not fully examined."
    elif exc.errno == errno.EPERM:
        code, reason = "access_restricted", "Privacy, sandbox, or system restrictions may block this location; the cause is not established."
    elif exc.errno == errno.ENOENT:
        code, reason = "not_found", "The selected location no longer exists."
    elif exc.errno in (errno.ELOOP, errno.ENOTDIR):
        code, reason = "unsafe_path", "A path component is a symbolic link or not a directory."
    else:
        code, reason = "read_failed", "Metadata could not be read."
    return {"path": path, "operation": operation, "code": code, "reason": reason, "errno": exc.errno}


def _path(value: str | os.PathLike[str]) -> str:
    path = os.path.abspath(os.path.expanduser(os.fspath(value)))
    if "\x00" in path:
        raise ValueError("A path cannot contain a null byte.")
    return path


def _open_dir(path: str) -> int:
    """Walk from / without following ancestor symlinks; caller owns returned fd."""
    fd = os.open(os.path.sep, _DIR_FLAGS)
    try:
        for part in Path(path).parts[1:]:
            before = os.stat(part, dir_fd=fd, follow_symlinks=False)
            if getattr(before, "st_flags", 0) & _DATALESS:
                raise OSError(errno.EPERM, "Cloud-only directory was not hydrated", path)
            next_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            if _identity(before) != _identity(os.fstat(next_fd)):
                os.close(next_fd)
                raise OSError(errno.ESTALE, "Directory changed while opening", path)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _identity(st: os.stat_result) -> tuple[int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns)


def _open_relative_dir(root_fd: int, relative: str, *, expected_device: int) -> int:
    """Open a category from a pinned selected root, never from its pathname."""
    parts = Path(relative).parts
    if not parts or os.path.isabs(relative) or any(part in (".", "..") for part in parts):
        raise OSError(errno.EINVAL, "A category must be a relative descendant", relative)
    fd = os.dup(root_fd)
    try:
        for part in parts:
            before = os.stat(part, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                raise OSError(errno.ENOTDIR, "Category component is not a directory", relative)
            if before.st_dev != expected_device:
                raise OSError(errno.EXDEV, "Category is on an unselected volume", relative)
            if getattr(before, "st_flags", 0) & _DATALESS:
                raise OSError(errno.EPERM, "Cloud-only category was not hydrated", relative)
            next_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            if _identity(before) != _identity(os.fstat(next_fd)):
                os.close(next_fd)
                raise OSError(errno.ESTALE, "Category changed while opening", relative)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _opaque_id(prefix: str, path: str, st: os.stat_result) -> str:
    data = os.fsencode(path) + f"\0{st.st_dev}:{st.st_ino}".encode("ascii")
    return prefix + "-" + hashlib.sha256(data).hexdigest()[:24]


def _read_plist(bundle_fd: int, bundle_path: str) -> dict[str, Any]:
    contents_st = os.stat("Contents", dir_fd=bundle_fd, follow_symlinks=False)
    if getattr(contents_st, "st_flags", 0) & _DATALESS:
        raise OSError(errno.EPERM, "Cloud-only bundle was not hydrated", bundle_path)
    contents_fd = os.open("Contents", _DIR_FLAGS, dir_fd=bundle_fd)
    try:
        if _identity(contents_st) != _identity(os.fstat(contents_fd)):
            raise OSError(errno.ESTALE, "Bundle changed while reading", bundle_path)
        plist_st = os.stat("Info.plist", dir_fd=contents_fd, follow_symlinks=False)
        if not stat.S_ISREG(plist_st.st_mode) or getattr(plist_st, "st_flags", 0) & _DATALESS:
            raise OSError(errno.EPERM, "Unsafe or cloud-only property list", bundle_path)
        fd = os.open("Info.plist", _FILE_FLAGS, dir_fd=contents_fd)
    finally:
        os.close(contents_fd)
    try:
        before = os.fstat(fd)
        if _identity(plist_st) != _identity(before):
            raise OSError(errno.ESTALE, "Property list changed while opening", bundle_path)
        if not stat.S_ISREG(before.st_mode) or getattr(before, "st_flags", 0) & _DATALESS:
            raise OSError(errno.EPERM, "Unsafe or cloud-only property list", bundle_path)
        if before.st_size > _MAX_PLIST:
            raise OSError(errno.EFBIG, "Property list exceeds metadata limit", bundle_path)
        chunks, count = [], 0
        while True:
            block = os.read(fd, min(65536, _MAX_PLIST + 1 - count))
            if not block:
                break
            chunks.append(block)
            count += len(block)
            if count > _MAX_PLIST:
                raise OSError(errno.EFBIG, "Property list exceeds metadata limit", bundle_path)
        if _identity(before) != _identity(os.fstat(fd)):
            raise OSError(errno.ESTALE, "Property list changed while reading", bundle_path)
        value = plistlib.loads(b"".join(chunks))
        if not isinstance(value, dict):
            raise ValueError("The property list is not a dictionary.")
        return value
    finally:
        os.close(fd)


def _string(metadata: dict[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    return value[:2048] if isinstance(value, str) else None


def _app_item(path: str, fd: int, errors: list[dict[str, Any]]) -> dict[str, Any]:
    st = os.fstat(fd)
    item = {
        "id": _opaque_id("app", path, st), "path": path,
        "kind": "application", "name": Path(path).stem,
        "bundle_id": None, "version": None, "build": None,
        "minimum_macos": None, "executable": None,
        "metadata_status": "available", "eligible": False,
        "report_only": True, "action": None,
        "unused_status": "unknown", "signature_status": "not_checked",
        "compatibility_status": "not_verified", "allocated_size": None,
    }
    try:
        metadata = _read_plist(fd, path)
        item.update({
            "name": _string(metadata, "CFBundleDisplayName") or _string(metadata, "CFBundleName") or item["name"],
            "bundle_id": _string(metadata, "CFBundleIdentifier"),
            "version": _string(metadata, "CFBundleShortVersionString"),
            "build": _string(metadata, "CFBundleVersion"),
            "minimum_macos": _string(metadata, "LSMinimumSystemVersion"),
            "executable": _string(metadata, "CFBundleExecutable"),
        })
    except OSError as exc:
        item["metadata_status"] = "unavailable"
        errors.append(_error(path + "/Contents/Info.plist", exc))
    except (ValueError, plistlib.InvalidFileException, ExpatError, OverflowError, RecursionError):
        item["metadata_status"] = "invalid"
        errors.append({"path": path + "/Contents/Info.plist", "code": "invalid_plist", "reason": "The app metadata could not be parsed."})
    return item


def scan_applications(roots: list[str] | None = None, *, cancel: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Find application bundles without launching apps or traversing their code."""
    unsupported = _unsupported("applications")
    if unsupported:
        return unsupported
    report = _report("applications")
    selected = roots if roots is not None else ["/Applications", str(Path.home() / "Applications")]
    report["roots"] = list(dict.fromkeys(_path(root) for root in selected))
    report["limitations"] = [
        "Application identity comes from untrusted Info.plist metadata; signatures, architecture compatibility, and last use are not verified.",
        "Bundle sizes and owned components are not measured. No uninstall or leftover-removal actions are available from this report.",
        "Folder search is limited to four levels below each selected root; nested application bundles are not traversed.",
    ]
    started, examined, visited = time.monotonic(), 0, set()

    def cancelled() -> bool:
        if report["status"] == "cancelled" or (cancel is not None and cancel()):
            report["status"] = "cancelled"
            return True
        return False

    for root in report["roots"]:
        if cancelled():
            break
        coverage = {"root": root, "status": "complete", "examined_entries": 0}
        report["coverage"].append(coverage)
        try:
            root_fd = _open_dir(root)
        except OSError as exc:
            coverage["status"] = "unavailable"
            report["errors"].append(_error(root, exc))
            continue
        root_dev = os.fstat(root_fd).st_dev
        errors_before = len(report["errors"])

        def visit(path: str, fd: int, depth: int) -> bool:
            nonlocal examined
            if cancelled():
                return False
            st = os.fstat(fd)
            identity = (st.st_dev, st.st_ino)
            if identity in visited:
                return True
            visited.add(identity)
            if path.lower().endswith(".app"):
                report["items"].append(_app_item(path, fd, report["errors"]))
                return True
            try:
                with os.scandir(fd) as entries:
                    for entry in entries:
                        if cancelled():
                            return False
                        examined += 1
                        coverage["examined_entries"] += 1
                        if examined > _MAX_APPLICATION_ENTRIES or time.monotonic() - started > _MAX_SECONDS:
                            report["errors"].append({"path": root, "code": "scan_limit", "reason": "Application discovery reached its entry or time limit."})
                            return False
                        child_path = os.path.join(path, entry.name)
                        try:
                            child_st = entry.stat(follow_symlinks=False)
                            if not stat.S_ISDIR(child_st.st_mode):
                                continue
                            if child_st.st_dev != root_dev or getattr(child_st, "st_flags", 0) & _DATALESS:
                                report["errors"].append({"path": child_path, "code": "excluded_location", "reason": "An unselected mount or cloud-only directory was not examined."})
                                continue
                            if depth >= 4:
                                report["errors"].append({"path": child_path, "code": "depth_limit", "reason": "Application discovery depth limit reached."})
                                continue
                            child_fd = os.open(entry.name, _DIR_FLAGS, dir_fd=fd)
                            try:
                                if _identity(child_st) != _identity(os.fstat(child_fd)):
                                    raise OSError(errno.ESTALE, "Directory changed", child_path)
                                if not visit(child_path, child_fd, depth + 1):
                                    return False
                            finally:
                                os.close(child_fd)
                        except OSError as exc:
                            report["errors"].append(_error(child_path, exc))
            except OSError as exc:
                report["errors"].append(_error(path, exc))
            return True

        try:
            visit(root, root_fd, 0)
        finally:
            os.close(root_fd)
        if len(report["errors"]) != errors_before:
            coverage["status"] = "partial"
        if cancelled():
            coverage["status"] = "cancelled"
            break
    report["items"].sort(key=lambda item: os.fsencode(item["path"]))
    report["summary"] = {"applications": len(report["items"]), "examined_entries": examined}
    _finish(report)
    return report


def _finish(report: dict[str, Any]) -> None:
    if report["status"] == "cancelled":
        return
    if report["coverage"] and all(c["status"] == "unavailable" for c in report["coverage"]):
        report["status"] = "unavailable"
    elif report["errors"] or any(c["status"] == "partial" for c in report["coverage"]):
        report["status"] = "partial"
    else:
        report["status"] = "complete"


def _run_probe(argv: list[str]) -> dict[str, Any]:
    """Only callers with a fixed system executable/argument list use this helper."""
    try:
        result = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=3,
            check=False, env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"},
        )
    except FileNotFoundError:
        return {"status": "unavailable", "reason": "Required macOS command is not installed."}
    except subprocess.TimeoutExpired:
        return {"status": "unavailable", "reason": "The read-only system probe timed out."}
    except OSError as exc:
        return {"status": "unavailable", "reason": "The system probe could not run.", "errno": exc.errno}
    if result.returncode != 0:
        return {"status": "unavailable", "reason": "The system probe did not complete successfully.", "exit_code": result.returncode}
    if len(result.stdout) > _MAX_PROBE_OUTPUT or len(result.stderr) > _MAX_PROBE_OUTPUT:
        return {"status": "unavailable", "reason": "The system probe exceeded the output limit."}
    return {"status": "available", "text": result.stdout.decode("utf-8", "replace").strip()}


def system_overview(*, cancel: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Report current system measurements without cleaning RAM or changing macOS."""
    unsupported = _unsupported("overview")
    if unsupported:
        return unsupported
    report = _report("overview")

    def cancelled() -> bool:
        if report["status"] == "cancelled" or (cancel is not None and cancel()):
            report["status"] = "cancelled"
            report["summary"] = {"cleanup_performed": False, "measured_at": utc_now()}
            return True
        return False

    if cancelled():
        return report
    report["measurements"] = {
        "os": {"system": "macOS", "version": platform.mac_ver()[0] or None, "process_architecture": platform.machine()},
    }
    report["limitations"] = [
        "Storage availability is a point-in-time filesystem measurement, not an amount CleanMac can reclaim; APFS shared containers and snapshots complicate attribution.",
        "VM page statistics are not a memory-pressure diagnosis or a recommendation to purge RAM. The process architecture may be translated.",
        "No speed test, network request, process termination, maintenance, or permission change is performed.",
    ]
    try:
        fs = os.statvfs(str(Path.home()))
        report["measurements"]["storage"] = {
            "status": "available", "scope": "filesystem containing the current home directory",
            "total_bytes": fs.f_blocks * fs.f_frsize,
            "free_bytes": fs.f_bfree * fs.f_frsize,
            "available_bytes": fs.f_bavail * fs.f_frsize,
            "reclaimable_bytes": None,
        }
    except OSError as exc:
        report["measurements"]["storage"] = {"status": "unavailable", "reason": "Storage metadata could not be read."}
        report["errors"].append(_error(str(Path.home()), exc, operation="statvfs"))
    if cancelled():
        return report
    memory = _run_probe(["/usr/sbin/sysctl", "-n", "hw.memsize"])
    if memory["status"] == "available" and re.fullmatch(r"[0-9]{1,20}", memory["text"]):
        report["measurements"]["physical_memory"] = {"status": "available", "bytes": int(memory["text"])}
    else:
        report["measurements"]["physical_memory"] = {"status": "unavailable", "reason": memory.get("reason", "Unexpected physical-memory response.")}
    if cancelled():
        return report
    vm = _run_probe(["/usr/bin/vm_stat"])
    vm_result: dict[str, Any] = {"status": "unavailable", "reason": vm.get("reason", "Unexpected VM-statistics response.")}
    if vm["status"] == "available":
        page = re.search(r"page size of ([0-9]+) bytes", vm["text"])
        if page and 0 < int(page.group(1)) <= 1024 * 1024:
            names = {"Pages free": "free", "Pages active": "active", "Pages inactive": "inactive", "Pages wired down": "wired", "Pages occupied by compressor": "compressor"}
            counts = {}
            for line in vm["text"].splitlines():
                match = re.fullmatch(r"([^:]+):\s*([0-9]{1,20})\.?\s*", line)
                if match and match.group(1) in names:
                    counts[names[match.group(1)]] = int(match.group(2))
            if counts:
                vm_result = {"status": "available", "page_size": int(page.group(1)), "pages": counts, "pressure": "not_measured"}
    report["measurements"]["vm_statistics"] = vm_result
    if cancelled():
        return report
    if any(isinstance(value, dict) and value.get("status") == "unavailable" for value in report["measurements"].values()):
        report["status"] = "partial"
    report["summary"] = {"cleanup_performed": False, "measured_at": utc_now()}
    return report


def _tree_size(path: str, budget: dict[str, Any], *, expected_device: int, directory_fd: int | None = None, cancel: Callable[[], bool] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "complete", "logical_size": 0, "allocated_size": 0, "files": 0, "directories": 0, "excluded_entries": 0, "errors": []}
    fd = os.dup(directory_fd) if directory_fd is not None else _open_dir(path)
    root_dev = os.fstat(fd).st_dev
    if root_dev != expected_device:
        os.close(fd)
        raise OSError(errno.EXDEV, "Category is on an unselected volume", path)
    seen_files: set[tuple[int, int]] = set()
    def visit(current: str, current_fd: int, depth: int) -> bool:
        if cancel is not None and cancel():
            result["status"] = "cancelled"
            return False
        result["directories"] += 1
        try:
            with os.scandir(current_fd) as entries:
                for entry in entries:
                    if cancel is not None and cancel():
                        result["status"] = "cancelled"
                        return False
                    budget["entries"] += 1
                    if budget["entries"] > _MAX_ENTRIES or time.monotonic() - budget["started"] > _MAX_SECONDS:
                        result["errors"].append({"path": current, "code": "scan_limit", "reason": "Metadata inventory reached its entry or time limit."})
                        result["status"] = "partial"
                        return False
                    child_path = os.path.join(current, entry.name)
                    try:
                        st = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(st.st_mode) or st.st_dev != root_dev or getattr(st, "st_flags", 0) & _DATALESS:
                            result["excluded_entries"] += 1
                            result["status"] = "partial"
                            continue
                        if stat.S_ISDIR(st.st_mode):
                            if depth >= _MAX_DEPTH:
                                result["excluded_entries"] += 1
                                result["status"] = "partial"
                                continue
                            child_fd = os.open(entry.name, _DIR_FLAGS, dir_fd=current_fd)
                            try:
                                if _identity(st) != _identity(os.fstat(child_fd)):
                                    raise OSError(errno.ESTALE, "Directory changed", child_path)
                                if not visit(child_path, child_fd, depth + 1):
                                    return False
                            finally:
                                os.close(child_fd)
                        elif stat.S_ISREG(st.st_mode):
                            identity = (st.st_dev, st.st_ino)
                            if identity in seen_files:
                                continue
                            seen_files.add(identity)
                            result["files"] += 1
                            result["logical_size"] += st.st_size
                            result["allocated_size"] += getattr(st, "st_blocks", 0) * 512
                        else:
                            result["excluded_entries"] += 1
                            result["status"] = "partial"
                    except OSError as exc:
                        result["errors"].append(_error(child_path, exc))
                        result["status"] = "partial"
        except OSError as exc:
            result["errors"].append(_error(current, exc))
            result["status"] = "partial"
        return True

    try:
        visit(path, fd, 0)
    finally:
        os.close(fd)
    return result


def scan_xcode(roots: list[str] | None = None, *, cancel: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Measure known categories under selected Developer roots; never run simctl."""
    unsupported = _unsupported("xcode")
    if unsupported:
        return unsupported
    report = _report("xcode")
    selected = roots if roots is not None else [str(Path.home() / "Library" / "Developer")]
    report["roots"] = list(dict.fromkeys(_path(root) for root in selected))
    report["report_only"] = True
    report["limitations"] = [
        "Roots are Developer directories (normally ~/Library/Developer), not project folders. Only the listed relative categories are examined.",
        "No simulator service is started: simctl, xcrun, and xcodebuild are not invoked. Device/runtime availability, active builds, and removal eligibility remain unknown.",
        "Categories can overlap (ModuleCache is inside DerivedData); category sizes are not added into a reclaimable total. APFS shared allocation remains uncertain.",
        "Archives, dSYMs, device data, and runtime images remain protected and report-only. Missing categories mean absent at this path, not proof that no copies exist elsewhere.",
    ]
    budget = {"entries": 0, "started": time.monotonic()}

    def cancelled() -> bool:
        if report["status"] == "cancelled" or (cancel is not None and cancel()):
            report["status"] = "cancelled"
            return True
        return False

    for root in report["roots"]:
        if cancelled():
            break
        try:
            root_fd = _open_dir(root)
            root_st = os.fstat(root_fd)
            root_dev = root_st.st_dev
        except OSError as exc:
            report["coverage"].append({"root": root, "status": "unavailable"})
            report["errors"].append(_error(root, exc))
            continue
        root_identity = {"device": root_st.st_dev, "inode": root_st.st_ino}
        try:
            for category, relative, reason in _XCODE_CATEGORIES:
                if cancelled():
                    break
                path = os.path.join(root, relative)
                coverage: dict[str, Any] = {"root": root, "root_identity": root_identity, "path": path, "category": category, "status": "complete"}
                report["coverage"].append(coverage)
                if budget["entries"] > _MAX_ENTRIES or time.monotonic() - budget["started"] > _MAX_SECONDS:
                    coverage["status"] = "partial"
                    coverage["reason"] = "The shared scan budget was exhausted; this category was not examined."
                    continue
                try:
                    category_fd = _open_relative_dir(root_fd, relative, expected_device=root_dev)
                    try:
                        st = os.fstat(category_fd)
                        sizes = _tree_size(path, budget, expected_device=root_dev, directory_fd=category_fd, cancel=cancelled)
                    finally:
                        os.close(category_fd)
                except FileNotFoundError:
                    coverage["status"] = "absent"
                    continue
                except OSError as exc:
                    coverage["status"] = "unavailable"
                    report["errors"].append(_error(path, exc))
                    continue
                report["errors"].extend(sizes.pop("errors"))
                coverage["status"] = sizes["status"]
                item = {
                    "id": _opaque_id("xcode", path, st), "path": path,
                    "root_identity": root_identity,
                    "device": st.st_dev, "inode": st.st_ino,
                    "kind": "xcode_category", "category": category,
                    "eligible": False, "protected": True, "report_only": True,
                    "reason": reason, "action": None, "managed_state": "unknown",
                    "size_provenance": "metadata st_size and st_blocks; unique inodes within this category",
                    "reclaimable_bytes": None, "overlaps": ["derived_data"] if category == "module_cache" else [],
                    **sizes,
                }
                report["items"].append(item)
                if cancelled():
                    break
        finally:
            os.close(root_fd)
        if cancelled():
            break
        # The pinned descriptor preserves the selected directory even if its
        # name is replaced. Mark the original display paths stale in that case.
        try:
            current_root_fd = _open_dir(root)
            try:
                current_root_st = os.fstat(current_root_fd)
                same_root = (current_root_st.st_dev, current_root_st.st_ino) == (root_st.st_dev, root_st.st_ino)
            finally:
                os.close(current_root_fd)
        except OSError:
            same_root = False
        if not same_root:
            report["errors"].append({"path": root, "code": "scope_changed", "reason": "The selected root pathname changed during inventory; sizes refer to the originally opened directory."})
            for entry in report["coverage"]:
                if entry["root"] == root:
                    entry["path_changed"] = True
                    if entry["status"] == "complete":
                        entry["status"] = "partial"
            for item in report["items"]:
                if item["root_identity"] == root_identity:
                    item["path_changed"] = True
    report["summary"] = {"categories_found": len(report["items"]), "examined_entries": budget["entries"], "reclaimable_bytes": None, "cleanup_performed": False}
    _finish(report)
    return report

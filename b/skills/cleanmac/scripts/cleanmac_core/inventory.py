"""Bounded, metadata-only filesystem inventory with descriptor-based identity checks.

Directory sizes are not recursively added to file totals. Allocated bytes describe
observed file allocation, not uniquely reclaimable capacity on APFS or snapshots.
"""
from __future__ import annotations

import errno
import os
import stat
import sys
import time
from typing import Callable

from .common import CleanMacError, digest, new_id, utc_now


DATALESS_FLAG = getattr(stat, "SF_DATALESS", 0x40000000)
MAX_DEPTH = 128
MAX_REPORTED_ERRORS = 200
_PACKAGE_SUFFIXES = (
    ".app", ".bundle", ".framework", ".plugin", ".appex", ".photoslibrary",
    ".photolibrary", ".musiclibrary", ".band", ".pages", ".numbers", ".key",
    ".rtfd", ".sparsebundle", ".backupbundle",
)
_PROTECTED_COMPONENTS = {
    ".git", ".hg", ".svn", ".ssh", ".gnupg", ".aws", ".azure", ".kube",
    ".codex", ".claude", ".kimi", ".gemini", ".cursor", ".openclaw",
    ".trash", ".trashes", ".fseventsd", ".spotlight-v100", ".timemachine",
    "backups.backupdb",
}
_SENSITIVE_LIBRARY_COMPONENTS = {
    "mail", "messages", "keychains", "calendars", "reminders", "accounts",
    "safari", "autosave information",
}
_AGENT_SUPPORT_COMPONENTS = {
    "claude", "claude code", "codex", "cursor", "kimi", "opencode",
}
_APP_DATA_COMPONENTS = {
    "library", ".cache", ".config", ".local", ".npm", ".gradle", ".m2",
    ".yarn", ".pnpm-store", ".nuget", ".cargo", ".rustup", ".docker",
}


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _canonical_root(value) -> str:
    try:
        path = os.fsdecode(os.fspath(value))
    except TypeError as exc:
        raise CleanMacError("Scan roots must be filesystem paths.") from exc
    if not path or "\x00" in path:
        raise CleanMacError("Scan roots must be nonempty paths without NUL characters.")
    path = os.path.abspath(os.path.expanduser(path))
    # macOS's standard aliases are part of the OS layout. Do not resolve any
    # other symbolic-link ancestry: selected links must never expand scope.
    if sys.platform == "darwin":
        for source, target in (("/tmp", "/private/tmp"), ("/var", "/private/var"), ("/etc", "/private/etc")):
            if path == source or path.startswith(source + os.sep):
                try:
                    value = os.lstat(source)
                    if stat.S_ISLNK(value.st_mode) and value.st_uid == 0 and os.path.realpath(source) == target:
                        path = target + path[len(source):]
                except OSError:
                    pass
                break
    return path


def _is_repository_fd(fd: int) -> bool:
    for name in (".git", ".hg", ".svn"):
        try:
            os.stat(name, dir_fd=fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            continue
    return False


def _ancestor_record(path: str, fd: int) -> dict:
    info = os.fstat(fd)
    return {
        "path": path, "device": info.st_dev, "inode": info.st_ino,
        "is_repository": _is_repository_fd(fd),
    }


def _open_parent(path: str, expected: list[dict] | None = None) -> tuple[int, list[dict]]:
    """Return an owned parent descriptor, opening every component without links."""
    if not isinstance(path, str) or not os.path.isabs(path) or os.path.normpath(path) != path or "\x00" in path:
        raise CleanMacError("The item path is not a normalized absolute path.", "unsafe_path")
    parent = os.path.dirname(path)
    parts = [p for p in parent.split(os.sep) if p]
    fd = os.open(os.sep, _directory_flags())
    records = []
    current_path = os.sep
    try:
        records.append(_ancestor_record(current_path, fd))
        for part in parts:
            next_fd = os.open(part, _directory_flags(), dir_fd=fd)
            os.close(fd)
            fd = next_fd
            current_path = os.path.join(current_path, part)
            records.append(_ancestor_record(current_path, fd))
        if expected is not None:
            if not isinstance(expected, list) or len(expected) != len(records):
                raise CleanMacError("The item's ancestor chain changed or is incomplete.", "stale_item")
            for actual, previous in zip(records, expected):
                if not isinstance(previous, dict) or any(actual[k] != previous.get(k) for k in ("path", "device", "inode")):
                    raise CleanMacError("An item's parent directory changed.", "stale_item")
        return fd, records
    except BaseException:
        os.close(fd)
        raise


def _stat_values(info: os.stat_result) -> dict:
    return {
        "device": info.st_dev, "inode": info.st_ino,
        "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns,
        "mode": info.st_mode, "nlink": info.st_nlink,
        "flags": getattr(info, "st_flags", 0),
        "logical_size": info.st_size,
    }


def _matches_stat(item: dict, info: os.stat_result) -> bool:
    return all(item.get(key) == value for key, value in _stat_values(info).items())


def _placeholder(path: str, info: os.stat_result) -> bool:
    return bool(getattr(info, "st_flags", 0) & DATALESS_FLAG) or os.path.basename(path).lower().endswith(".icloud")


def _policy_reason(path: str, info: os.stat_result, ancestors: list[dict], *, repository: bool = False) -> str | None:
    components = [part.casefold() for part in path.split(os.sep) if part]
    name = components[-1] if components else ""
    if _placeholder(path, info):
        return "cloud-placeholder"
    if any(component in _PROTECTED_COMPONENTS for component in components):
        return "protected-app-credential-backup-or-agent-data"
    if repository or any(value.get("is_repository") for value in ancestors):
        return "source-repository"
    if any(component.endswith(_PACKAGE_SUFFIXES) for component in components):
        return "package-internals"
    if "cloudstorage" in components or "mobile documents" in components:
        return "cloud-provider-managed-data"
    if "library" in components:
        index = components.index("library")
        if index + 1 < len(components) and components[index + 1] in _SENSITIVE_LIBRARY_COMPONENTS:
            return "protected-library-data"
    for index, component in enumerate(components[:-1]):
        if component == "application support" and components[index + 1] in _AGENT_SUPPORT_COMPONENTS:
            return "agent-app-data"
        if component == "mobilesync" and components[index + 1] == "backup":
            return "device-backup"
        if component == "b-skills" and components[index + 1] == "cleanmac":
            return "cleanmac-state"
    if name == ".env" or name.startswith(".env.") or name.endswith((".pem", ".p12", ".pfx", ".keychain", ".keychain-db")):
        return "credential-or-configuration-file"
    if info.st_uid != os.getuid():
        return "not-owned-by-current-user"
    return None


def _is_app_data(path: str) -> bool:
    return any(part.casefold() in _APP_DATA_COMPONENTS for part in path.split(os.sep))


def assert_item_identity(item: dict) -> None:
    """Revalidate all parent identities and exact item metadata without contents."""
    if not isinstance(item, dict) or not isinstance(item.get("ancestors"), list):
        raise CleanMacError("Invalid item record.", "unsafe_path")
    path = item.get("path")
    fd = None
    try:
        fd, ancestors = _open_parent(path, item.get("ancestors"))
        info = os.stat(os.path.basename(path), dir_fd=fd, follow_symlinks=False)
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)) or not _matches_stat(item, info):
            raise CleanMacError("The item changed since scanning.", "stale_item", {"path": path})
        if _policy_reason(path, info, ancestors):
            raise CleanMacError("The item is protected from generic cleanup.", "protected_item", {"path": path})
    except OSError as exc:
        raise CleanMacError("The item or its parent is no longer safely accessible.", "stale_item", {"path": path, "errno": exc.errno}) from exc
    finally:
        if fd is not None:
            os.close(fd)


def validate_item_identity(item: dict) -> bool:
    try:
        assert_item_identity(item)
        return True
    except (CleanMacError, OSError, TypeError, ValueError):
        return False


def safe_open_item(item: dict) -> int:
    """Open a validated local regular file read-only; caller owns the descriptor.

    Identity is checked before and after open. Readers must additionally fstat
    after reading; this cannot prevent an already-open writer changing content.
    """
    if not isinstance(item, dict) or not isinstance(item.get("ancestors"), list) or item.get("protected") or item.get("is_placeholder") or item.get("kind") != "file":
        raise CleanMacError("Only unprotected local regular files may be opened.", "protected_item")
    parent_fd = file_fd = None
    path = item.get("path")
    try:
        parent_fd, ancestors = _open_parent(path, item.get("ancestors"))
        before = os.stat(os.path.basename(path), dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or not _matches_stat(item, before):
            raise CleanMacError("The file changed before it could be opened.", "stale_item", {"path": path})
        if _policy_reason(path, before, ancestors):
            raise CleanMacError("The file is protected or cloud-managed.", "protected_item", {"path": path})
        file_fd = os.open(os.path.basename(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0), dir_fd=parent_fd)
        after = os.fstat(file_fd)
        if not stat.S_ISREG(after.st_mode) or not _matches_stat(item, after) or _placeholder(path, after):
            raise CleanMacError("The file changed during open.", "stale_item", {"path": path})
        result, file_fd = file_fd, None
        return result
    except OSError as exc:
        raise CleanMacError("The file cannot be safely opened.", "stale_item", {"path": path, "errno": exc.errno}) from exc
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
        if file_fd is not None:
            os.close(file_fd)


def scan_roots(roots, *, cancel: Callable | None = None, progress: Callable | None = None,
               max_items: int = 100000, exclude_paths=None) -> dict:
    """Inspect explicit roots without opening file contents or traversing links.

    max_items bounds examined entries, including entries excluded by policy.
    Deliberate exclusions are recorded separately from unreadable coverage.
    """
    if not isinstance(max_items, int) or isinstance(max_items, bool) or not 1 <= max_items <= 1000000:
        raise CleanMacError("max_items must be between 1 and 1000000.")
    if isinstance(roots, (str, bytes, os.PathLike)):
        roots = [roots]
    try:
        roots = list(roots)
    except TypeError as exc:
        raise CleanMacError("Provide at least one scan root.") from exc
    if not roots or len(roots) > 64:
        raise CleanMacError("Provide between 1 and 64 explicit scan roots.")
    normalized = list(dict.fromkeys(_canonical_root(root) for root in roots))
    exclusions = [_canonical_root(path) for path in (exclude_paths or [])]
    result = {
        "id": new_id("scan"), "schema_version": 1, "created_at": utc_now(),
        "roots": normalized, "items": [], "errors": [], "exclusions": [],
        "status": "complete", "counts": {
            "items": 0, "entries_seen": 0, "files": 0, "directories": 0,
            "unique_files": 0, "duplicate_file_aliases": 0,
            "protected": 0, "placeholders": 0, "excluded": 0,
            "symlinks": 0, "special_files": 0, "unreadable": 0,
            "logical_bytes": 0, "allocated_bytes": 0,
            "selected_roots": len(normalized), "scanned_roots": 0,
        },
        "accounting": {"allocated_bytes": "observed-allocation-not-uniquely-reclaimable", "directory_sizes_in_totals": False},
    }
    counts = result["counts"]
    seen_paths = set()
    visited_directories = set()
    sized_files = set()
    started = time.monotonic()
    stopped = False
    frames = []

    def add_error(path, code, message, exception=None):
        counts["unreadable"] += 1
        result["status"] = "partial"
        if len(result["errors"]) < MAX_REPORTED_ERRORS:
            error = {"path": path, "code": code, "message": message}
            if exception is not None:
                error["errno"] = exception.errno
            result["errors"].append(error)

    def exclude(path, reason):
        counts["excluded"] += 1
        if len(result["exclusions"]) < MAX_REPORTED_ERRORS:
            result["exclusions"].append({"path": path, "reason": reason})

    def check_stop():
        nonlocal stopped
        if cancel is not None and cancel():
            result["status"] = "cancelled"
            stopped = True
        elif counts["entries_seen"] >= max_items:
            add_error(None, "item_limit", "The entry limit was reached; remaining scope was not scanned.")
            stopped = True
        return stopped

    def report_progress():
        if progress is not None:
            progress({"phase": "inventory", "counts": dict(counts), "elapsed_seconds": round(time.monotonic() - started, 3)})

    def process(path, info, root, ancestors, parent_fd, *, is_root=False, depth=0):
        """Record one entry, optionally returning an owned traversal frame."""
        if path in seen_paths:
            return None
        seen_paths.add(path)
        counts["entries_seen"] += 1
        if any(path == value or path.startswith(value + os.sep) for value in exclusions):
            exclude(path, "user-ignore")
            return None
        if stat.S_ISLNK(info.st_mode):
            counts["symlinks"] += 1
            exclude(path, "symbolic-link")
            if is_root:
                add_error(path, "symlink_root", "A symbolic link is not a scan root.")
            return None
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            counts["special_files"] += 1
            exclude(path, "special-file")
            return None
        directory = stat.S_ISDIR(info.st_mode)
        kind = "directory" if directory else "file"
        reason = _policy_reason(path, info, ancestors)
        if not is_root and info.st_dev != root["device"]:
            reason = "unselected-volume"
            add_error(path, "unselected_volume", "A different volume requires its own explicit scan root.")
        child_fd = None
        repository = False
        if directory and reason is None:
            if depth >= MAX_DEPTH:
                reason = "depth-limit"
                add_error(path, "depth_limit", "Maximum traversal depth reached.")
            else:
                try:
                    if path == os.sep:
                        child_fd = os.open(os.sep, _directory_flags())
                    else:
                        child_fd = os.open(os.path.basename(path), _directory_flags(), dir_fd=parent_fd)
                    opened = os.fstat(child_fd)
                    if not _matches_stat(_stat_values(info), opened):
                        raise CleanMacError("The directory changed while opening.", "changed_directory")
                    repository = _is_repository_fd(child_fd)
                    if repository:
                        reason = "source-repository"
                except (OSError, CleanMacError) as exc:
                    if child_fd is not None:
                        os.close(child_fd)
                        child_fd = None
                    reason = "unreadable-directory"
                    add_error(path, "access_denied" if isinstance(exc, OSError) and exc.errno in (errno.EACCES, errno.EPERM) else "directory_error", str(exc), exc if isinstance(exc, OSError) else None)
        values = _stat_values(info)
        item = {
            "path": path, "root": root["path"], "kind": kind,
            **values,
            "allocated_size": getattr(info, "st_blocks", 0) * 512,
            "uid": info.st_uid, "gid": info.st_gid,
            "is_placeholder": _placeholder(path, info),
            "protected": reason is not None, "protection_reason": reason,
            "ancestors": [dict(value) for value in ancestors],
            "is_app_data": _is_app_data(path),
            "category_hint": "app-data" if _is_app_data(path) else "personal-file",
        }
        item["id"] = "item-" + digest({"path": path, **values, "ancestors": item["ancestors"]})[:24]
        result["items"].append(item)
        counts["items"] += 1
        counts["directories" if directory else "files"] += 1
        counts["protected"] += int(reason is not None)
        counts["placeholders"] += int(item["is_placeholder"])
        if not directory:
            identity = (info.st_dev, info.st_ino)
            if identity in sized_files:
                counts["duplicate_file_aliases"] += 1
            else:
                sized_files.add(identity)
                counts["unique_files"] += 1
                counts["logical_bytes"] += info.st_size
                counts["allocated_bytes"] += item["allocated_size"]
        if reason:
            exclude(path, reason)
            if child_fd is not None:
                os.close(child_fd)
            if is_root:
                add_error(path, "protected_root", "This root requires a dedicated adapter or is excluded from generic scanning.")
            return None
        if not directory:
            if is_root:
                counts["scanned_roots"] += 1
            return None
        identity = (info.st_dev, info.st_ino)
        if identity in visited_directories:
            os.close(child_fd)
            exclude(path, "already-inventoried-directory")
            return None
        visited_directories.add(identity)
        try:
            iterator = os.scandir(child_fd)
        except OSError as exc:
            os.close(child_fd)
            item["protected"] = True
            item["protection_reason"] = "unreadable-directory"
            counts["protected"] += 1
            add_error(path, "access_denied" if exc.errno in (errno.EACCES, errno.EPERM) else "directory_error", str(exc), exc)
            return None
        if is_root:
            counts["scanned_roots"] += 1
        return {"fd": child_fd, "iterator": iterator, "path": path, "root": root,
                "ancestors": ancestors + [{"path": path, "device": info.st_dev, "inode": info.st_ino, "is_repository": repository}],
                "depth": depth}

    try:
        report_progress()
        for path in normalized:
            if check_stop():
                break
            if path in seen_paths:
                continue
            parent_fd = None
            try:
                if path == os.sep:
                    info, ancestors = os.lstat(os.sep), []
                else:
                    parent_fd, ancestors = _open_parent(path)
                    info = os.stat(os.path.basename(path), dir_fd=parent_fd, follow_symlinks=False)
                root = {"path": path, "device": info.st_dev}
                frame = process(path, info, root, ancestors, parent_fd, is_root=True)
                if frame is not None:
                    frames.append(frame)
            except OSError as exc:
                add_error(path, "access_denied" if exc.errno in (errno.EACCES, errno.EPERM) else "root_unavailable", str(exc), exc)
            finally:
                if parent_fd is not None:
                    os.close(parent_fd)
            while frames and not stopped:
                if check_stop():
                    break
                frame = frames[-1]
                try:
                    entry = next(frame["iterator"])
                except StopIteration:
                    frame["iterator"].close()
                    os.close(frame["fd"])
                    frames.pop()
                    continue
                except OSError as exc:
                    add_error(frame["path"], "directory_error", str(exc), exc)
                    frame["iterator"].close()
                    os.close(frame["fd"])
                    frames.pop()
                    continue
                entry_path = os.path.join(frame["path"], entry.name)
                try:
                    # DirEntry.stat uses the enumerated directory descriptor.
                    info = entry.stat(follow_symlinks=False)
                    child = process(entry_path, info, frame["root"], frame["ancestors"], frame["fd"], depth=frame["depth"] + 1)
                    if child is not None:
                        frames.append(child)
                except OSError as exc:
                    counts["entries_seen"] += 1
                    add_error(entry_path, "access_denied" if exc.errno in (errno.EACCES, errno.EPERM) else "entry_changed", str(exc), exc)
                if counts["entries_seen"] % 128 == 0:
                    report_progress()
        if result["status"] != "cancelled" and counts["scanned_roots"] == 0 and (result["errors"] or result["exclusions"]):
            result["status"] = "unavailable"
    except KeyboardInterrupt:
        result["status"] = "cancelled"
    finally:
        for frame in reversed(frames):
            frame["iterator"].close()
            os.close(frame["fd"])
    result["completed_at"] = utc_now()
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    report_progress()
    return result

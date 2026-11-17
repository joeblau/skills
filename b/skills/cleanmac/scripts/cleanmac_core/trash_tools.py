"""Exact, explicitly approved permanent erasure within current-user local Trash.

This module has its own scope gate. Generic file inventory and Trash recovery
protections are not relaxed. A private same-volume capture isolates one selected
regular file before final content verification and unlink; there is no empty-all.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
import platform
import plistlib
import pwd
import stat
import time

from .actions import checkpoint_action, create_action_plan, execute_action_plan
from .common import CleanMacError, digest, new_id, utc_now
from .executor import _hash_fd, _identity, _matches, _open_directory, _rename_exclusive
from .permissions import SETTINGS_URLS, describe_access_error

SOURCES = [
    "https://support.apple.com/guide/mac-help/delete-files-and-folders-on-mac-mchlp1093/mac",
    "https://developer.apple.com/library/archive/documentation/FileManagement/Conceptual/APFS_Guide/ToolsandAPIs/ToolsandAPIs.html",
]
TOOL_SPECS = [{
    "name": "trash", "title": "Review and permanently erase selected Trash files", "feature_ids": ["CM-026"],
    "actions": ["inspect", "plan", "apply"], "scope_required": True,
    "description": "Exact current-user startup/external-volume Trash roots. One regular file per irreversible plan; no folder erasure or whole-Trash operation.",
    "requirements": ["macOS", "Read/write access to the explicitly selected current-user Trash", "Volume supports exclusive atomic rename"],
    "configuration": {"max_items": "Inventory limit, 1 to 100000; default 20000"},
    "limitations": ["Mail Trash needs the Mail app or provider workflow; this tool never edits mailbox data.",
                    "Permanent erasure has no CleanMac recovery or secure-overwrite guarantee. New arrivals remain untouched."],
}]
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_DATALESS = getattr(stat, "SF_DATALESS", 0x40000000)
_LOCKED = (getattr(stat, "UF_IMMUTABLE", 2) | getattr(stat, "SF_IMMUTABLE", 0x20000)
           | getattr(stat, "UF_APPEND", 4) | getattr(stat, "SF_APPEND", 0x40000))
_PACKAGES = (".app", ".photoslibrary", ".photolibrary", ".musiclibrary", ".bundle", ".framework", ".backupbundle", ".sparsebundle")
_PROTECTED_NAMES = {".ssh", ".gnupg", ".codex", ".claude", ".agents", ".git", ".Trash", ".Trashes"}


def _fail(message, code="trash_unavailable", **details):
    return CleanMacError(message, code=code, details=details or None)


def _home():
    # HOME is not authority to erase another account's files.
    return pwd.getpwuid(os.getuid()).pw_dir


def _access_error(path, error):
    result = describe_access_error(path, error)
    own_trash = str(Path(_home()) / ".Trash")
    if getattr(error, "errno", None) == errno.EPERM and (path == own_trash or path.startswith(own_trash + "/")):
        # There is no dedicated Trash switch in Files and Folders. This is a
        # handoff suggestion, not a diagnosis or automatic privacy change.
        result["suggested_settings_pane"] = "full-disk-access"
        result["system_settings_url"] = SETTINGS_URLS["full-disk-access"]
        for action in result["actions"]:
            if action["id"] == "open-settings":
                action["pane"] = "full-disk-access"
    return result


def _volume(path):
    from .app_tools import _run
    result = _run(["/usr/sbin/diskutil", "info", "-plist", path], timeout=10)
    if result["returncode"] != 0:
        raise _fail("The selected Trash volume could not be identified.", "volume_unverified")
    try:
        value = plistlib.loads(result["stdout"].encode("utf-8"))
    except (ValueError, plistlib.InvalidFileException) as exc:
        raise _fail("The selected volume identity was not recognized.", "volume_unverified") from exc
    if not isinstance(value, dict) or not isinstance(value.get("VolumeUUID"), str) or not isinstance(value.get("DeviceIdentifier"), str):
        raise _fail("A stable mounted-volume identity is required before Trash erasure.", "volume_unverified")
    return {"uuid": value["VolumeUUID"], "device_identifier": value["DeviceIdentifier"], "mount_point": value.get("MountPoint")}


def _scope(path):
    if not isinstance(path, str) or not path or "\0" in path:
        raise _fail("Choose an exact local Trash root.", "invalid_trash_scope")
    path = os.path.abspath(os.path.expanduser(path))
    home = os.path.abspath(_home())
    if path == str(Path(home) / ".Trash"):
        return path, "startup", home
    parts = Path(path).parts
    if len(parts) == 5 and parts[:2] == ("/", "Volumes") and parts[3:] == (".Trashes", str(os.getuid())):
        volume = str(Path("/Volumes") / parts[2])
        if not os.path.ismount(volume):
            raise _fail("The selected external Trash volume is not mounted.", "volume_unavailable")
        return path, "external", volume
    raise _fail("Choose your ~/.Trash or an explicitly mounted /Volumes/<volume>/.Trashes/<your uid>. Mail, other users, and arbitrary folders are not supported.", "invalid_trash_scope")


def _open(path, expected=None):
    fd = os.open("/", _DIR)
    current = "/"
    ancestors = []
    expected_by_path = {x["path"]: x for x in expected} if expected is not None else None
    try:
        for part in (None, *Path(path).parts[1:]):
            if part is not None:
                before = os.stat(part, dir_fd=fd, follow_symlinks=False)
                if getattr(before, "st_flags", 0) & _DATALESS:
                    raise _fail("Cloud-only locations are not hydrated for Trash review.", "placeholder_excluded")
                child = os.open(part, _DIR, dir_fd=fd)
                opened = os.fstat(child)
                if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                    os.close(child)
                    raise _fail("A selected Trash ancestor changed while opening.", "trash_ancestor_changed")
                os.close(fd)
                fd = child
                current = os.path.join(current, part)
            info = os.fstat(fd)
            if getattr(info, "st_flags", 0) & _DATALESS:
                raise _fail("Cloud-only locations are not hydrated for Trash review.", "placeholder_excluded")
            value = {"path": current, "device": info.st_dev, "inode": info.st_ino}
            ancestors.append(value)
            if expected_by_path is not None and expected_by_path.get(current) != value:
                raise _fail("A selected Trash ancestor changed; inspect again.", "trash_ancestor_changed")
        return fd, ancestors
    except BaseException:
        os.close(fd)
        raise


def _root(path, expected=None):
    path, kind, volume_path = _scope(path)
    fd, ancestors = _open(path, expected.get("ancestors") if expected else None)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise _fail("The current user's Trash must be owner-only before erasure.", "unsafe_trash_permissions")
        volume = _volume(volume_path)
        if expected and (volume != expected["volume"] or (info.st_dev, info.st_ino) != (expected["device"], expected["inode"])):
            raise _fail("The selected volume or Trash directory changed.", "trash_volume_changed")
        return fd, {"path": path, "kind": kind, "device": info.st_dev, "inode": info.st_ino,
                    "ancestors": ancestors, "volume": volume}
    except BaseException:
        os.close(fd)
        raise


def _reason(path, info, root_device):
    if info.st_dev != root_device:
        return "unselected-volume"
    if getattr(info, "st_flags", 0) & _DATALESS:
        return "cloud-placeholder"
    if getattr(info, "st_flags", 0) & _LOCKED:
        return "locked-file"
    if info.st_uid != os.getuid():
        return "different-owner"
    if stat.S_ISLNK(info.st_mode):
        return "symbolic-link"
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        return "special-file"
    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
        return "multiple-hard-links"
    name = Path(path).name
    if name.startswith(".cleanmac-erase-"):
        return "active-recovery-stage"
    if name in _PROTECTED_NAMES or name.lower().endswith(_PACKAGES):
        return "protected-package-or-data"
    return None


def _inspect(options, store, cancel=None, progress=None):
    roots = options.get("roots", [])
    if not isinstance(roots, list) or not roots:
        raise _fail("Choose explicit Trash roots before inspection.", "scope_required")
    limit = options.get("configuration", {}).get("max_items", 20000)
    if type(limit) is not int or not 1 <= limit <= 100000:
        raise _fail("max_items must be between 1 and 100000.", "invalid_configuration")
    report = {"schema_version": 1, "id": new_id("trashscan"), "tool": "trash", "created_at": utc_now(),
              "status": "complete", "read_only": True, "roots": [], "items": [], "coverage": [], "errors": [],
              "summary": {"logical_bytes": 0, "allocated_bytes_estimate": 0, "eligible_files": 0}, "sources": SOURCES,
              "limitations": TOOL_SPECS[0]["limitations"] + ["Folder erasure is unsupported; protected packages and recovery stages are not traversed."]}
    started = time.monotonic()
    visited = set()
    identities = set()
    for requested in roots:
        path, _, _ = _scope(requested)
        if path in visited:
            continue
        visited.add(path)
        if cancel and cancel():
            report["status"] = "cancelled"
            break
        coverage = {"path": path, "status": "complete", "entries": 0}
        report["coverage"].append(coverage)
        try:
            root_fd, root = _root(path)
        except (OSError, CleanMacError) as exc:
            coverage["status"] = "unscanned"
            report["status"] = "partial"
            report["errors"].append(_access_error(path, exc) if isinstance(exc, OSError) else {"path": path, **exc.as_dict()})
            continue
        report["roots"].append(root)

        def visit(fd, current, ancestors, depth):
            with os.scandir(fd) as entries:
                for entry in entries:
                    if cancel and cancel():
                        coverage["status"] = report["status"] = "cancelled"
                        return
                    if len(report["items"]) >= limit or time.monotonic() - started > 20:
                        coverage["status"] = report["status"] = "partial"
                        report["errors"].append({"path": path, "code": "scan_limit"})
                        return
                    child_path = os.path.join(current, entry.name)
                    try:
                        info = entry.stat(follow_symlinks=False)
                        reason = _reason(child_path, info, root["device"])
                        directory = stat.S_ISDIR(info.st_mode)
                        item = {"path": child_path, "root": path, "kind": "directory" if directory else "file" if stat.S_ISREG(info.st_mode) else "other",
                                **_identity(info), "ctime_ns": info.st_ctime_ns, "uid": info.st_uid, "flags": getattr(info, "st_flags", 0),
                                "ancestors": ancestors, "eligible": reason is None and not directory,
                                "reason": reason or ("directory-erasure-unsupported" if directory else "Exact selection and irreversible approval required."),
                                "allocated_bytes_estimate": getattr(info, "st_blocks", 0) * 512}
                        item["id"] = "trashfile-" + digest(item)[:24]
                        report["items"].append(item)
                        coverage["entries"] += 1
                        if progress:
                            progress({"phase": "inventory", "counts": {"entries_seen": len(report["items"])}})
                        identity = (info.st_dev, info.st_ino)
                        if stat.S_ISREG(info.st_mode) and identity not in identities:
                            report["summary"]["logical_bytes"] += info.st_size
                            report["summary"]["allocated_bytes_estimate"] += item["allocated_bytes_estimate"]
                            identities.add(identity)
                        report["summary"]["eligible_files"] += int(item["eligible"])
                        if directory and reason is None and depth < 32:
                            nested = os.open(entry.name, _DIR, dir_fd=fd)
                            try:
                                opened = os.fstat(nested)
                                if (opened.st_dev, opened.st_ino) != identity:
                                    raise _fail("A Trash folder changed while scanning.", "trash_ancestor_changed")
                                visit(nested, child_path, [*ancestors, {"path": child_path, "device": info.st_dev, "inode": info.st_ino}], depth + 1)
                            finally:
                                os.close(nested)
                            if coverage["status"] != "complete":
                                return
                        elif directory and depth >= 32 and reason is None:
                            coverage["status"] = report["status"] = "partial"
                    except (OSError, CleanMacError) as exc:
                        report["status"] = coverage["status"] = "partial"
                        report["errors"].append(_access_error(child_path, exc) if isinstance(exc, OSError) else {"path": child_path, **exc.as_dict()})
        try:
            visit(root_fd, path, root["ancestors"], 0)
        except OSError as exc:
            coverage["status"] = "unscanned"
            report["status"] = "partial"
            report["errors"].append(_access_error(path, exc))
        finally:
            os.close(root_fd)
    if report["coverage"] and all(c["status"] == "unscanned" for c in report["coverage"]):
        report["status"] = "permission-needed" if any(e.get("status") == "permission-needed" for e in report["errors"]) else "unavailable"
    return store.put("scans", report)


def _file(item, root, *, content=True, cancel=None, captured=False, directory_fd=None, name=None):
    if not isinstance(item, dict) or item.get("eligible") is not True or item.get("kind") != "file":
        raise _fail("Choose an eligible regular Trash file.", "ineligible_trash_item")
    path = item.get("path")
    if not isinstance(path, str) or not path.startswith(root["path"] + "/") or os.path.normpath(path) != path:
        raise _fail("The selected file is outside the approved Trash.", "invalid_trash_scope")
    owns_parent = directory_fd is None
    parent = _open_directory(str(Path(path).parent), expected=item["ancestors"]) if owns_parent else directory_fd
    try:
        name = name or Path(path).name
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if _reason(path, before, root["device"]) is not None or not _matches(before, item) or (not captured and before.st_ctime_ns != item["ctime_ns"]):
            raise _fail("The selected Trash file changed after inspection.", "trash_file_changed")
        fd = os.open(name, _FILE, dir_fd=parent)
        try:
            if not _matches(os.fstat(fd), item):
                raise _fail("The selected Trash file changed while opening.", "trash_file_changed")
            hash_value = _hash_fd(fd, cancel) if content else None
            after = os.fstat(fd)
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (not _matches(after, item) or after.st_ctime_ns != before.st_ctime_ns
                    or not _matches(named, item) or named.st_ctime_ns != after.st_ctime_ns
                    or _reason(path, named, root["device"]) is not None
                    or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)):
                raise _fail("The selected Trash file changed during verification.", "trash_file_changed")
            if content and item.get("content_sha256") and hash_value != item["content_sha256"]:
                raise _fail("The selected Trash file's contents changed.", "trash_content_changed")
            return hash_value
        finally:
            os.close(fd)
    finally:
        if owns_parent:
            os.close(parent)


def _plan(options, store, cancel=None):
    scan = store.get("scans", options.get("record_id"))
    if scan.get("tool") != "trash":
        raise _fail("Choose a saved Trash inspection.", "invalid_scan")
    selected = options.get("selected", [])
    if not isinstance(selected, list) or len(selected) != 1:
        raise _fail("Select exactly one regular Trash file per permanent-erasure plan.", "invalid_selection")
    items = [item for item in scan.get("items", []) if item.get("id") == selected[0]]
    if len(items) != 1:
        raise _fail("The selected Trash item is not in this report.", "invalid_selection")
    item = dict(items[0])
    roots = [root for root in scan.get("roots", []) if root.get("path") == item.get("root")]
    if len(roots) != 1:
        raise _fail("The item's scanned Trash root is missing.", "invalid_scan")
    root = roots[0]
    fd, _ = _root(root["path"], root)
    os.close(fd)
    item["content_sha256"] = _file(item, root, cancel=cancel)
    return create_action_plan(store, "trash", "erase", {"root": root, "item": item, "irreversible": True, "recovery": "unavailable"},
        {"title": "Permanently erase exactly one selected Trash file", "path": item["path"], "volume": root["volume"],
         "logical_bytes": item["logical_size"], "action": "permanent-erasure", "irreversible": True, "recovery": "unavailable",
         "consequence": "This file cannot be restored by CleanMac or Put Back after erasure. Backups, snapshots, and other copies are not securely overwritten.",
         "scope": "One approved file only; folders and new Trash arrivals are preserved."})


def _apply(options, store, cancel=None, progress=None):
    def validate(plan):
        payload = plan["payload"]
        if payload.get("irreversible") is not True or payload.get("recovery") != "unavailable":
            raise _fail("This is not an explicit permanent-erasure plan.", "invalid_erase_plan")
        root, item = payload["root"], payload["item"]
        fd, _ = _root(root["path"], root)
        os.close(fd)
        _file(item, root, cancel=cancel)

    def execute(plan):
        root, item = plan["payload"]["root"], plan["payload"]["item"]
        stage_name = ".cleanmac-erase-" + plan["id"]
        capture_name = "selected-file"
        stage_path = str(Path(root["path"]) / stage_name)
        captured_path = str(Path(stage_path) / capture_name)
        source_name = Path(item["path"]).name
        checkpoint = {"original_path": item["path"], "captured_path": captured_path, "stage_path": stage_path,
                      "phase": "before-capture", "erased": False, "irreversible": True}
        root_fd = source_fd = stage_fd = None
        captured = erased = False

        def check_stage():
            # A descriptor survives renaming its directory. It must still name
            # the approved private directory inside the pinned Trash root.
            check_root, _ = _open(root["path"], root["ancestors"])
            os.close(check_root)
            named = os.stat(stage_name, dir_fd=root_fd, follow_symlinks=False)
            opened = os.fstat(stage_fd)
            if (not stat.S_ISDIR(named.st_mode) or named.st_uid != os.getuid()
                    or stat.S_IMODE(named.st_mode) != 0o700 or named.st_dev != root["device"]
                    or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)):
                raise _fail("The private capture directory moved or was replaced.", "erase_stage_changed")

        try:
            root_fd, _ = _root(root["path"], root)
            source_fd = _open_directory(str(Path(item["path"]).parent), expected=item["ancestors"])
            _file(item, root, cancel=cancel, directory_fd=source_fd)
            checkpoint_action(checkpoint)
            os.mkdir(stage_name, 0o700, dir_fd=root_fd)
            stage_fd = os.open(stage_name, _DIR, dir_fd=root_fd)
            stage_info = os.fstat(stage_fd)
            if stage_info.st_uid != os.getuid() or stat.S_IMODE(stage_info.st_mode) != 0o700 or stage_info.st_dev != root["device"]:
                raise _fail("The private erasure capture is not safe.", "unsafe_erase_stage")
            os.fsync(root_fd)
            checkpoint.update(stage_identity={"device": stage_info.st_dev, "inode": stage_info.st_ino})
            checkpoint_action(checkpoint)
            _rename_exclusive(source_fd, source_name, stage_fd, capture_name)
            captured = True
            checkpoint.update(phase="captured")
            checkpoint_action(checkpoint)
            _file(item, root, cancel=cancel, captured=True, directory_fd=stage_fd, name=capture_name)
            # Reopen the original hierarchy and mounted volume after capture.
            check_root, _ = _root(root["path"], root)
            os.close(check_root)
            check_parent = _open_directory(str(Path(item["path"]).parent), expected=item["ancestors"])
            os.close(check_parent)
            if cancel and cancel():
                raise _fail("Cancelled before permanent erasure.", "cancelled")
            checkpoint.update(phase="before-unlink")
            checkpoint_action(checkpoint)
            # Rehash after durable journaling, immediately before the irreversible
            # step; the only deletion target is inside the pinned private capture.
            _file(item, root, cancel=cancel, captured=True, directory_fd=stage_fd, name=capture_name)
            check_stage()
            os.unlink(capture_name, dir_fd=stage_fd)
            erased = True
            os.fsync(stage_fd)
            checkpoint.update(phase="erased", erased=True)
            checkpoint_action(checkpoint)
            check_stage()
            os.rmdir(stage_name, dir_fd=root_fd)
            os.fsync(root_fd)
            return {"status": "completed", "path": item["path"], "erased": True, "verified": True,
                    "logical_bytes_erased": item["logical_size"], "recovery": "unavailable",
                    "free_space_change": "not_measured", "new_arrivals_preserved": True}
        except BaseException:
            if captured and not erased and stage_fd is not None and source_fd is not None:
                try:
                    # Never overwrite a new arrival at the source path. Restore a
                    # captured replacement too: a failed identity check is not
                    # authority to discard someone else's changed file.
                    check_parent = _open_directory(str(Path(item["path"]).parent), expected=item["ancestors"])
                    os.close(check_parent)
                    _rename_exclusive(stage_fd, capture_name, source_fd, source_name)
                    checkpoint.update(phase="restored-after-stop", captured_path=None)
                    checkpoint_action(checkpoint)
                except BaseException:
                    checkpoint.update(phase="retained-for-manual-recovery", recovery_guidance="The file was not erased. Restore the captured file only after checking the original path for conflicts.")
                    try:
                        checkpoint_action(checkpoint)
                    except BaseException:
                        pass
            raise
        finally:
            for fd in (stage_fd, source_fd, root_fd):
                if fd is not None:
                    os.close(fd)

    return execute_action_plan(store, options.get("record_id"), options.get("approve"), "trash", "erase", validate, execute,
                               cancel=cancel, progress=progress)


def dispatch(name, action, options, *, store, cancel=None, progress=None):
    if name != "trash" or action not in TOOL_SPECS[0]["actions"]:
        raise _fail("Unsupported Trash action.", "unsupported_action")
    if platform.system() != "Darwin":
        return {"tool": "trash", "status": "unavailable", "reason": "This Trash adapter supports macOS only."}
    if not isinstance(options, dict) or not isinstance(options.get("configuration", {}), dict):
        raise _fail("Trash options must be an object.", "invalid_configuration")
    if action == "inspect":
        return _inspect(options, store, cancel, progress)
    if action == "plan":
        return _plan(options, store, cancel)
    return _apply(options, store, cancel, progress)

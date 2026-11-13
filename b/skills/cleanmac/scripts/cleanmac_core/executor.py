"""Journaled, explicitly approved file moves. No permanent-deletion fallback.

Only regular, single-link files can be captured into private same-volume state.
The native helper receives that captured path, never the user's original path.
Unknown interrupted operations are retained for recovery rather than retried.
"""
from __future__ import annotations

import copy
import ctypes
import errno
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

from .common import CleanMacError, digest, new_id, utc_now


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
_TERMINAL = {"trashed", "restored", "failed", "skipped", "conflict"}
_UNCERTAIN = {"pending", "captured", "ambiguous", "verify-denied", "restore_pending", "restore_captured"}
_MAX_NATIVE_RECEIPT = 65536


def _error(message, code="execution_failed", **details):
    return CleanMacError(message, code=code, details=details or None)


def _exception(error):
    if isinstance(error, CleanMacError):
        return {"code": error.code, "message": str(error), "details": error.details}
    return {"code": "filesystem_error", "message": str(error), "errno": getattr(error, "errno", None)}


def _absolute(path):
    if not isinstance(path, str) or not os.path.isabs(path) or "\x00" in path:
        raise _error("An absolute, NUL-free path is required.", "invalid_path")
    if path != os.path.normpath(path) or ".." in Path(path).parts:
        raise _error("Noncanonical path rejected.", "invalid_path")
    return path


def _open_directory(path, expected=None, private=False):
    """Open each ancestor without following links, validating scan identities."""
    path = _absolute(os.fspath(path))
    expected = {x["path"]: x for x in expected} if expected is not None else None
    fd = os.open("/", _DIR_FLAGS)
    current = "/"
    try:
        for part in (None, *Path(path).parts[1:]):
            if part is not None:
                child = os.open(part, _DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
                current = os.path.join(current, part)
            value = os.fstat(fd)
            if expected is not None:
                identity = expected.get(current)
                if identity is None or (value.st_dev, value.st_ino) != (identity["device"], identity["inode"]):
                    raise _error("A parent directory changed since the scan.", "ancestor_changed", path=current)
        value = os.fstat(fd)
        if private and (value.st_uid != os.getuid() or stat.S_IMODE(value.st_mode) != 0o700):
            raise _error("Private staging must be owned by this user with mode 0700.", "unsafe_state", path=path)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _rename_function():
    """Return the platform's atomic no-replacement rename, or fail closed.

    Darwin's installed rename(2) and sys/stdio.h define RENAME_EXCL as 0x4.
    Linux renameat2 uses RENAME_NOREPLACE=1. No check-then-overwrite fallback.
    """
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        name, flags = "renameatx_np", 0x4
    elif sys.platform.startswith("linux"):
        name, flags = "renameat2", 1
    else:
        raise _error("Atomic no-overwrite file moves are unavailable on this platform.", "unavailable")
    function = getattr(library, name, None)
    if function is None:
        raise _error("The platform lacks an atomic no-overwrite file move API.", "unavailable")
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    return function, flags


def _rename_exclusive(source_fd, source_name, destination_fd, destination_name):
    for name in (source_name, destination_name):
        if not name or name in {".", ".."} or "/" in name or "\x00" in name:
            raise _error("Only a single filename is accepted for a file move.", "invalid_path")
    function, flags = _rename_function()
    if function(source_fd, os.fsencode(source_name), destination_fd, os.fsencode(destination_name), flags):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), destination_name)
    os.fsync(source_fd)
    if source_fd != destination_fd:
        os.fsync(destination_fd)


def _identity(value):
    return {
        "device": value.st_dev, "inode": value.st_ino,
        "logical_size": value.st_size, "mtime_ns": value.st_mtime_ns,
        "mode": value.st_mode, "nlink": value.st_nlink,
    }


def _matches(value, item):
    # Rename can update ctime, so the captured identity deliberately omits it.
    return stat.S_ISREG(value.st_mode) and all(
        getattr(value, attr) == item[key]
        for key, attr in (("device", "st_dev"), ("inode", "st_ino"),
                          ("logical_size", "st_size"), ("mtime_ns", "st_mtime_ns"),
                          ("mode", "st_mode"), ("nlink", "st_nlink"))
    )


def _hash_fd(fd, cancel=None, algorithm="sha256"):
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise _error("Only regular single-link files can be verified.", "ineligible")
    os.lseek(fd, 0, os.SEEK_SET)
    hasher = hashlib.new(algorithm)
    while True:
        if cancel and cancel():
            raise _error("Stopped before moving the selected item.", "cancelled")
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        hasher.update(chunk)
    after = os.fstat(fd)
    if _identity(before) != _identity(after) or before.st_ctime_ns != after.st_ctime_ns:
        raise _error("File content changed while it was being verified.", "file_changed")
    return hasher.hexdigest()


def _validate_live(item, cancel=None):
    from .inventory import assert_item_identity, safe_open_item
    assert_item_identity(item)
    expected_hash = item.get("content_sha256") or item.get("sha256")
    if expected_hash:
        fd = safe_open_item(item)
        try:
            if not hmac.compare_digest(_hash_fd(fd, cancel), expected_hash):
                raise _error("File content changed since the approved plan.", "file_changed", item_id=item["id"])
        finally:
            os.close(fd)
        assert_item_identity(item)


def _validate_rule(item, cancel=None):
    from .rules import classify_items
    if cancel and cancel():
        raise _error("Stopped before validating the selected rule.", "cancelled")
    findings = classify_items([item], cancel=cancel)
    if cancel and cancel():
        raise _error("Stopped while validating the selected rule.", "cancelled")
    matching = [f for f in findings if f.get("item_id") == item["id"]]
    if any(not f.get("eligible", False) for f in matching):
        raise _error("Current cleanup rules no longer permit this item.", "rule_changed", item_id=item["id"])
    recorded = item.get("matched_rule")
    if recorded:
        if not any(f.get("eligible") and f.get("rule_id") == recorded.get("rule_id")
                   and f.get("rule_version") == recorded.get("rule_version")
                   and f.get("rule_digest") == recorded.get("rule_digest") for f in matching):
            raise _error("The selected cleanup rule changed or no longer matches.", "rule_changed", item_id=item["id"])
        for finding in matching:
            if finding.get("eligible") and finding.get("content_sha512"):
                if finding["content_sha512"] != item.get("rule_content_sha512"):
                    raise _error("The cache content proof differs from the approved plan; rescan it.", "rule_changed", item_id=item["id"])
    elif (item.get("is_app_data") or item.get("category_hint") == "app-data") and not any(f.get("eligible") for f in matching):
        raise _error("App data requires a currently validated cleanup rule.", "ineligible", item_id=item["id"])


def _native_executable(native_path=None):
    configured = native_path if native_path is not None else os.environ.get("CLEANMAC_NATIVE_PATH")
    path = Path(configured) if configured is not None else Path(__file__).resolve().parents[2] / "native/bin/cleanmac-native"
    path = Path(_absolute(os.path.abspath(os.fspath(path))))
    try:
        value = path.lstat()
    except OSError as error:
        raise _error("Build the native Trash helper before applying a cleanup plan.", "native_helper_unavailable") from error
    if not stat.S_ISREG(value.st_mode) or not os.access(path, os.X_OK):
        raise _error("The native Trash helper must be a regular executable file.", "native_helper_unavailable")
    try:
        result = subprocess.run([os.fspath(path), "info"], capture_output=True, text=True, timeout=5, check=False)
        if result.returncode or len(result.stdout) > 65536:
            raise ValueError("Helper info failed")
        info = json.loads(result.stdout)
        if (not isinstance(info, dict) or type(info.get("protocol_version")) is not int
                or info["protocol_version"] != 1 or info.get("platform") != "macos"
                or info.get("privileged") is not False
                or not isinstance(info.get("version"), str) or not info["version"].startswith("1.")
                or not isinstance(info.get("capabilities"), list)
                or not {"trash", "trash_preflight"}.issubset(info["capabilities"])):
            raise ValueError("Incompatible helper protocol")
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeError) as error:
        raise _error("The native helper is incompatible or unavailable; rebuild it before cleanup.", "native_helper_unavailable") from error
    return path


def _trash_access_error(native_error=None):
    return _error(
        "Cleanup needs access to Trash so the moved files can be verified and restored. No selected file has been moved by this preflight. Continue scanning, or review the running host's macOS privacy access and retry the unchanged plan.",
        "trash_access_required", permission_needed=True, blocked_scope="trash_verification_and_restore",
        system_settings_url="x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles",
        actions=["continue_scanning", "open_settings", "retry_after_user_change"], native_error=native_error,
    )


def _preflight_recovery(native, item):
    """Require the helper AND this runner to access the actual Trash directory.

    Foundation can move an item even when TCC denies directory access to its
    caller. A read-only create:false destination probe must precede capture.
    """
    try:
        result = subprocess.run(
            [os.fspath(native), "preflight", "--path", item["path"]],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise _trash_access_error(_exception(error)) from error
    raw = result.stdout if result.returncode == 0 else result.stderr
    try:
        if len(raw) > 65536:
            raise ValueError("Oversized preflight result")
        info = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise _trash_access_error({"code": "invalid_preflight", "message": "The helper did not supply a usable read-only Trash preflight."}) from error
    if result.returncode or not isinstance(info, dict) or info.get("status") != "available":
        raise _trash_access_error(info.get("error") if isinstance(info, dict) else None)
    trash_directory = _absolute(info.get("trash_directory"))
    if info.get("created_directory") is not False:
        raise _trash_access_error({"code": "invalid_preflight", "message": "Trash preflight must not create directories."})
    try:
        descriptor = _open_directory(trash_directory)
        try:
            value = os.fstat(descriptor)
            if (value.st_dev != item["device"] or info.get("source_device") != item["device"]
                    or info.get("trash_device") != value.st_dev):
                raise _error("A verified same-volume Trash destination is unavailable.", "cross_device_unavailable")
        finally:
            os.close(descriptor)
    except OSError as error:
        raise _trash_access_error(_exception(error)) from error
    return trash_directory


def _ensure_stage(store, operation):
    root_fd = _open_directory(os.fspath(store.root), private=True)
    try:
        try:
            os.mkdir("quarantine", 0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        quarantine_fd = os.open("quarantine", _DIR_FLAGS, dir_fd=root_fd)
    finally:
        os.close(root_fd)
    try:
        value = os.fstat(quarantine_fd)
        if value.st_uid != os.getuid() or stat.S_IMODE(value.st_mode) != 0o700:
            raise _error("Unsafe private quarantine directory.", "unsafe_state")
        os.mkdir(operation["id"], 0o700, dir_fd=quarantine_fd)
        result = os.open(operation["id"], _DIR_FLAGS, dir_fd=quarantine_fd)
        os.fsync(quarantine_fd)
        return result
    finally:
        os.close(quarantine_fd)


def _stage_paths(store, operation):
    folder = Path(store.root) / "quarantine" / operation["id"]
    return folder / Path(operation["original_path"]).name, folder / (".cleanmac-native-receipt-" + operation["id"] + ".json")


def _read_native_receipt(path):
    parent_fd = _open_directory(os.fspath(Path(path).parent), private=True)
    try:
        fd = os.open(Path(path).name, _FILE_FLAGS, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    try:
        value = os.fstat(fd)
        if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.getuid()
                or value.st_nlink != 1 or stat.S_IMODE(value.st_mode) & 0o077
                or value.st_size > _MAX_NATIVE_RECEIPT):
            raise _error("Invalid native operation receipt.", "invalid_native_receipt")
        raw = os.read(fd, _MAX_NATIVE_RECEIPT + 1)
        if len(raw) > _MAX_NATIVE_RECEIPT:
            raise _error("Native operation receipt is too large.", "invalid_native_receipt")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("Receipt must be an object")
        return result
    except (ValueError, UnicodeError) as error:
        raise _error("Native operation receipt is unreadable.", "invalid_native_receipt") from error
    finally:
        os.close(fd)


def _native_trash(native, stage_path, receipt_path):
    # No shell, environment-derived command, or permanent-removal fallback.
    result = subprocess.run(
        [os.fspath(native), "trash", "--path", os.fspath(stage_path), "--receipt", os.fspath(receipt_path)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    # The durable file, not stdout, is authoritative after an interrupted caller.
    try:
        receipt = _read_native_receipt(receipt_path)
    except (OSError, CleanMacError) as error:
        raise _error("The native helper did not leave a valid durable result.", "native_outcome_unknown", returncode=result.returncode) from error
    if receipt.get("status") != "trashed" or receipt.get("original_path") != os.fspath(stage_path):
        diagnostic = receipt.get("error")
        code = "permission_denied" if isinstance(diagnostic, dict) and diagnostic.get("code") == "permission_denied" else "native_outcome_unknown"
        raise _error("The native helper's result needs recovery review.", code, returncode=result.returncode, native_error=diagnostic)
    return receipt


def _check_trash_result(native_receipt, item):
    path = _absolute(native_receipt.get("trash_path"))
    parent_fd = _open_directory(os.fspath(Path(path).parent))
    try:
        fd = os.open(Path(path).name, _FILE_FLAGS, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    try:
        value = os.fstat(fd)
        if not _matches(value, item):
            raise _error("The returned Trash item does not match the captured file.", "native_outcome_unknown")
        expected_hash = item.get("content_sha256") or item.get("sha256")
        if expected_hash and _hash_fd(fd) != expected_hash:
            raise _error("The returned Trash item has changed content.", "native_outcome_unknown")
        if item.get("rule_content_sha512") and _hash_fd(fd, algorithm="sha512") != item["rule_content_sha512"]:
            raise _error("The returned cache item has changed content.", "native_outcome_unknown")
    finally:
        os.close(fd)
    return path, _identity(value)


def _rollback_capture(operation, source_fd, stage_fd):
    """Best-effort rollback only when the original name remains free."""
    name = Path(operation["original_path"]).name
    try:
        _rename_exclusive(stage_fd, name, source_fd, name)
        operation["rollback"] = "returned_to_original_path"
        operation.pop("recovery_path", None)
        return True
    except OSError as error:
        operation["rollback"] = "retained_in_private_quarantine"
        operation["recovery_path"] = operation["stage_path"]
        operation["rollback_error"] = _exception(error)
        return False


def _free_bytes(root):
    try:
        value = os.statvfs(root)
        return value.f_bavail * value.f_frsize
    except OSError:
        return None


def _save(store, receipt, status=None):
    if status is not None:
        receipt["status"] = status
    receipt["updated_at"] = utc_now()
    counts = {}
    for operation in receipt["operations"]:
        counts[operation["status"]] = counts.get(operation["status"], 0) + 1
    observed_trash = [op for op in receipt["operations"]
                      if op["status"] != "restored" and (op["status"] == "trashed" or op.get("native_result", {}).get("status") == "trashed")]
    receipt["summary"] = {
        "selected_count": len(receipt["operations"]),
        "trashed_count": len(observed_trash),
        "verified_trashed_count": counts.get("trashed", 0),
        "unverified_move_count": sum(op["status"] != "trashed" for op in observed_trash),
        "restored_count": counts.get("restored", 0),
        "failed_count": counts.get("failed", 0),
        "skipped_count": counts.get("skipped", 0),
        "conflict_count": counts.get("conflict", 0),
        "unstarted_count": counts.get("not_started", 0),
        "uncertain_count": sum(counts.get(key, 0) for key in _UNCERTAIN),
        "trash_retained_bytes_estimate": sum(op["item"].get("allocated_size", 0) or 0 for op in observed_trash),
        "reclaimed_bytes": None,
        "note": "Trash retains data. Free-space changes may include unrelated filesystem activity; shared allocation is uncertain.",
    }
    return store.put("receipts", receipt)


def _finish(store, receipt, cancelled=False):
    if any(op["status"] in _UNCERTAIN or op.get("recovery_path") for op in receipt["operations"]):
        status = "needs-recovery"
    elif cancelled:
        status = "cancelled"
    elif any(op["status"] in {"failed", "skipped", "conflict", "not_started"} for op in receipt["operations"]):
        status = "partial"
    else:
        status = "complete"
    after = _free_bytes(store.root)
    before = receipt.get("free_space_before")
    receipt["free_space_after"] = after
    receipt["measured_free_space_delta"] = after - before if after is not None and before is not None else None
    _save(store, receipt, status)
    return copy.deepcopy(receipt)


def _existing_receipt(store, receipt_id):
    # list() avoids depending on a store-specific missing-record error code.
    for receipt in store.list("receipts"):
        if receipt.get("id") == receipt_id:
            return receipt
    return None


def apply_plan(plan, *, approval_digest, store, native_path=None, cancel=None):
    """Apply exactly the approved manifest, with recoverable per-item outcomes."""
    from .plans import validate_plan
    validate_plan(plan)
    if (not isinstance(approval_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", approval_digest)
            or not hmac.compare_digest(approval_digest, plan["digest"])):
        raise _error("Approval must match the exact reviewed plan digest.", "approval_required")
    native = _native_executable(native_path)
    _rename_function()  # Check no-overwrite support before capturing anything.
    receipt_id = "receipt-" + digest({"plan_id": plan["id"], "digest": plan["digest"]})[:32]
    with store.lock("mutation"):
        receipt = _existing_receipt(store, receipt_id)
        if receipt is not None:
            _validate_receipt(receipt, plan=plan)
            if any(op["status"] in _UNCERTAIN or op.get("recovery_path") for op in receipt["operations"]):
                return _finish(store, receipt)
            if all(op["status"] != "not_started" for op in receipt["operations"]):
                return copy.deepcopy(receipt)
        else:
            receipt = {
                "id": receipt_id, "schema_version": 1, "created_at": utc_now(),
                "plan_id": plan["id"], "plan_digest": plan["digest"],
                "plan_snapshot": copy.deepcopy(plan),
                "action": "trash", "status": "running", "free_space_before": _free_bytes(store.root),
                "operations": [{"id": new_id("op"), "item_id": item["id"], "original_path": item["path"],
                                "item": copy.deepcopy(item), "status": "not_started"} for item in plan["items"]],
            }
            _save(store, receipt)
        preflighted_devices = set()
        for operation in receipt["operations"]:
            if cancel and cancel():
                return _finish(store, receipt, cancelled=True)
            if operation["status"] == "not_started" and operation["item"]["device"] not in preflighted_devices:
                _preflight_recovery(native, operation["item"])
                preflighted_devices.add(operation["item"]["device"])
        keepers = {item["id"]: item for item in plan.get("keepers", [])}
        for operation in receipt["operations"]:
            if operation["status"] != "not_started":
                continue
            if cancel and cancel():
                return _finish(store, receipt, cancelled=True)
            item = operation["item"]
            source_fd = stage_fd = captured_fd = None
            try:
                _validate_live(item, cancel)
                _validate_rule(item, cancel)
                keeper = keepers.get(item.get("keeper_id"))
                if item.get("duplicate_group_id") and keeper is None:
                    raise _error("Duplicate cleanup has no retained keeper.", "invalid_plan")
                if keeper is not None:
                    _validate_live(keeper, cancel)
                    if item.get("sha256") != keeper.get("sha256"):
                        raise _error("Duplicate keeper no longer matches its selected copy.", "file_changed")
                source_fd = _open_directory(os.fspath(Path(item["path"]).parent), item["ancestors"])
                root_fd = _open_directory(os.fspath(store.root), private=True)
                try:
                    if os.fstat(root_fd).st_dev != item["device"]:
                        raise _error("This item is on another volume; safe same-volume capture is unavailable.", "cross_device_unavailable")
                finally:
                    os.close(root_fd)
                if cancel and cancel():
                    return _finish(store, receipt, cancelled=True)
                stage_path, native_receipt_path = _stage_paths(store, operation)
                operation.update(stage_path=os.fspath(stage_path), native_receipt_path=os.fspath(native_receipt_path), status="pending")
                _save(store, receipt)
                stage_fd = _ensure_stage(store, operation)
                _rename_exclusive(source_fd, Path(item["path"]).name, stage_fd, stage_path.name)
                operation["status"] = "captured"
                _save(store, receipt)
                captured_fd = os.open(stage_path.name, _FILE_FLAGS, dir_fd=stage_fd)
                value = os.fstat(captured_fd)
                if not _matches(value, item):
                    raise _error("The file changed during capture; it will not be sent to Trash.", "file_changed")
                expected_hash = item.get("content_sha256") or item.get("sha256")
                if expected_hash and _hash_fd(captured_fd, cancel) != expected_hash:
                    raise _error("The captured file differs from the approved content.", "file_changed")
                if item.get("rule_content_sha512") and _hash_fd(captured_fd, cancel, "sha512") != item["rule_content_sha512"]:
                    raise _error("The captured cache differs from its approved content proof.", "file_changed")
                if keeper is not None:
                    _validate_live(keeper, cancel)
                native_receipt = _native_trash(native, stage_path, native_receipt_path)
                # Preserve the reported destination even if subsequent identity or
                # content verification fails. It is not yet an approved restore handle.
                operation["native_result"] = native_receipt
                _save(store, receipt)
                trash_path, identity = _check_trash_result(native_receipt, item)
                operation.update(status="trashed", trash_path=trash_path, trash_identity=identity, completed_at=utc_now())
                _save(store, receipt)
            except KeyboardInterrupt:
                if operation["status"] in {"pending", "captured"}:
                    operation["status"] = "ambiguous"
                    operation["error"] = {"code": "interrupted", "message": "Interrupted during an operation; inspect the recovery receipt before continuing."}
                _save(store, receipt)
                return _finish(store, receipt, cancelled=True)
            except (CleanMacError, OSError, subprocess.SubprocessError) as error:
                operation["error"] = _exception(error)
                if operation.get("native_result", {}).get("status") == "trashed" and getattr(error, "errno", None) in {errno.EACCES, errno.EPERM}:
                    operation["status"] = "verify-denied"
                    operation["unverified_trash_path"] = operation["native_result"].get("trash_path")
                    operation["error"]["code"] = "trash_verification_denied"
                    operation["error"]["message"] = "The helper reported moving this file to Trash, but access was denied during verification. Preserve this receipt and review permissions before recovery."
                    _save(store, receipt)
                    return _finish(store, receipt)
                captured = False
                if stage_fd is not None:
                    try:
                        value = os.stat(Path(item["path"]).name, dir_fd=stage_fd, follow_symlinks=False)
                        captured = True
                    except FileNotFoundError:
                        pass
                if captured and source_fd is not None:
                    operation["status"] = "failed" if _rollback_capture(operation, source_fd, stage_fd) else "ambiguous"
                elif operation["status"] in {"pending", "captured"}:
                    # If capture never occurred the original identity is still present.
                    try:
                        _validate_live(item)
                        operation["status"] = "failed"
                    except (CleanMacError, OSError):
                        operation["status"] = "ambiguous"
                elif getattr(error, "code", None) == "cancelled" and operation["status"] == "not_started":
                    operation["status"] = "not_started"
                else:
                    operation["status"] = "skipped"
                _save(store, receipt)
                if getattr(error, "code", None) == "cancelled":
                    return _finish(store, receipt, cancelled=True)
                if operation["status"] == "ambiguous":
                    return _finish(store, receipt)
            finally:
                for fd in (captured_fd, stage_fd, source_fd):
                    if fd is not None:
                        os.close(fd)
        return _finish(store, receipt)


def _validate_receipt(receipt, plan=None):
    from .plans import validate_plan
    if not isinstance(receipt, dict) or receipt.get("schema_version") != 1 or receipt.get("action") != "trash":
        raise _error("Not a supported cleanup receipt.", "invalid_receipt")
    if not isinstance(receipt.get("operations"), list) or not receipt.get("id"):
        raise _error("Receipt is missing its operation records.", "invalid_receipt")
    bound_plan = receipt.get("plan_snapshot")
    try:
        validate_plan(bound_plan)
    except (CleanMacError, TypeError, KeyError) as error:
        raise _error("Receipt has no valid bound plan snapshot.", "invalid_receipt") from error
    expected_id = "receipt-" + digest({"plan_id": bound_plan["id"], "digest": bound_plan["digest"]})[:32]
    if (receipt["id"] != expected_id or receipt.get("plan_id") != bound_plan["id"]
            or receipt.get("plan_digest") != bound_plan["digest"]
            or (plan is not None and bound_plan != plan)):
        raise _error("Saved receipt does not match the approved plan.", "state_conflict")
    if len(receipt["operations"]) != len(bound_plan["items"]):
        raise _error("Receipt operations differ from the approved selection.", "invalid_receipt")
    seen_operations = set()
    for operation, selected in zip(receipt["operations"], bound_plan["items"]):
        if not isinstance(operation, dict) or operation.get("status") not in (_TERMINAL | _UNCERTAIN | {"not_started"}):
            raise _error("Receipt contains an invalid operation.", "invalid_receipt")
        item = operation.get("item", {})
        if item != selected or operation.get("id") in seen_operations or not re.fullmatch(r"op-[a-f0-9]{32}", str(operation.get("id", ""))):
            raise _error("Receipt operation differs from the approved selection.", "invalid_receipt")
        seen_operations.add(operation["id"])
        if operation.get("original_path") != item.get("path") or operation.get("item_id") != item.get("id"):
            raise _error("Receipt target identity is inconsistent.", "invalid_receipt")
        recorded_trash_identity = operation.get("trash_identity")
        expected_trash_identity = {key: item[key] for key in ("device", "inode", "logical_size", "mtime_ns", "mode", "nlink")}
        if recorded_trash_identity is not None and recorded_trash_identity != expected_trash_identity:
            raise _error("Receipt recovery identity differs from its approved item.", "invalid_receipt")
        _absolute(operation["original_path"])


def restore_receipt(receipt, *, store, native_path=None, cancel=None):
    """Restore verified Trash files without overwriting any destination.

    Caller owns the explicit receipt-ID approval interaction. A pending restore
    is never automatically repeated. No helper is needed for descriptor moves.
    """
    _validate_receipt(receipt)
    _rename_function()
    with store.lock("mutation"):
        saved = _existing_receipt(store, receipt["id"])
        if saved is None:
            raise _error("The cleanup receipt is not present in this state directory.", "invalid_receipt")
        _validate_receipt(saved)
        if saved.get("plan_snapshot") != receipt.get("plan_snapshot"):
            raise _error("The selected recovery receipt changed.", "state_conflict")
        receipt = copy.deepcopy(saved)
        for operation in receipt["operations"]:
            if operation["status"] in _UNCERTAIN:
                return _finish(store, receipt)
            if operation["status"] not in {"trashed", "conflict"}:
                continue
            if cancel and cancel():
                return _finish(store, receipt, cancelled=True)
            item = operation["item"]
            destination_fd = trash_fd = stage_fd = file_fd = None
            try:
                destination_fd = _open_directory(os.fspath(Path(item["path"]).parent), item["ancestors"])
                original_name = Path(item["path"]).name
                try:
                    os.stat(original_name, dir_fd=destination_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise _error("A file now occupies the original path; it was not overwritten.", "restore_conflict")
                trash_path = _absolute(operation.get("trash_path"))
                trash_fd = _open_directory(os.fspath(Path(trash_path).parent))
                file_fd = os.open(Path(trash_path).name, _FILE_FLAGS, dir_fd=trash_fd)
                identity = item  # Never let a mutable recovery handle select another inode.
                if not _matches(os.fstat(file_fd), identity):
                    raise _error("The Trash item changed; recovery needs manual review.", "file_changed")
                expected_hash = item.get("content_sha256") or item.get("sha256")
                if expected_hash and _hash_fd(file_fd, cancel) != expected_hash:
                    raise _error("The Trash item's content changed.", "file_changed")
                if item.get("rule_content_sha512") and _hash_fd(file_fd, cancel, "sha512") != item["rule_content_sha512"]:
                    raise _error("The cached content in Trash changed.", "file_changed")
                stage_path = Path(_absolute(operation["stage_path"]))
                expected_stage, expected_receipt = _stage_paths(store, operation)
                if stage_path != expected_stage:
                    raise _error("Receipt staging path is inconsistent.", "invalid_receipt")
                stage_fd = _open_directory(os.fspath(stage_path.parent), private=True)
                if os.fstat(destination_fd).st_dev != os.fstat(trash_fd).st_dev or os.fstat(stage_fd).st_dev != os.fstat(trash_fd).st_dev:
                    raise _error("Recovery cannot move across volumes.", "cross_device_unavailable")
                operation["status"] = "restore_pending"
                _save(store, receipt)
                _rename_exclusive(trash_fd, Path(trash_path).name, stage_fd, stage_path.name)
                operation["status"] = "restore_captured"
                _save(store, receipt)
                captured = os.stat(stage_path.name, dir_fd=stage_fd, follow_symlinks=False)
                if not _matches(captured, identity):
                    raise _error("The Trash item changed during recovery capture.", "file_changed")
                if expected_hash and _hash_fd(file_fd, cancel) != expected_hash:
                    raise _error("The captured recovery content changed.", "file_changed")
                if item.get("rule_content_sha512") and _hash_fd(file_fd, cancel, "sha512") != item["rule_content_sha512"]:
                    raise _error("The captured cache recovery content changed.", "file_changed")
                # Reopen the original parent after capture to detect replaced ancestors.
                check_fd = _open_directory(os.fspath(Path(item["path"]).parent), item["ancestors"])
                os.close(check_fd)
                _rename_exclusive(stage_fd, stage_path.name, destination_fd, original_name)
                operation.update(status="restored", restored_at=utc_now(), restore_path=item["path"])
                operation.pop("error", None)
                operation.pop("recovery_path", None)
                _save(store, receipt)
            except KeyboardInterrupt:
                if operation["status"] in {"restore_pending", "restore_captured"}:
                    operation["status"] = "ambiguous"
                operation["error"] = {"code": "interrupted", "message": "Recovery interrupted; inspect the recorded locations before retrying."}
                _save(store, receipt)
                return _finish(store, receipt, cancelled=True)
            except (CleanMacError, OSError) as error:
                operation["error"] = _exception(error)
                was_captured = operation["status"] == "restore_captured"
                if was_captured and stage_fd is not None and trash_fd is not None:
                    try:
                        _rename_exclusive(stage_fd, Path(operation["stage_path"]).name, trash_fd, Path(operation["trash_path"]).name)
                        operation["status"] = "conflict"
                    except OSError as rollback_error:
                        operation["status"] = "ambiguous"
                        operation["recovery_path"] = operation["stage_path"]
                        operation["rollback_error"] = _exception(rollback_error)
                elif operation["status"] == "restore_pending":
                    operation["status"] = "ambiguous"
                else:
                    operation["status"] = "conflict"
                _save(store, receipt)
                if operation["status"] == "ambiguous":
                    return _finish(store, receipt)
            finally:
                for fd in (file_fd, stage_fd, trash_fd, destination_fd):
                    if fd is not None:
                        os.close(fd)
        return _finish(store, receipt)

"""Private, atomic JSON storage for CleanMac sessions and recovery records.

All filesystem operations below are relative to validated directory descriptors.
The store does not repair permissions, follow symlinks, or discard recovery data
as an incidental part of forgetting a scan.
"""

from __future__ import annotations

from contextlib import contextmanager, ExitStack
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from typing import Iterator
import uuid

from .common import CleanMacError


_KINDS = frozenset({"scans", "plans", "receipts"})
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_MAX_RECORD_BYTES = 256 * 1024 * 1024
_MAX_RECORDS = 10000
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_RECOVERY_KEYS = frozenset({
    "trash_path", "trash_url", "restore_handle", "restore_handles",
    "staging_path", "quarantine_path", "quarantine", "captured_path",
})


def _invalid(message: str, code: str = "state_unsafe_path", **details) -> CleanMacError:
    return CleanMacError(message, code=code, details=details or None)


def _name(value: str, label: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise _invalid(f"Invalid {label}; use letters, numbers, hyphens, and underscores.",
                       "state_invalid_record")
    return value


def _kind(value: str) -> str:
    if not isinstance(value, str) or value not in _KINDS:
        raise _invalid("Unknown state record kind.", "state_invalid_record")
    return value


def _private(info: os.stat_result, *, directory: bool) -> None:
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != os.getuid():
        raise _invalid("State must be owned by the current user and have the expected file type.")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise _invalid("State has group or other access; choose an owner-only state directory.")
    if not directory and info.st_nlink != 1:
        raise _invalid("Hard-linked state files are not supported.")


def _open_directory(path: Path, *, create: bool = False) -> int:
    """Open an absolute path without following any directory symlink."""
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        parts = path.parts[1:]
        if not parts:
            raise _invalid("The filesystem root cannot be a state directory.")
        for index, part in enumerate(parts):
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            try:
                info = os.fstat(child)
                # A shared sticky temporary parent is acceptable; an ordinary
                # directory writable by another user can redirect state paths.
                shared_writable = stat.S_IMODE(info.st_mode) & 0o022
                sticky = bool(info.st_mode & stat.S_ISVTX)
                if shared_writable and not (sticky and info.st_uid == 0):
                    raise _invalid("A state-directory ancestor is writable by another user.")
                if index == len(parts) - 1:
                    _private(info, directory=True)
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_child(parent: int, name: str, *, create: bool = False) -> int:
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    try:
        _private(os.fstat(descriptor), directory=True)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _serialize(value: dict) -> bytes:
    if not isinstance(value, dict):
        raise _invalid("A state record must be a JSON object.", "state_invalid_record")
    if type(value.get("schema_version", 1)) is not int or value.get("schema_version", 1) != 1:
        raise _invalid("Unsupported state schema version.", "state_invalid_record")
    record = dict(value)
    record.setdefault("schema_version", 1)
    try:
        data = (json.dumps(record, sort_keys=True, ensure_ascii=True,
                           allow_nan=False, separators=(",", ":")) + "\n").encode("ascii")
    except (TypeError, ValueError, RecursionError) as error:
        raise _invalid("State contains values that cannot be represented as JSON.",
                       "state_invalid_record") from error
    if len(data) > _MAX_RECORD_BYTES:
        raise _invalid("State record exceeds the supported size.", "state_invalid_record")
    return data


def _read(parent: int, filename: str) -> dict:
    # Nonblocking open lets the subsequent type check reject a substituted FIFO
    # without hanging before fstat can establish that it is not a regular file.
    descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        info = os.fstat(descriptor)
        _private(info, directory=False)
        if info.st_size > _MAX_RECORD_BYTES:
            raise _invalid("State record exceeds the supported size.", "state_corrupt")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            data = source.read(_MAX_RECORD_BYTES + 1)
        if len(data) > _MAX_RECORD_BYTES:
            raise _invalid("State record exceeds the supported size.", "state_corrupt")
        try:
            value = json.loads(data, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (ValueError, UnicodeDecodeError, RecursionError) as error:
            raise _invalid("State JSON is corrupt; its original file has been preserved.",
                           "state_corrupt") from error
        if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
                or value.get("schema_version") != 1):
            raise _invalid("Unsupported or missing state schema; original file preserved.", "state_corrupt")
        return value
    finally:
        os.close(descriptor)


def _write(parent: int, filename: str, data: bytes) -> None:
    # Validate an existing target before replacing it. Never replace a symlink,
    # hardlink, public file, or directory that happens to have a valid name.
    try:
        _private(os.stat(filename, dir_fd=parent, follow_symlinks=False), directory=False)
    except FileNotFoundError:
        pass
    temporary = f".pending-{uuid.uuid4().hex}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=parent)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as destination:
            destination.write(data)
            destination.flush()
            os.fsync(descriptor)
        os.replace(temporary, filename, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass


def _contains_recovery(value) -> bool:
    if isinstance(value, dict):
        return any((key in _RECOVERY_KEYS and bool(item)) or _contains_recovery(item)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_recovery(item) for item in value)
    return False


def _references_id(value, identifier: str) -> bool:
    if isinstance(value, dict):
        return any(_references_id(item, identifier) for item in value.values())
    if isinstance(value, list):
        return any(_references_id(item, identifier) for item in value)
    return isinstance(value, str) and value == identifier


def _receipt_requires_plan(record: dict) -> bool:
    if record.get("record_type") == "action_receipt":
        if record.get("execution_started") is False and record.get("status") in {"not_started", "failed"}:
            return _contains_recovery({key: value for key, value in record.items() if key != "plan_snapshot"})
        return True
    if _contains_recovery(record):
        return True
    operations = record.get("operations")
    if isinstance(operations, list) and operations:
        return (record.get("status") not in {"cancelled", "partial"}
                or any(not isinstance(operation, dict) or operation.get("status") != "not_started" for operation in operations))
    # An unrecognized referenced receipt is not evidence that nothing happened.
    return True


class StateStore:
    """Persist private JSON records; instantiate with a separate fixture directory in tests."""

    def __init__(self, data_dir=None):
        selected = data_dir if data_dir is not None else os.environ.get("CLEANMAC_DATA_DIR")
        if selected is None:
            base = os.environ.get("XDG_DATA_HOME")
            selected = Path(base).expanduser() / "b-skills" / "cleanmac" if base else (
                Path.home() / ".local" / "share" / "b-skills" / "cleanmac")
        if not isinstance(selected, (str, os.PathLike)) or not os.fspath(selected):
            raise _invalid("A state directory must be a nonempty filesystem path.")
        raw = os.fspath(selected)
        if not isinstance(raw, str) or "\x00" in raw:
            raise _invalid("The state directory path is invalid.")
        self.root = Path(os.path.abspath(os.path.expanduser(raw)))
        skill_root = Path(__file__).resolve().parents[2]
        if self.root == skill_root or skill_root in self.root.parents:
            raise _invalid("Keep runtime state outside the skill installation.")
        try:
            descriptor = _open_directory(self.root, create=True)
            try:
                info = os.fstat(descriptor)
                self._identity = (info.st_dev, info.st_ino)
                for kind in sorted(_KINDS):
                    child = _open_child(descriptor, kind, create=True)
                    os.close(child)
                child = _open_child(descriptor, ".locks", create=True)
                os.close(child)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise self._os_error(error) from error

    @staticmethod
    def _os_error(error: OSError) -> CleanMacError:
        if error.errno == errno.ENOENT:
            return _invalid("The requested state record or directory was not found.", "state_not_found")
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            return _invalid("State paths cannot contain symlinks or non-directory ancestors.")
        return _invalid("The private state operation failed.", "state_io_error", errno=error.errno)

    @contextmanager
    def _root_fd(self) -> Iterator[int]:
        descriptor = None
        try:
            descriptor = _open_directory(self.root)
            info = os.fstat(descriptor)
            if (info.st_dev, info.st_ino) != self._identity:
                raise _invalid("The state directory changed; reopen and inspect the session before continuing.")
            yield descriptor
        except OSError as error:
            raise self._os_error(error) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @contextmanager
    def _kind_fd(self, kind: str) -> Iterator[int]:
        _kind(kind)
        with self._root_fd() as root:
            descriptor = _open_child(root, kind)
            try:
                yield descriptor
            finally:
                os.close(descriptor)

    @contextmanager
    def lock(self, name: str, *, timeout: float = 5.0) -> Iterator[None]:
        """Acquire a process lock, failing with state_busy instead of waiting forever."""
        _name(name, "lock name")
        if (not isinstance(timeout, (int, float)) or not math.isfinite(timeout)
                or timeout < 0 or timeout > 60):
            raise _invalid("Lock timeout must be between zero and 60 seconds.", "state_invalid_record")
        with self._root_fd() as root:
            locks = _open_child(root, ".locks")
            descriptor = None
            acquired = False
            try:
                descriptor = os.open(name + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                                     0o600, dir_fd=locks)
                _private(os.fstat(descriptor), directory=False)
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                        break
                    except BlockingIOError as error:
                        if time.monotonic() >= deadline:
                            raise _invalid("Another CleanMac operation holds this session lock.",
                                           "state_busy", lock=name) from error
                        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
                yield
            finally:
                if descriptor is not None:
                    if acquired:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    os.close(descriptor)
                os.close(locks)

    def put(self, kind: str, record: dict) -> dict:
        _kind(kind)
        if not isinstance(record, dict):
            raise _invalid("A state record must be a JSON object.", "state_invalid_record")
        identifier = _name(record.get("id"), "record ID")
        data = _serialize(record)
        with self.lock("records"):
            with self._kind_fd(kind) as directory:
                _write(directory, identifier + ".json", data)
        return json.loads(data)

    def get(self, kind: str, identifier: str) -> dict:
        _kind(kind)
        _name(identifier, "record ID")
        with self._kind_fd(kind) as directory:
            record = _read(directory, identifier + ".json")
        if record.get("id") != identifier:
            raise _invalid("State record ID does not match its filename; original preserved.", "state_corrupt")
        return record

    def list(self, kind: str) -> list[dict]:
        _kind(kind)
        records = []
        with self.lock("records"):
            with self._kind_fd(kind) as directory:
                filenames = sorted(name for name in os.listdir(directory) if name.endswith(".json"))
                if len(filenames) > _MAX_RECORDS:
                    raise _invalid("Too many state records for a single history request.", "state_limit")
                for filename in filenames:
                    identifier = _name(filename[:-5], "record ID")
                    record = _read(directory, filename)
                    if record.get("id") != identifier:
                        raise _invalid("State record ID does not match its filename; original preserved.", "state_corrupt")
                    records.append(record)
        return sorted(records, key=lambda item: (str(item.get("created_at", "")), item["id"]), reverse=True)

    def delete(self, kind: str, identifier: str, *, discard_recovery: bool = False) -> None:
        _kind(kind)
        _name(identifier, "record ID")
        if not isinstance(discard_recovery, bool):
            raise _invalid("Recovery disposal must be an explicit Boolean choice.", "state_invalid_record")
        observed = self.get(kind, identifier)
        locks = []
        record_type = observed.get("record_type")
        if kind in {"plans", "receipts"}:
            plan_id = identifier if kind == "plans" else observed.get("plan_id", "")
            action = re.fullmatch(r"plan-action-([0-9a-f]{32})", str(plan_id))
            if action:
                locks.append(("action-" + action.group(1), 5.0))
            # Native image actions nest action -> mutation; never invert that.
            locks.append(("mutation", 5.0))
        elif kind == "scans" and record_type == "oauth_session":
            session = re.fullmatch(r"oauth-session-([0-9a-f]{32})", identifier)
            if session is None:
                raise _invalid("OAuth session identity is invalid; retain the record.", "oauth_session_retention")
            locks.extend([("worker-" + session.group(1), 0.0), (identifier, 5.0)])
        elif kind == "scans" and record_type == "oauth_connection":
            connection = re.fullmatch(r"oauth-connection-([0-9a-f]{32})", identifier)
            if connection is None:
                raise _invalid("OAuth connection identity is invalid; retain the record.", "oauth_account_retention")
            locks.append(("oauth-" + connection.group(1), 5.0))

        with ExitStack() as stack:
            for name, timeout in locks:
                try:
                    stack.enter_context(self.lock(name, timeout=timeout))
                except CleanMacError as error:
                    if name.startswith("worker-") and error.code == "state_busy":
                        raise _invalid("The consent worker is still running. Use tool accounts --action cancel --record SESSION_ID, then wait for it to stop before forgetting this session.",
                                       "oauth_session_retention", session_id=identifier) from None
                    raise
            current = self.get(kind, identifier)
            if current.get("record_type") != record_type:
                raise _invalid("The record changed while preparing deletion; inspect it again.", "state_busy")
            if kind == "scans" and record_type == "oauth_session":
                if current.get("status") not in {"cancelled", "expired", "denied", "failed", "connected"}:
                    raise _invalid("This consent session is still active. Use tool accounts --action cancel --record SESSION_ID, or check its status after expiry, before forgetting it.",
                                   "oauth_session_retention", session_id=identifier)
            elif kind == "scans" and record_type == "oauth_connection":
                if current.get("status") != "disconnected":
                    raise _invalid("This account may still have stored credentials. Use tool accounts --action disconnect-plan --record CONNECTION_ID and apply its reviewed plan before forgetting the connection.",
                                   "oauth_account_retention", connection_id=identifier)
                from .oauth import Keychain
                if Keychain().contains(identifier):
                    raise _invalid("Account credentials still exist in Keychain. Complete the accounts disconnect workflow before forgetting this connection.",
                                   "oauth_account_retention", connection_id=identifier)
            with self.lock("records"):
                with self._kind_fd(kind) as directory:
                    record = _read(directory, identifier + ".json")
                    if record != current or record.get("id") != identifier:
                        raise _invalid("The record changed while preparing deletion; inspect it again.", "state_busy")
                    if kind == "plans":
                        with self._kind_fd("receipts") as receipts:
                            filenames = [name for name in os.listdir(receipts) if name.endswith(".json")]
                            if len(filenames) > _MAX_RECORDS:
                                raise _invalid("Too many receipts to safely check plan retention.", "state_limit")
                            for filename in filenames:
                                receipt_id = _name(filename[:-5], "receipt ID")
                                receipt = _read(receipts, filename)
                                if receipt.get("id") != receipt_id:
                                    raise _invalid("Receipt identity does not match its filename; original preserved.", "state_corrupt")
                                if _references_id(receipt, identifier) and _receipt_requires_plan(receipt):
                                    raise _invalid("This plan is referenced by a started or recovery-bearing receipt. Inspect and retain that receipt and plan together for recovery and audit.",
                                                   "plan_retention", plan_id=identifier, receipt_id=receipt_id)
                    if kind == "receipts" and ((record_type == "action_receipt" and _receipt_requires_plan(record))
                            or (record_type != "action_receipt" and _contains_recovery(record) and not discard_recovery)):
                        raise _invalid("This receipt contains recovery or action audit records. Retain it so recovery and repeated-action checks remain available.",
                                       "recovery_retention", receipt_id=identifier)
                    os.unlink(identifier + ".json", dir_fd=directory)
                    os.fsync(directory)

    def get_settings(self) -> dict:
        with self._root_fd() as root:
            try:
                return _read(root, "settings.json")
            except FileNotFoundError:
                return {"schema_version": 1}

    def save_settings(self, settings: dict) -> dict:
        data = _serialize(settings)
        with self.lock("settings"):
            with self._root_fd() as root:
                _write(root, "settings.json", data)
        return json.loads(data)

    def update_settings(self, mutate) -> dict:
        """Atomically read, change and save shared settings without lost updates.

        The synchronous callback receives a fresh JSON object and may modify it
        in place, returning None, or return a replacement object. It must not
        call StateStore methods, perform external work or acquire another lock.
        Exceptions and invalid results leave the previous settings untouched.
        """
        if not callable(mutate):
            raise _invalid("A settings update requires a callback.", "state_invalid_record")
        with self.lock("settings"):
            with self._root_fd() as root:
                try:
                    current = _read(root, "settings.json")
                except FileNotFoundError:
                    current = {"schema_version": 1}
                result = mutate(current)
                data = _serialize(current if result is None else result)
                _write(root, "settings.json", data)
        return json.loads(data)

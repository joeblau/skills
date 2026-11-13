"""Private, atomic JSON storage for CleanMac sessions and recovery records.

All filesystem operations below are relative to validated directory descriptors.
The store does not repair permissions, follow symlinks, or discard recovery data
as an incidental part of forgetting a scan.
"""

from __future__ import annotations

from contextlib import contextmanager
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
        with self.lock("records"):
            with self._kind_fd(kind) as directory:
                record = _read(directory, identifier + ".json")
                if record.get("id") != identifier:
                    raise _invalid("State record ID does not match its filename; original preserved.", "state_corrupt")
                if kind == "receipts" and _contains_recovery(record) and not discard_recovery:
                    raise _invalid("This receipt contains recovery locations. Retain it, or explicitly discard recovery history.",
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

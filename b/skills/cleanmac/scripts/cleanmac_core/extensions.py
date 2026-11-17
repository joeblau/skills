"""Discover bundled tools without probing accounts, folders, or native permissions.

Imports are limited to this package's fixed module list. Tool configuration is
bounded JSON, never executable code; credentials are references, not values.
"""
from __future__ import annotations

import importlib
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any

from .common import CleanMacError


MODULES = (
    "cleanup_tools", "native_tools", "maintenance_tools", "malware_tools",
    "browser_tools", "app_tools", "cloud_tools", "mail_tools",
    "monitoring_tools", "lifecycle_tools", "setup_tools", "accounts_tools", "trash_tools", "file_tools",
)
MAX_CONFIG_BYTES = 65536
MAX_CONFIG_NODES = 4096
MAX_CONFIG_DEPTH = 12
MAX_SELECTIONS = 10000
_NAME = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_FEATURE = re.compile(r"CM-[0-9]{3}\Z")
_SECRET_KEYS = {
    "token", "accesstoken", "refreshtoken", "idtoken", "password", "passwd",
    "clientsecret", "apikey", "apitoken", "authorization", "credential",
    "credentials", "secret", "secretkey", "privatekey", "bearer",
}


def _configuration_error(message: str = "Provide a JSON object with bounded tool configuration.") -> CleanMacError:
    # Never include raw JSON, filenames, decoder excerpts, or values here.
    return CleanMacError(message, code="invalid_tool_configuration")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _configuration_error("Configuration contains a duplicate key.")
        result[key] = value
    return result


def _is_credential_reference(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("source") == "environment":
        return set(value) == {"source", "name"} and isinstance(value.get("name"), str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", value["name"]) is not None
    if value.get("source") == "keychain":
        return {"source", "service"} <= set(value) <= {"source", "service", "account"} and all(isinstance(child, str) and child and len(child) <= 256 and "\x00" not in child for child in value.values())
    return False


def validate_configuration(value: Any) -> dict:
    if not isinstance(value, dict):
        raise _configuration_error()
    nodes = 0

    def walk(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if depth > MAX_CONFIG_DEPTH or nodes > MAX_CONFIG_NODES:
            raise _configuration_error("Configuration exceeds the supported size or nesting limit.")
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or not key or len(key) > 128 or "\x00" in key:
                    raise _configuration_error("Configuration contains an invalid field name.")
                normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
                if normalized in _SECRET_KEYS and not (normalized == "credential" and _is_credential_reference(child)):
                    raise _configuration_error("Do not pass credentials in configuration. Use the tool's environment-variable or Keychain reference fields.")
                walk(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                walk(child, depth + 1)
        elif isinstance(item, str):
            if len(item) > MAX_CONFIG_BYTES or "\x00" in item:
                raise _configuration_error("Configuration contains an invalid string value.")
        elif item is None or isinstance(item, bool):
            return
        elif isinstance(item, int):
            if item.bit_length() > 64:
                raise _configuration_error("Configuration contains an out-of-range number.")
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise _configuration_error("Configuration numbers must be finite.")
        else:
            raise _configuration_error()

    walk(value, 0)
    try:
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise _configuration_error() from None
    if len(encoded.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise _configuration_error("Configuration exceeds 64 KiB.")
    return value


def parse_configuration(raw: str | None = None, *, path: str | None = None) -> dict:
    if raw is not None and path is not None:
        raise _configuration_error("Choose inline configuration or a configuration file, not both.")
    if path is not None:
        from .permissions import _open_directory
        try:
            location = Path(os.path.abspath(os.path.expanduser(path)))
            parent_fd = _open_directory(str(location.parent))
            try:
                fd = os.open(location.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
            finally:
                os.close(parent_fd)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
                    raise _configuration_error("The configuration file must be an owner-only regular file without symbolic or hard links.")
                if info.st_size > MAX_CONFIG_BYTES:
                    raise _configuration_error("Configuration exceeds 64 KiB.")
                chunks = bytearray()
                while len(chunks) <= MAX_CONFIG_BYTES:
                    chunk = os.read(fd, min(8192, MAX_CONFIG_BYTES + 1 - len(chunks)))
                    if not chunk:
                        break
                    chunks.extend(chunk)
                raw = chunks.decode("utf-8")
            finally:
                os.close(fd)
        except CleanMacError:
            raise
        except (OSError, ValueError, UnicodeError):
            raise _configuration_error("The private configuration file could not be read; use a real path with owner-only permissions.") from None
    if raw is None:
        return {}
    try:
        encoded_size = len(raw.encode("utf-8")) if isinstance(raw, str) else MAX_CONFIG_BYTES + 1
    except UnicodeError:
        raise _configuration_error("Configuration is not valid UTF-8 text.") from None
    if encoded_size > MAX_CONFIG_BYTES:
        raise _configuration_error("Configuration exceeds 64 KiB.")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(_configuration_error("Configuration numbers must be finite.")))
    except CleanMacError:
        raise
    except (ValueError, TypeError, RecursionError):
        raise _configuration_error("Configuration is not a valid JSON object.") from None
    return validate_configuration(value)


def normalize_options(*, roots=None, selected=None, record_id=None, approve=None, configuration=None) -> dict:
    def strings(values, limit, maximum, label):
        if not isinstance(values, (list, tuple)) or len(values) > limit:
            raise CleanMacError(f"Too many or invalid {label}.", code="invalid_tool_options")
        result = []
        for value in values:
            if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
                raise CleanMacError(f"Provide valid {label}.", code="invalid_tool_options")
            if value not in result:
                result.append(value)
        return result

    paths = strings([] if roots is None else roots, 128, 32768, "selected roots")
    # Roots can be opaque remote folder IDs. Local adapters resolve filesystem
    # paths themselves; the shared CLI must not silently rewrite remote scope.
    ids = strings([] if selected is None else selected, MAX_SELECTIONS, 4096, "selected IDs")
    for value in (record_id, approve):
        if value is not None and (not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value):
            raise CleanMacError("Provide a valid saved record and approval identifier.", code="invalid_tool_options")
    return {"roots": paths, "selected": ids, "record_id": record_id, "approve": approve,
            "configuration": validate_configuration({} if configuration is None else configuration)}


def _registry():
    entries = {}
    failures = []
    collisions = set()
    for module_name in MODULES:
        try:
            module = importlib.import_module("." + module_name, package=__package__)
            specs = module.TOOL_SPECS
            if not isinstance(specs, (list, tuple)) or not callable(module.dispatch):
                raise ValueError("invalid tool metadata")
            module_entries = []
            for raw in specs:
                if not isinstance(raw, dict):
                    raise ValueError("invalid tool metadata")
                spec = dict(raw)
                name, title = spec.get("name"), spec.get("title")
                actions, features = spec.get("actions"), spec.get("feature_ids")
                if not isinstance(name, str) or not _NAME.fullmatch(name) or not isinstance(title, str) or not title or len(title) > 256:
                    raise ValueError("invalid tool metadata")
                if not isinstance(actions, (list, tuple)) or not actions or any(not isinstance(a, str) or not _NAME.fullmatch(a) for a in actions):
                    raise ValueError("invalid action metadata")
                if not isinstance(features, (list, tuple)) or any(not isinstance(f, str) or not _FEATURE.fullmatch(f) for f in features):
                    raise ValueError("invalid feature metadata")
                encoded = json.dumps(spec, ensure_ascii=True, allow_nan=False)
                if len(encoded) > MAX_CONFIG_BYTES:
                    raise ValueError("oversized tool metadata")
                spec.update(module=module_name, implementation="installed", dependency_status="not-checked",
                            actions=list(dict.fromkeys(actions)), feature_ids=list(dict.fromkeys(features)))
                approvals = spec.get("approval_actions", ["apply"] if "apply" in actions else [])
                if not isinstance(approvals, (list, tuple)) or any(action not in actions for action in approvals):
                    raise ValueError("invalid approval metadata")
                spec["approval_actions"] = list(dict.fromkeys(approvals))
                spec.setdefault("usage", f"cleanmac tool {name} --action ACTION [--root PATH] [--select ID] [--record ID] [--approve DIGEST] [--config JSON]")
                module_entries.append((name, spec, module))
            for name, spec, module in module_entries:
                if name in entries or name in collisions:
                    entries.pop(name, None)
                    collisions.add(name)
                    failures.append({"module": module_name, "reason": "duplicate-tool-name", "tool": name})
                else:
                    entries[name] = (spec, module)
        except Exception as error:
            failures.append({"module": module_name, "reason": "module-unavailable",
                             "error_type": type(error).__name__})
    return entries, failures


def catalog(name: str | None = None) -> dict:
    entries, failures = _registry()
    if name is not None:
        if name not in entries:
            raise CleanMacError("This tool is not installed or its registration is unavailable. Run cleanmac tools to see available registrations.", code="tool_unavailable")
        records = [entries[name][0]]
    else:
        records = [entries[key][0] for key in sorted(entries)]
    return {"schema_version": 1, "status": "partial" if failures else "complete", "tools": records,
            "unavailable_modules": failures, "dependency_status": "not-checked",
            "message": "This lists installed tool implementations. Each invocation checks its required access, configuration, native helpers, and provider dependencies; no scan or permission request has run."}


def list_tools() -> dict:
    return catalog()


def dispatch_tool(name: str, action: str, options: dict, *, store, cancel=None, progress=None) -> dict:
    entries, _ = _registry()
    if name not in entries:
        raise CleanMacError("This tool is not installed or its registration is unavailable.", code="tool_unavailable")
    spec, module = entries[name]
    if action not in spec["actions"]:
        raise CleanMacError("This action is not supported by the selected tool. Inspect cleanmac tools TOOL for its actions.",
                            code="unsupported_tool_action", details={"actions": spec["actions"]})
    options = normalize_options(**options)
    if action in spec.get("approval_actions", ["apply"]) and (not options["record_id"] or not options["approve"]):
        raise CleanMacError("Apply requires an exact saved plan and its approval digest.", code="approval_required")
    if cancel and cancel():
        return {"schema_version": 1, "tool": name, "action": action, "status": "cancelled",
                "message": "Stopped before the tool began."}
    try:
        result = module.dispatch(name, action, options, store=store, cancel=cancel, progress=progress)
    except CleanMacError:
        raise
    except Exception as error:
        raise CleanMacError("The tool could not complete its action. Inspect history before retrying an interrupted mutation.",
                            code="tool_failed", details={"error_type": type(error).__name__}) from None
    if not isinstance(result, dict):
        raise CleanMacError("The tool did not return a structured result; inspect history before retrying an action.", code="invalid_tool_result")
    result.setdefault("tool", name)
    result.setdefault("schema_version", 1)
    return result

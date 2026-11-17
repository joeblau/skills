"""Explicit saved scopes and opt-in, read-only launchd scan schedules.

Persisted preferences are not action approvals. The scheduled runner accepts only
stored local scan settings and cannot dispatch cleanup, account, or shell actions.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys
import time
from typing import Any

from .common import CleanMacError, digest, display_path, new_id, utc_now
from .inventory import _canonical_root, _open_parent, _directory_flags


TOOL_SPECS = [
    {"name": "saved-scopes", "title": "Saved scan scopes", "feature_ids": ["CM-010"],
     "actions": ["inspect", "save", "remove", "run"], "scope": "Named local roots, filters, and scan areas; never saved cleanup approval."},
    {"name": "favorites", "title": "Favorite tools", "feature_ids": ["CM-010"],
     "actions": ["inspect", "add", "remove"], "scope": "Private preferred tool names; availability is checked when a tool runs."},
    {"name": "schedules", "title": "Scheduled local scans", "feature_ids": ["CM-076"],
     "actions": ["inspect", "plan", "apply", "run"], "scope": "Explicitly installed user LaunchAgents run selected read-only scans and optional measured alerts."},
    {"name": "smart-care", "title": "Smart Care", "feature_ids": ["CM-009"],
     "actions": ["inspect", "review"], "scope": "Shared local inventory with per-check outcomes and no automatic action selection."},
]
TOOL_SPECS[0].update(
    description="Save or replace an explicitly chosen local scan scope. Saving does not scan, select files, or approve cleanup. Run checks the saved directory identities before scanning.",
    configuration={"name": "Required for save/run/remove: 1–64 letters, digits, hyphens, underscores.",
                   "areas": "Save only: array drawn from files,large,duplicates,caches,storage; default files,large,storage.",
                   "search": "Save only: optional remembered literal review filter; does not widen scan scope.",
                   "max_items": "Save only: 1–1000000; default 100000.",
                   "min_size": "Save only: nonnegative large-file threshold in bytes; default 52428800.",
                   "duplicate_min_size": "Save only: minimum duplicate size in bytes; default 1."},
    examples=["tool saved-scopes --action save --root ~/Downloads --config '{\"name\":\"Downloads\",\"areas\":[\"files\",\"duplicates\"]}'",
              "tool saved-scopes --action run --config '{\"name\":\"Downloads\"}'",
              "tool saved-scopes --action remove --config '{\"name\":\"Downloads\"}'"],
    requirements=["Explicit local roots for save; run accepts the preset name only, with no scope overrides."],
)
TOOL_SPECS[1].update(
    description="Remember tool names for discovery. Favorites do not enable unavailable capabilities or save approvals.",
    configuration={"name": "Required for add/remove: a tool name from `tools`, for example smart-care, memory, or app-updates."},
    examples=["tool favorites --action add --config '{\"name\":\"smart-care\"}'", "tool favorites --action inspect"],
)
TOOL_SPECS[2].update(
    description="Plan an opt-in local read-only schedule, review its exact roots/policies, then apply that plan ID and digest. Inspect is read-only. Cancelling before apply leaves no LaunchAgent. Disable/remove also require a separate reviewed plan.",
    configuration={"operation": "plan only: create(default), enable, disable, or remove. For existing schedules supply --record SCHEDULE_ID; apply uses --record PLAN_ID --approve DIGEST.",
                   "name": "Create only: short schedule name. Choose roots with --root or an exact saved preset with preset.",
                   "preset": "Create only: saved-scopes name. Copies its reviewed roots/areas/limits into the plan; cannot be combined with --root or scan-setting overrides. Later preset edits never change this schedule.",
                   "areas": "Create only: files,large,duplicates,caches,storage; default files,large,storage.",
                   "interval_seconds": "Create only: 900–2678400; default 86400. RunAtLoad is false.",
                   "quiet_hours": "Create only: optional [start_hour,end_hour] in local 24h time; midnight crossing supported; equal hours means no quiet period.",
                   "require_ac_power": "Create only: true(default) skips when AC power cannot be verified.",
                   "max_load_per_cpu": "Create only: 0.1–4; default 1.0. A one-minute load policy, not CPU percentage.",
                   "notify": "Create only: false(default). True requests generic notifications only when measured thresholds are met.",
                   "low_space_bytes": "Create only: free-space threshold for each selected volume; default 10737418240.",
                   "alert_repeat_seconds": "Create only: 3600–2678400; default 86400. Identical alerts are suppressed within this interval.",
                   "checks": "Create only: optional objects memory:{limit}, battery:{sample_seconds}, updates:{brew_path}, malware:{engine_path,mode,targets}. Explicit local engine/provider dependencies are checked every run; no updates, quarantine, or account access are automated.",
                   "battery_charge_percent": "Create only: 1–99; default 20. Alert needs a measured charge at/below threshold while disconnected from external power.",
                   "max_items": "Create only: 1–1000000; default 100000.",
                   "min_size": "Create only: large-file threshold in bytes.",
                   "duplicate_min_size": "Create only: minimum duplicate size in bytes.",
                   "search": "Create only: optional remembered review filter."},
    examples=["tool schedules --action plan --root ~/Downloads --config '{\"operation\":\"create\",\"name\":\"weekly\",\"interval_seconds\":604800,\"notify\":false}'",
              "tool schedules --action plan --config '{\"name\":\"weekly\",\"preset\":\"Downloads\",\"interval_seconds\":604800}'",
              "tool schedules --action apply --record PLAN_ID --approve DIGEST",
              "tool schedules --action plan --record SCHEDULE_ID --config '{\"operation\":\"disable\"}'",
              "tool schedules --action plan --record SCHEDULE_ID --config '{\"operation\":\"remove\"}'"],
    requirements=["macOS GUI login session, accessible selected local folders, current-user LaunchAgents directory, Python 3.10+.",
                  "System Settings may independently control background execution and notifications. The skill does not grant those permissions."],
)
TOOL_SPECS[3].update(
    description="Run selected local checks once, keep separate check outcomes, then use the saved scan ID with review and plan. Optional dedicated checks run only when explicitly configured; missing checks remain unscanned.",
    configuration={"modules": "Array of files,large,duplicates,caches,storage,malware,updates,health,maintenance; default storage,large,duplicates,caches.",
                   "checks": "Opt-in configuration for selected extra modules: malware:{engine_path,mode,targets}, updates:{brew_path}, health:{sample_seconds,limit}, maintenance:{tool:memory-diagnosis|purgeable|periodic|disk-permissions}.",
                   "max_items": "1–1000000; default 100000.", "min_size": "Large-file minimum bytes.",
                   "duplicate_min_size": "Duplicate minimum bytes.", "search": "Remembered literal review filter."},
    examples=["tool smart-care --action inspect --root ~/Downloads --config '{\"modules\":[\"storage\",\"duplicates\",\"caches\"]}'",
              "tool smart-care --action review --record SCAN_ID"],
)
SOURCES = [
    "https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html",
    "https://github.com/apple-oss-distributions/launchd/blob/main/man/launchd.plist.5",
]
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_AREAS = {"files", "large", "duplicates", "caches", "storage"}
_LABEL_PREFIX = "com.b-skills.cleanmac.scan."
_MAX_PLIST = 128 * 1024


def _error(message, code="invalid_configuration"):
    return CleanMacError(message, code=code)


def _name(value, label="name"):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise _error(f"Use 1–64 letters, digits, hyphens, or underscores for the {label}.")
    return value


def _integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise _error(f"{label} must be an integer from {minimum} to {maximum}.")
    return value


def _boolean(value, label):
    if type(value) is not bool:
        raise _error(f"{label} must be true or false.")
    return value


def _configuration(options):
    value = options.get("configuration", {})
    if not isinstance(value, dict):
        raise _error("configuration must be an object.")
    return value


def _check_keys(config, allowed):
    unknown = sorted(set(config) - set(allowed))
    if unknown:
        raise _error("Unsupported configuration fields: " + ", ".join(unknown))


def _settings_change(store, change):
    """Use the shared atomic settings update without nesting a lock."""
    result = []
    def update(settings):
        result.append(change(settings))
    store.update_settings(update)
    return result[0]


def _lifecycle(settings):
    value = settings.setdefault("lifecycle", {"presets": {}, "favorites": [], "schedules": {}})
    if (not isinstance(value, dict) or not isinstance(value.get("presets"), dict)
            or not isinstance(value.get("favorites"), list) or not isinstance(value.get("schedules"), dict)):
        raise _error("Saved lifecycle settings have an unsupported schema.", "state_corrupt")
    return value


def _snapshot(store):
    return _lifecycle(store.get_settings())


def _root_identity(raw):
    path = _canonical_root(raw)
    if path == os.sep:
        raise _error("Choose specific local folders instead of the filesystem root.", "invalid_scope")
    try:
        parent, ancestors = _open_parent(path)
    except OSError as error:
        raise CleanMacError("A selected root's ancestry could not be opened without following links.",
                            code="scope_unavailable", details={"path": path, "errno": error.errno}) from error
    fd = None
    try:
        fd = os.open(os.path.basename(path), _directory_flags(), dir_fd=parent)
        value = os.fstat(fd)
        # A preset must not silently become a cloud account or an external volume.
        if getattr(value, "st_flags", 0) & getattr(stat, "SF_DATALESS", 0x40000000):
            raise _error("Cloud placeholders cannot be saved as local scan roots.", "invalid_scope")
        parts = {part.casefold() for part in Path(path).parts}
        if parts.intersection({"cloudstorage", "mobile documents"}):
            raise _error("Use the cloud adapter for provider-managed locations.", "invalid_scope")
        return {"path": path, "device": value.st_dev, "inode": value.st_ino,
                "ancestors": [{k: item[k] for k in ("path", "device", "inode")} for item in ancestors]}
    except OSError as error:
        raise CleanMacError("A selected scan root could not be opened without following links.",
                            code="scope_unavailable", details={"path": path, "errno": error.errno}) from error
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def _scan_config(options, config):
    roots = options.get("roots", [])
    if not isinstance(roots, list) or not roots or len(roots) > 32:
        raise _error("Choose 1–32 explicit local roots.", "invalid_scope")
    identities = []
    seen = set()
    for root in roots:
        identity = _root_identity(root)
        if identity["path"] not in seen:
            seen.add(identity["path"])
            identities.append(identity)
    areas = config.get("areas", ["files", "large", "storage"])
    if not isinstance(areas, list) or not areas or any(not isinstance(x, str) or x not in _AREAS for x in areas):
        raise _error("areas must be a nonempty array of supported local scan areas.")
    search = config.get("search")
    if search is not None and (not isinstance(search, str) or len(search) > 256 or "\x00" in search):
        raise _error("search must be a short string.")
    return {"roots": identities, "areas": sorted(set(areas)),
            "max_items": _integer(config.get("max_items", 100000), 1, 1000000, "max_items"),
            "min_size": _integer(config.get("min_size", 50 * 1024 * 1024), 0, 2**63 - 1, "min_size"),
            "duplicate_min_size": _integer(config.get("duplicate_min_size", 1), 1, 2**63 - 1, "duplicate_min_size"),
            "filters": {"search": search}, "approval": None}


def _validate_scope(config):
    if (not isinstance(config, dict) or not isinstance(config.get("roots"), list)
            or not 1 <= len(config["roots"]) <= 32):
        raise _error("Stored scan scope is invalid.", "state_corrupt")
    for expected in config["roots"]:
        if not isinstance(expected, dict) or _root_identity(expected.get("path")) != expected:
            raise _error("A saved root or its ancestry changed. Review and save the scope again.", "stale_scope")


def _validate_saved_scan(config):
    if not isinstance(config, dict):
        raise _error("Stored scan settings are invalid.", "state_corrupt")
    areas = config.get("areas")
    if not isinstance(areas, list) or not areas or any(not isinstance(value, str) or value not in _AREAS for value in areas):
        raise _error("Stored scan areas are unsupported.", "state_corrupt")
    _integer(config.get("max_items"), 1, 1000000, "max_items")
    _integer(config.get("min_size"), 0, 2**63 - 1, "min_size")
    _integer(config.get("duplicate_min_size"), 1, 2**63 - 1, "duplicate_min_size")
    if config.get("approval") is not None:
        raise _error("Saved scan settings must not contain action approval.", "state_corrupt")


def _run_scan(config, store, cancel=None, progress=None):
    from .cli import _scan
    _validate_saved_scan(config)
    _validate_scope(config)
    args = argparse.Namespace(root=[root["path"] for root in config["roots"]],
                              areas=",".join(config["areas"]), max_items=config["max_items"],
                              min_size=config["min_size"], duplicate_min_size=config["duplicate_min_size"])
    return _scan(args, store, cancel, progress)


_SCAN_KEYS = {"areas", "max_items", "min_size", "duplicate_min_size", "search"}


def _presets(action, options, store, cancel, progress):
    config = _configuration(options)
    _check_keys(config, _SCAN_KEYS | {"name"})
    if action == "inspect":
        return {"status": "complete", "presets": _snapshot(store)["presets"], "saved_approval": False}
    name = _name(config.get("name"), "preset name")
    if action == "save":
        preset = {"name": name, "created_at": utc_now(), "scan": _scan_config(options, config)}
        _settings_change(store, lambda settings: _lifecycle(settings)["presets"].__setitem__(name, preset))
        return {"status": "saved", "preset": preset, "saved_approval": False}
    if action == "remove":
        removed = _settings_change(store, lambda settings: _lifecycle(settings)["presets"].pop(name, None))
        return {"status": "removed" if removed else "not_found", "name": name, "files_changed": False}
    if action == "run":
        if options.get("roots") or set(config) - {"name"}:
            raise _error("A saved scope cannot be overridden at run time; save a reviewed replacement.", "scope_mismatch")
        preset = _snapshot(store)["presets"].get(name)
        if preset is None:
            raise _error("Saved scan scope was not found.", "record_not_found")
        scan = _run_scan(preset["scan"], store, cancel, progress)
        scan["preset_name"] = name
        scan["saved_filters"] = preset["scan"].get("filters", {})
        store.put("scans", scan)
        return scan
    raise _error("Unsupported saved-scopes action.", "unsupported_action")


def _favorites(action, options, store):
    config = _configuration(options)
    _check_keys(config, {"name"})
    if action == "inspect":
        from .extensions import catalog
        names = _snapshot(store)["favorites"]
        available = {item["name"]: item for item in catalog()["tools"]}
        return {"status": "complete", "favorites": names, "approval": None,
                "tools": [available.get(name, {"name": name, "implementation": "unavailable",
                                                "reason": "This saved tool name is no longer installed."}) for name in names]}
    name = _name(config.get("name"), "tool name")
    if action not in {"add", "remove"}:
        raise _error("Unsupported favorites action.", "unsupported_action")
    if action == "add":
        from .extensions import catalog
        catalog(name)  # Discovery only; do not probe account, file, or permission access.
    def change(settings):
        current = _lifecycle(settings)["favorites"]
        if action == "add" and name not in current:
            if len(current) >= 100:
                raise _error("At most 100 favorite tools may be saved.")
            current.append(name)
        elif action == "remove" and name in current:
            current.remove(name)
        return current
    return {"status": "complete", "favorites": _settings_change(store, change), "approval": None}


def _launchagents_path():
    return Path.home() / "Library" / "LaunchAgents"


def _agent_dir(create=False):
    path = _launchagents_path()
    parent, _ = _open_parent(str(path))
    try:
        if create:
            try:
                os.mkdir(path.name, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
        descriptor = os.open(path.name, _directory_flags(), dir_fd=parent)
    finally:
        os.close(parent)
    info = os.fstat(descriptor)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
        os.close(descriptor)
        raise _error("The user LaunchAgents directory must not be writable by others.", "unsafe_launchagents")
    return descriptor


def _label(identifier):
    return _LABEL_PREFIX + _name(identifier, "schedule ID")


def _agent_bytes(identifier, store, interval):
    runner = Path(__file__).resolve().with_name("schedule_runner.py")
    python = Path(sys.executable).resolve()
    return plistlib.dumps({
        "Label": _label(identifier),
        "ProgramArguments": [str(python), "-I", str(runner), "--data-dir", str(store.root), "--schedule", identifier],
        "StartInterval": interval, "RunAtLoad": False, "ProcessType": "Background",
        "LowPriorityIO": True, "Nice": 10, "ThrottleInterval": 60,
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONNOUSERSITE": "1"},
    }, fmt=plistlib.FMT_XML, sort_keys=True)


def _agent_read(identifier):
    try:
        directory = _agent_dir()
    except FileNotFoundError:
        return None
    fd = None
    try:
        try:
            fd = os.open(_label(identifier) + ".plist", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return None
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or info.st_mode & 0o022 or info.st_size > _MAX_PLIST):
            raise _error("Existing LaunchAgent is not an owned, regular configuration file.", "unsafe_launchagent")
        value = os.read(fd, _MAX_PLIST + 1)
        if len(value) > _MAX_PLIST:
            raise _error("LaunchAgent exceeds supported size.", "unsafe_launchagent")
        return value
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)


def _agent_write(identifier, data):
    directory = _agent_dir(create=True)
    fd = None
    try:
        fd = os.open(_label(identifier) + ".plist", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        with os.fdopen(fd, "wb", closefd=False) as target:
            target.write(data)
            target.flush()
            os.fsync(fd)
        os.fsync(directory)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)


def _agent_remove(identifier, expected):
    directory = _agent_dir()
    fd = None
    try:
        name = _label(identifier) + ".plist"
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or os.read(fd, _MAX_PLIST + 1) != expected):
            raise _error("LaunchAgent changed; its file was preserved.", "stale_schedule")
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise _error("LaunchAgent identity changed; its file was preserved.", "stale_schedule")
        os.unlink(name, dir_fd=directory)
        os.fsync(directory)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)


def _launchctl(arguments):
    return subprocess.run(["/bin/launchctl", *arguments], capture_output=True, text=True,
                          timeout=15, check=False)


def _loaded(identifier):
    result = _launchctl(["print", f"gui/{os.getuid()}/{_label(identifier)}"])
    if result.returncode == 0:
        return True
    # Missing service codes are not generic authorization failures.
    if result.returncode in (3, 113) or "Could not find service" in result.stderr:
        return False
    raise _error("launchd state could not be checked; resolve the user-session error before changing persistence.",
                 "schedule_state_unknown")


def _checks_config(checks, *, scheduled=False):
    allowed = {"malware", "updates", "memory", "battery"} if scheduled else {"malware", "updates", "health", "maintenance"}
    fields = {"malware": {"engine_path", "mode", "targets"}, "updates": {"brew_path"},
              "memory": {"limit"}, "battery": {"sample_seconds"},
              "health": {"sample_seconds", "limit"}, "maintenance": {"tool"}}
    if not isinstance(checks, dict) or set(checks) - allowed:
        raise _error("checks must name only explicitly supported read-only checks: " + ", ".join(sorted(allowed)))
    for name, config in checks.items():
        if not isinstance(config, dict):
            raise _error("Each additional check needs a configuration object.")
        _check_keys(config, fields[name])
        if name == "malware":
            path = config.get("engine_path")
            if not isinstance(path, str) or not os.path.isabs(path) or "\x00" in path:
                raise _error("Scheduled/combined malware scans need an explicit absolute engine_path.")
            if config.get("mode", "balanced") not in {"quick", "balanced", "deep"}:
                raise _error("Choose quick, balanced, or deep malware mode.")
            targets = config.get("targets", ["files"])
            if not isinstance(targets, list) or not targets or any(not isinstance(x, str) or x not in {"files", "archives", "disk-images", "mail", "external", "icloud"} for x in targets):
                raise _error("Choose supported malware targets.")
        if name == "updates" and "brew_path" in config:
            path = config["brew_path"]
            if not isinstance(path, str) or not os.path.isabs(path) or "\x00" in path:
                raise _error("brew_path must be an absolute local executable path.")
        if name == "maintenance" and config.get("tool", "memory-diagnosis") not in {"memory-diagnosis", "purgeable", "periodic", "disk-permissions"}:
            raise _error("Combined maintenance supports memory-diagnosis, purgeable, periodic, or disk-permissions inspection.")
        if "sample_seconds" in config:
            value = config["sample_seconds"]
            if type(value) not in (int, float) or not (0 if name == "battery" else 0.1) <= value <= 5:
                raise _error("sample_seconds is outside the bounded monitor range.")
        if "limit" in config:
            _integer(config["limit"], 1, 1000, "limit")
    return json.loads(json.dumps(checks, allow_nan=False))


def _run_check(name, configuration, roots, store, cancel=None, progress=None):
    options = {"roots": roots, "selected": [], "record_id": None, "approve": None,
               "configuration": configuration}
    try:
        if name == "malware":
            from . import malware_tools
            return malware_tools.dispatch("malware-scan", "inspect", options, store=store, cancel=cancel, progress=progress)
        if name == "updates":
            from . import app_tools
            return app_tools.dispatch("app-updates", "inspect", options, store=store, cancel=cancel, progress=progress)
        if name in {"health", "memory", "battery"}:
            from . import monitoring_tools
            return monitoring_tools.dispatch(name, "inspect", options, store=store, cancel=cancel, progress=progress)
        if name == "maintenance":
            from . import maintenance_tools
            options["configuration"] = {}
            return maintenance_tools.dispatch(configuration.get("tool", "memory-diagnosis"), "inspect", options,
                                              store=store, cancel=cancel, progress=progress)
    except (CleanMacError, OSError, ImportError, subprocess.SubprocessError) as error:
        return {"status": "unavailable", "coverage": "unscanned", "code": getattr(error, "code", "check_unavailable"),
                "message": "This selected check could not complete its required preflight or measurement.", "items": []}
    raise _error("Unsupported combined check.", "unsupported_action")


def _measured_alerts(name, result, schedule):
    if result.get("status") not in {"complete", "completed", "partial"}:
        return []
    if name == "memory":
        pressure = result.get("measurements", {}).get("pressure", {})
        if pressure.get("status") == "available" and pressure.get("level") in {"warning", "critical"}:
            return [{"key": "memory-pressure-" + pressure["level"], "kind": "memory-pressure", "level": pressure["level"]}]
    if name == "battery":
        alerts = []
        for index, item in enumerate(result.get("items", [])):
            charge = item.get("charge_percent")
            if type(charge) in (int, float) and 0 <= charge <= schedule["battery_charge_percent"] and item.get("external_power") is False:
                alerts.append({"key": "battery-charge-" + str(index), "kind": "battery-charge", "charge_percent": charge,
                               "threshold_percent": schedule["battery_charge_percent"]})
        return alerts
    if name == "updates":
        items = [item for item in result.get("items", []) if item.get("update_available") is True and item.get("source") == "homebrew"]
        if items:
            identity = sorted((str(item.get("token", "")), str(item.get("available_version", ""))) for item in items)
            return [{"key": "app-updates-" + digest(identity)[:20], "kind": "app-updates", "count": len(items),
                     "source": "installed Homebrew metadata; no auto-update or install performed"}]
    if name == "malware":
        count = result.get("counts", {}).get("detections")
        if type(count) is int and count > 0 and result.get("engine", {}).get("status") == "available":
            return [{"key": "malware-detections-" + str(count), "kind": "malware-detections", "count": count,
                     "action": "Review engine evidence before selecting any quarantine action."}]
    return []


def _schedule_config(options, config, store):
    _check_keys(config, _SCAN_KEYS | {"operation", "name", "interval_seconds", "quiet_hours",
                                    "require_ac_power", "max_load_per_cpu", "notify", "low_space_bytes",
                                    "alert_repeat_seconds", "checks", "battery_charge_percent", "preset"})
    quiet = config.get("quiet_hours")
    if quiet is not None and (not isinstance(quiet, list) or len(quiet) != 2
                              or any(type(x) is not int or not 0 <= x <= 23 for x in quiet)):
        raise _error("quiet_hours must be [start_hour, end_hour] in local time, or null.")
    load = config.get("max_load_per_cpu", 1.0)
    if type(load) not in (int, float) or not 0.1 <= load <= 4:
        raise _error("max_load_per_cpu must be a finite number from 0.1 to 4.")
    checks = _checks_config(config.get("checks", {}), scheduled=True)
    if "preset" in config:
        preset_name = _name(config["preset"], "preset name")
        if options.get("roots") or set(config).intersection(_SCAN_KEYS):
            raise _error("A scheduled preset cannot be combined with root or scan-setting overrides.", "scope_mismatch")
        preset = _snapshot(store)["presets"].get(preset_name)
        if preset is None:
            raise _error("Saved scan scope was not found.", "record_not_found")
        scope = json.loads(json.dumps(preset["scan"]))
        _validate_scope(scope)
    else:
        preset_name = None
        scope = _scan_config(options, config)
    return {"id": new_id("schedule"), "name": _name(config.get("name"), "schedule name"),
            "created_at": utc_now(), "scan": scope, "source_preset": preset_name,
            "interval_seconds": _integer(config.get("interval_seconds", 86400), 900, 31 * 86400, "interval_seconds"),
            "quiet_hours": quiet, "require_ac_power": _boolean(config.get("require_ac_power", True), "require_ac_power"),
            "max_load_per_cpu": float(load), "notify": _boolean(config.get("notify", False), "notify"),
            "low_space_bytes": _integer(config.get("low_space_bytes", 10 * 1024**3), 1, 2**63 - 1, "low_space_bytes"),
            "alert_repeat_seconds": _integer(config.get("alert_repeat_seconds", 86400), 3600, 31 * 86400, "alert_repeat_seconds"),
            "checks": checks,
            "battery_charge_percent": _integer(config.get("battery_charge_percent", 20), 1, 99, "battery_charge_percent"),
            "enabled": True, "state": "planned", "read_only": True,
            "allowed_actions": ["scan", "measure-free-space", "notify"], "approval": None}


def _measure_free(root):
    """Measure the selected directory's open volume, without a second path lookup."""
    parent, _ = _open_parent(root["path"], root["ancestors"])
    fd = None
    try:
        fd = os.open(os.path.basename(root["path"]), _directory_flags(), dir_fd=parent)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (root["device"], root["inode"]):
            raise _error("Selected volume root changed before measurement.", "stale_scope")
        values = os.fstatvfs(fd)
        return {"device": root["device"], "free_bytes": values.f_bavail * values.f_frsize}
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def _schedule_plan(options, store):
    from .actions import create_action_plan
    if sys.platform != "darwin":
        raise _error("LaunchAgent schedules require macOS.", "unsupported_platform")
    config = _configuration(options)
    operation = config.get("operation", "create")
    if operation not in {"create", "enable", "disable", "remove"}:
        raise _error("Choose create, enable, disable, or remove.")
    if operation == "create":
        if len(_snapshot(store)["schedules"]) >= 64:
            raise _error("At most 64 schedules may be saved; remove an unused schedule first.", "schedule_limit")
        schedule = _schedule_config(options, config, store)
        previous = None
    else:
        _check_keys(config, {"operation"})
        if options.get("roots"):
            raise _error("Lifecycle changes cannot replace a schedule's scope.", "scope_mismatch")
        identifier = _name(options.get("record_id"), "schedule ID")
        previous = _snapshot(store)["schedules"].get(identifier)
        if previous is None:
            raise _error("Schedule was not found.", "record_not_found")
        if operation == "enable" and previous.get("state") not in {"ready", "disabled"}:
            raise _error("Inspect the interrupted schedule before changing it.", "recovery_needed")
        schedule = previous
    identifier = schedule["id"]
    data = _agent_bytes(identifier, store, schedule["interval_seconds"])
    current = _agent_read(identifier)
    expected_values = [None] if previous is None else [None, data]
    if current not in expected_values:
        raise _error("LaunchAgent does not match its stored configuration; inspect it before continuing.", "stale_schedule")
    loaded = _loaded(identifier)
    if previous is None and loaded:
        raise _error("A service with this generated schedule label already exists.", "schedule_conflict")
    payload = {"operation": operation, "schedule": schedule, "previous": previous,
               "agent_sha256": hashlib.sha256(data).hexdigest(),
               "existing_agent_sha256": hashlib.sha256(current).hexdigest() if current else None,
               "loaded": loaded, "launchagents_path": str(_launchagents_path()),
               "program": plistlib.loads(data)["ProgramArguments"], "launchagent": plistlib.loads(data)}
    return create_action_plan(store, "schedules", operation, payload,
                              {"operation": operation, "schedule_name": schedule["name"],
                               "roots": [x["path"] for x in schedule["scan"]["roots"]],
                               "read_only": True, "notifications": schedule["notify"],
                               "interval_seconds": schedule["interval_seconds"],
                               "additional_checks": list(schedule["checks"]),
                               "persistence": "User LaunchAgent; no cleanup or account access is authorized."})


def _schedule_apply(options, store, cancel, progress):
    from .actions import execute_action_plan, checkpoint_action
    identifier = options.get("record_id")
    plan = store.get("plans", identifier)
    operation = plan.get("action")
    if operation not in {"create", "enable", "disable", "remove"}:
        raise _error("This is not a schedule lifecycle plan.", "invalid_plan")

    def validate(actual):
        payload = actual["payload"]
        schedule = payload["schedule"]
        if sys.platform != "darwin" or payload["launchagents_path"] != str(_launchagents_path()):
            raise _error("Schedule host or installation path changed.", "stale_schedule")
        current = _snapshot(store)["schedules"].get(schedule["id"])
        if current != payload["previous"]:
            raise _error("Schedule settings changed after review.", "stale_schedule")
        data = _agent_bytes(schedule["id"], store, schedule["interval_seconds"])
        if (hashlib.sha256(data).hexdigest() != payload["agent_sha256"]
                or plistlib.loads(data) != payload["launchagent"]):
            raise _error("Schedule runner or Python path changed after review.", "stale_schedule")
        existing = _agent_read(schedule["id"])
        if (hashlib.sha256(existing).hexdigest() if existing else None) != payload["existing_agent_sha256"]:
            raise _error("LaunchAgent changed after review.", "stale_schedule")
        if _loaded(schedule["id"]) != payload["loaded"]:
            raise _error("launchd service state changed after review.", "stale_schedule")
        if operation in {"create", "enable"}:
            _validate_scope(schedule["scan"])

    def execute(actual):
        payload = actual["payload"]
        schedule = dict(payload["schedule"])
        sid = schedule["id"]
        data = _agent_bytes(sid, store, schedule["interval_seconds"])
        schedule["state"] = "changing"
        schedule["enabled"] = False
        checkpoint_action({"schedule_id": sid, "phase": "disable-runner-before-lifecycle-change",
                           "agent_path": str(_launchagents_path() / (_label(sid) + ".plist")),
                           "operation": operation})
        _settings_change(store, lambda settings: _lifecycle(settings)["schedules"].__setitem__(sid, schedule))
        if operation in {"create", "enable"}:
            if payload["existing_agent_sha256"] is None:
                checkpoint_action({"schedule_id": sid, "phase": "create-agent-file", "operation": operation})
                _agent_write(sid, data)
            if not payload["loaded"]:
                checkpoint_action({"schedule_id": sid, "phase": "bootstrap", "operation": operation})
                result = _launchctl(["bootstrap", f"gui/{os.getuid()}", str(_launchagents_path() / (_label(sid) + ".plist"))])
                if result.returncode != 0:
                    raise _error("launchd did not confirm loading this schedule; inspect the saved receipt and service before retrying.", "schedule_load_failed")
            if not _loaded(sid):
                raise _error("launchd did not confirm the scheduled service.", "schedule_state_unknown")
            schedule.update(state="ready", enabled=True)
            _settings_change(store, lambda settings: _lifecycle(settings)["schedules"].__setitem__(sid, schedule))
        else:
            if payload["loaded"]:
                checkpoint_action({"schedule_id": sid, "phase": "bootout", "operation": operation})
                result = _launchctl(["bootout", f"gui/{os.getuid()}/{_label(sid)}"])
                if result.returncode != 0:
                    raise _error("launchd did not confirm stopping this schedule; its runner is disabled in settings.", "schedule_stop_failed")
            if _loaded(sid):
                raise _error("The scheduled service still appears loaded; runner remains disabled.", "schedule_state_unknown")
            if payload["existing_agent_sha256"] is not None:
                checkpoint_action({"schedule_id": sid, "phase": "remove-agent-file", "operation": operation})
                _agent_remove(sid, data)
            if operation == "remove":
                _settings_change(store, lambda settings: _lifecycle(settings)["schedules"].pop(sid, None))
            else:
                schedule.update(state="disabled", enabled=False)
                _settings_change(store, lambda settings: _lifecycle(settings)["schedules"].__setitem__(sid, schedule))
        return {"status": "completed", "operation": operation, "schedule_id": sid,
                "read_only": True, "scan_records_preserved": True}
    sid = _name(plan.get("payload", {}).get("schedule", {}).get("id"), "schedule ID")
    with store.lock("schedule-" + sid):
        return execute_action_plan(store, identifier, options.get("approve"), "schedules", operation,
                                   validate, execute, cancel=cancel, progress=progress)


def _quiet(now, hours):
    if hours is None or hours[0] == hours[1]:
        return False
    hour = now.hour
    return hours[0] <= hour < hours[1] if hours[0] < hours[1] else hour >= hours[0] or hour < hours[1]


def _power_source():
    result = subprocess.run(["/usr/bin/pmset", "-g", "batt"], capture_output=True, text=True,
                            timeout=5, check=False)
    if result.returncode == 0:
        if "AC Power" in result.stdout:
            return "ac"
        if "Battery Power" in result.stdout:
            return "battery"
    return "unknown"


def _notify_alerts():
    # Fixed generic content, no filenames, mailbox/account data, or script interpolation.
    script = 'display notification "A scheduled check met a threshold you configured. Review CleanMac history." with title "CleanMac scan"'
    result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True,
                            text=True, timeout=10, check=False)
    return "submitted" if result.returncode == 0 else "unavailable"


def run_schedule(store, identifier, *, cancel=None, progress=None, now=None):
    """One read-only scheduled run; no external dispatch or cleanup entrypoint."""
    if sys.platform != "darwin":
        raise _error("Scheduled scans require macOS.", "unsupported_platform")
    identifier = _name(identifier, "schedule ID")
    now = now or dt.datetime.now().astimezone()
    with store.lock("run-" + identifier, timeout=0):
        schedule = _snapshot(store)["schedules"].get(identifier)
        if schedule is None:
            raise _error("Schedule does not exist.", "record_not_found")
        report = {"schema_version": 1, "id": new_id("scheduled-run"), "created_at": utc_now(),
                  "schedule_id": identifier, "status": "skipped", "read_only": True,
                  "notification": "not-requested", "errors": []}
        def finish():
            store.put("scans", report)
            def change(settings):
                current = _lifecycle(settings)["schedules"].get(identifier)
                if current is not None:
                    current["last_run"] = {k: report[k] for k in ("id", "created_at", "status", "reason") if k in report}
            _settings_change(store, change)
            return report
        if not schedule.get("enabled") or schedule.get("state") != "ready":
            report["reason"] = "disabled-or-changing"
            return finish()
        if cancel and cancel():
            report.update(status="cancelled", reason="cancelled-before-start")
            return finish()
        if _quiet(now, schedule.get("quiet_hours")):
            report["reason"] = "quiet-hours"
            return finish()
        try:
            _boolean(schedule.get("notify"), "notify")
            _boolean(schedule.get("require_ac_power"), "require_ac_power")
            if schedule.get("read_only") is not True or schedule.get("allowed_actions") != ["scan", "measure-free-space", "notify"]:
                raise _error("Stored schedules authorize only the known read-only runner.", "state_corrupt")
            _checks_config(schedule.get("checks", {}), scheduled=True)
            _integer(schedule.get("low_space_bytes"), 1, 2**63 - 1, "low_space_bytes")
            _integer(schedule.get("battery_charge_percent"), 1, 99, "battery_charge_percent")
            _integer(schedule.get("alert_repeat_seconds"), 3600, 31 * 86400, "alert_repeat_seconds")
            if schedule["require_ac_power"] and _power_source() != "ac":
                report["reason"] = "ac-power-unavailable-or-unverified"
                return finish()
            load = os.getloadavg()[0] / max(1, os.cpu_count() or 1)
            if load > schedule["max_load_per_cpu"]:
                report["reason"] = "load-policy"
                return finish()
            last_check = 0.0
            saved_stopped = False
            def stopped():
                nonlocal last_check, saved_stopped
                if cancel and cancel():
                    return True
                monotonic_now = time.monotonic()
                if monotonic_now - last_check >= 0.25:
                    current = _snapshot(store)["schedules"].get(identifier)
                    saved_stopped = not current or not current.get("enabled") or current.get("state") != "ready"
                    last_check = monotonic_now
                return saved_stopped
            scan = _run_scan(schedule["scan"], store, stopped, progress)
            report.update(status=scan["status"], scan_id=scan["id"], summary=scan.get("summary", {}))
            report["coverage_errors"] = scan.get("errors", [])
            if scan["status"] == "cancelled" or stopped():
                report.update(status="cancelled", reason="cancelled-during-scan")
                return finish()
            free = []
            for root in schedule["scan"]["roots"]:
                try:
                    free.append(_measure_free(root))
                except (OSError, CleanMacError) as error:
                    report["errors"].append({"code": getattr(error, "code", "space_measurement_failed")})
                    report["status"] = "partial"
            by_device = {value["device"]: value["free_bytes"] for value in free}
            low = sorted(device for device, amount in by_device.items() if amount < schedule["low_space_bytes"])
            report["free_space"] = [{"device": device, "free_bytes": amount} for device, amount in by_device.items()]
            report["low_space_devices"] = low
            alerts = [{"key": "low-space-" + str(device), "kind": "low-space",
                       "free_bytes": by_device[device], "threshold_bytes": schedule["low_space_bytes"]} for device in low]
            report["checks"] = []
            for name, configuration in schedule.get("checks", {}).items():
                if stopped():
                    report.update(status="cancelled", reason="cancelled-before-additional-check")
                    return finish()
                result = _run_check(name, configuration, [x["path"] for x in schedule["scan"]["roots"]], store, stopped, progress)
                report["checks"].append({"name": name, "result": result})
                if result.get("status") == "cancelled" or stopped():
                    report.update(status="cancelled", reason="cancelled-during-additional-check")
                    return finish()
                if result.get("status") not in {"complete", "completed"}:
                    report["status"] = "partial"
                alerts.extend(_measured_alerts(name, result, schedule))
            report["alerts"] = alerts
            alert_keys = sorted(alert["key"] for alert in alerts)
            previous = schedule.get("last_alert", {})
            timestamp = now.timestamp()
            due = bool(alert_keys) and (previous.get("keys") != alert_keys or timestamp - previous.get("at", 0) >= schedule["alert_repeat_seconds"])
            current = _snapshot(store)["schedules"].get(identifier)
            if schedule["notify"] and alert_keys:
                if not current or not current.get("enabled"):
                    report["notification"] = "suppressed-disabled"
                elif due:
                    report["notification"] = _notify_alerts()
                    if report["notification"] == "submitted":
                        def record_alert(settings):
                            saved = _lifecycle(settings)["schedules"].get(identifier)
                            if saved:
                                saved["last_alert"] = {"keys": alert_keys, "at": timestamp}
                        _settings_change(store, record_alert)
                else:
                    report["notification"] = "deduplicated"
            elif not alert_keys and previous:
                def clear_alert(settings):
                    saved = _lifecycle(settings)["schedules"].get(identifier)
                    if saved:
                        saved.pop("last_alert", None)
                _settings_change(store, clear_alert)
        except (CleanMacError, OSError, subprocess.SubprocessError) as error:
            report.update(status="failed", reason=getattr(error, "code", "scheduled_scan_failed"))
            report["errors"].append({"code": report["reason"]})
        return finish()


def _schedules(action, options, store, cancel, progress):
    if action == "plan":
        return _schedule_plan(options, store)
    if action == "apply":
        return _schedule_apply(options, store, cancel, progress)
    if action == "run":
        if options.get("roots") or _configuration(options):
            raise _error("Scheduled runs use their saved scope and policies; overrides are not supported.", "scope_mismatch")
        return run_schedule(store, options.get("record_id"), cancel=cancel, progress=progress)
    if action != "inspect":
        raise _error("Unsupported schedule action.", "unsupported_action")
    values = []
    for schedule in _snapshot(store)["schedules"].values():
        entry = dict(schedule)
        if sys.platform == "darwin":
            try:
                current = _agent_read(schedule["id"])
                entry["agent_file"] = "matching" if current == _agent_bytes(schedule["id"], store, schedule["interval_seconds"]) else "missing" if current is None else "changed"
                entry["loaded"] = _loaded(schedule["id"])
            except (CleanMacError, OSError, subprocess.SubprocessError):
                entry["loaded"] = "unknown"
                entry["inspection_error"] = "Schedule installation could not be verified."
        else:
            entry["loaded"] = "unsupported-platform"
        values.append(entry)
    return {"status": "complete", "schedules": values, "persistence_installed_by_inspect": False,
            "supported_alerts": ["selected-volume-low-space", "memory-pressure", "battery-charge", "app-updates", "malware-detections"],
            "other_alerts": "Unresponsive-app, device-drain, and Trash-volume alerts have no verified measurement adapter and are unavailable.",
            "sources": SOURCES}


def _smart_care(action, options, store, cancel, progress):
    if action == "review":
        record = store.get("scans", options.get("record_id"))
        if record.get("tool") != "smart-care":
            raise _error("This is not a Smart Care scan.", "invalid_record")
        return record
    if action != "inspect":
        raise _error("Smart Care is read-only; review its saved scan and use an exact selected cleanup plan.", "unsupported_action")
    config = _configuration(options)
    _check_keys(config, _SCAN_KEYS | {"modules", "checks"})
    checks = _checks_config(config.get("checks", {}))
    modules = config.get("modules", ["storage", "large", "duplicates", "caches"])
    known = _AREAS | {"malware", "maintenance", "updates", "health"}
    if not isinstance(modules, list) or not modules or any(not isinstance(x, str) or x not in known for x in modules):
        raise _error("modules must be a nonempty array of known Smart Care checks.")
    if set(checks) - set(modules):
        raise _error("Every configured check must also be selected in modules.")
    areas = [name for name in modules if name in _AREAS]
    if areas:
        scope = _scan_config(options, dict(config, areas=areas))
        scan = _run_scan(scope, store, cancel, progress)
    else:
        scan = {"schema_version": 1, "id": new_id("scan"), "created_at": utc_now(), "status": "complete",
                "roots": [], "areas": [], "items": [], "findings": [], "duplicate_groups": [], "errors": [],
                "summary": {"selected_count": 0, "reclaimable_bytes": None}}
    tiles = []
    for name in dict.fromkeys(modules):
        if name in areas:
            tiles.append({"name": name, "status": scan["status"], "scan_id": scan["id"], "selected_count": 0})
        elif name in checks and scan["status"] != "cancelled" and not (cancel and cancel()):
            result = _run_check(name, checks[name], options.get("roots", []), store, cancel, progress)
            tiles.append({"name": name, "status": result.get("status", "unavailable"), "record_id": result.get("id"),
                          "result": result, "selected_count": 0})
        else:
            tiles.append({"name": name, "status": "unscanned", "reason": "Run the dedicated tool with its own required scope and preflight.", "result": None})
    scan.update(tool="smart-care", tiles=tiles, saved_filters={"search": config.get("search")},
                selection_note="Use this scan ID with review and plan. No file or action was selected.")
    if any(tile["status"] not in {"complete", "completed"} for tile in tiles) and scan["status"] == "complete":
        scan["status"] = "partial"
    store.put("scans", scan)
    return scan


def dispatch(name, action, options, *, store, cancel=None, progress=None):
    if not isinstance(options, dict):
        raise _error("Tool options must be an object.")
    if name == "saved-scopes":
        result = _presets(action, options, store, cancel, progress)
    elif name == "favorites":
        result = _favorites(action, options, store)
    elif name == "schedules":
        result = _schedules(action, options, store, cancel, progress)
    elif name == "smart-care":
        result = _smart_care(action, options, store, cancel, progress)
    else:
        raise _error("Unknown lifecycle tool.", "unknown_tool")
    result.setdefault("schema_version", 1)
    result.setdefault("tool", name)
    return result

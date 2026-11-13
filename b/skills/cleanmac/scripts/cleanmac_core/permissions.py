"""Scoped capability checks and user-directed macOS permission handoffs.

No permission database is queried. A successful root probe does not establish
Full Disk Access or imply that its descendants were scanned.
"""

from __future__ import annotations

import errno
import importlib
import json
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys
from typing import Any

from .common import CleanMacError, display_path, utc_now


SETTINGS_URLS = {
    "files-and-folders": "x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders",
    "full-disk-access": "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles",
}
PERMISSION_SOURCES = [
    "https://support.apple.com/guide/security/controlling-app-access-to-files-secddd1d86a6/web",
    "https://developer.apple.com/documentation/security/app-sandbox",
]
_FEATURE_SUPPORT = {
    "CM-001": ("permissions", "capabilities", ["inspect"], "Selected-root access and runtime preflight; not a global permission audit."),
    "CM-002": ("rules", "rules", ["inspect"], "Only the bundled reviewed rules; unknown layouts remain report-only."),
    "CM-003": ("inventory", "scan", ["scan"], "Bounded metadata inventory of selected local roots; partial coverage is explicit."),
    "CM-004": ("plans", "plan", ["plan"], "Explicit regular-file selections only; protected items and unsupported actions are rejected."),
    "CM-005": ("executor", "apply / restore", ["trash", "restore"], "Native Trash helper and same-volume private capture required; no permanent deletion."),
    "CM-006": ("review", "review", ["review"], "Interactive local-file workflow; advanced categories remain unavailable."),
    "CM-007": ("state", "history", ["inspect"], "Private local sessions, plans, and receipts; no remote account history."),
    "CM-009": ("cli", None, ["scan", "review"], "Launch cleanmac with no subcommand for the interactive menu. Selected local storage areas only; no malware, updates, or maintenance automation."),
    "CM-010": ("cli", "ignore", ["inspect", "manage-exclusions"], "Persistent ignores and saved reviews/history are supported; favorites and named scan presets are not implemented."),
    "CM-011": ("rules", "scan --areas caches", ["scan", "plan"], "Only individually eligible cache rules; a cache-folder name is not permission to remove it."),
    "CM-022": ("adapters", "xcode", ["inspect"], "Read-only Xcode category inventory; simulator/runtime deletion is unavailable."),
    "CM-044": ("adapters", "apps", ["inspect"], "Read-only app bundle inventory; app removal and compatibility guarantees are unavailable."),
    "CM-048": ("inventory", "scan --areas large", ["scan", "plan"], "Explicit local file review; age or size never automatically selects a file."),
    "CM-049": ("duplicates", "scan --areas duplicates", ["scan", "plan"], "Exact local-file duplicates with retained keepers; no cloud hydration or similar-image matching."),
    "CM-051": ("inventory", "scan --root <Downloads> --areas files", ["scan", "plan"], "Selected Downloads file inventory; download-origin attribution is not implemented."),
    "CM-053": ("inventory", "scan --areas storage", ["scan", "review"], "Local inventory and size reporting; allocated estimates are not guaranteed reclaimable space."),
    "CM-075": ("adapters", "overview", ["inspect"], "Read-only system overview; no continuous health monitor or automated optimization."),
}


def _skill_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _host_context() -> dict[str, Any]:
    """Environment provides a hint, never proof of TCC attribution."""
    known = {
        "Apple_Terminal": "Terminal",
        "iTerm.app": "iTerm",
        "vscode": "Visual Studio Code",
        "WarpTerminal": "Warp",
        "ghostty": "Ghostty",
    }
    hint = known.get(os.environ.get("TERM_PROGRAM", ""))
    return {
        "executable": sys.executable,
        "parent_pid": os.getppid(),
        "host_hint": hint,
        "host_hint_source": "TERM_PROGRAM" if hint else None,
        "attribution": "unverified",
        "instruction": (
            f"macOS may name {hint} or the app that launched it. Use the exact app named in the permission alert or System Settings."
            if hint else
            "Use the exact app named in the macOS permission alert. For an agent, this can be its desktop host or terminal; the Python executable alone does not identify the responsible app."
        ),
    }


def _suggested_pane(path: str) -> str:
    parts = Path(os.path.abspath(os.path.expanduser(path))).parts
    # An app-data location can need broad privacy access. This is guidance, not
    # an assertion that FDA is granted, necessary, or sufficient for that path.
    if "Library" in parts and any(part in parts for part in ("Mail", "Messages", "Safari", "Containers", "Group Containers")):
        return "full-disk-access"
    return "files-and-folders"


def describe_access_error(path: os.PathLike[str] | str, error: BaseException) -> dict[str, Any]:
    """Explain a failure without falsely diagnosing TCC from a single errno."""
    value = os.fspath(path)
    number = getattr(error, "errno", None)
    pane = _suggested_pane(value)
    host = _host_context()
    result: dict[str, Any] = {
        "path": value,
        "display_path": display_path(value),
        "errno": number,
        "capability": "selected-root-access",
        "status": "unavailable",
        "coverage": "unscanned",
        "system_settings_url": None,
        "host": host,
        "can_continue_with_accessible_items": True,
        "actions": [
            {"id": "skip", "label": "Continue with accessible items"},
            {"id": "retry", "label": "Retry this location after resolving access"},
        ],
    }
    if number == errno.EACCES:
        result.update(
            status="permission-needed", reason="filesystem-permission-denied",
            message="Filesystem permissions or an ACL denied access. Review this location's ownership and permissions, or skip it. Full Disk Access does not replace ordinary file permissions.",
            action="review-file-permissions",
        )
    elif number == errno.EPERM:
        result.update(
            status="permission-needed", reason="privacy-or-system-policy-denied",
            message="macOS denied this operation. Privacy controls, App Sandbox, system protection, or another policy may be responsible; this error alone cannot identify which. Review only the access needed for this location, or skip it.",
            action="review-privacy-settings", system_settings_url=SETTINGS_URLS[pane],
            suggested_settings_pane=pane,
        )
        result["actions"].append({"id": "open-settings", "label": "Open the relevant privacy settings", "pane": pane, "requires_explicit_request": True})
    elif number == errno.ENOENT:
        result.update(reason="location-missing", message="The selected location does not exist or its volume is unavailable.", action="choose-location-or-reconnect-volume")
    elif number == errno.ENOTDIR:
        result.update(reason="not-a-directory", message="A path component is not a directory. Choose an existing local location.", action="choose-location")
    elif number == errno.ELOOP:
        result.update(reason="symlink-not-followed", message="This location includes a symbolic link. Select the intended real directory explicitly.", action="choose-real-location")
    elif number == errno.EROFS:
        result.update(reason="read-only-volume", message="The volume is read-only. Inspection may still be possible; cleanup cannot write to it.", action="continue-read-only")
    else:
        result.update(reason="access-check-failed", message="Access could not be checked. The location remains unscanned; no permission change was attempted.", action="review-error-or-skip")
    return result


def _probe_root(path: os.PathLike[str] | str) -> dict[str, Any]:
    try:
        raw = os.fspath(path)
    except TypeError as error:
        raise CleanMacError("A selected root must be a filesystem path.", code="invalid_root") from error
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise CleanMacError("A selected root must be a nonempty filesystem path.", code="invalid_root")
    value = os.path.abspath(os.path.expanduser(raw))
    try:
        info = os.lstat(value)
        if stat.S_ISLNK(info.st_mode):
            raise OSError(errno.ELOOP, "Symbolic root is not followed", value)
        if not stat.S_ISDIR(info.st_mode):
            return {
                "path": value, "display_path": display_path(value), "status": "unavailable",
                "reason": "directory-required", "coverage": "unscanned",
                "message": "Select a directory root. No file contents were opened.",
                "can_continue_with_accessible_items": True,
            }
        # A directory listing can cause a macOS consent dialog. It is only
        # attempted for a root explicitly provided by the caller. Never probe
        # a protected sentinel file to guess global Full Disk Access.
        fd = _open_directory(value)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                return {"path": value, "display_path": display_path(value), "status": "unavailable", "reason": "location-changed", "coverage": "unscanned", "message": "The selected root changed during its access check. Retry it."}
            with os.scandir(fd) as entries:
                next(entries, None)
        finally:
            os.close(fd)
        return {
            "path": value, "display_path": display_path(value), "status": "available",
            "reason": "root-listing-accessible", "coverage": "root-only",
            "message": "The selected directory can be listed. Descendant access and content reads have not been verified.",
            "full_disk_access": "unknown", "sandbox": "unknown",
        }
    except OSError as error:
        return describe_access_error(value, error)


def _open_directory(path: str) -> int:
    """Open each directory component without traversing substituted links."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(os.sep, flags)
    try:
        for component in Path(path).parts[1:]:
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _helper_status(native_path: os.PathLike[str] | str | None, is_macos: bool) -> dict[str, Any]:
    path = Path(native_path) if native_path is not None else _skill_root() / "native" / "bin" / "cleanmac-native"
    result: dict[str, Any] = {
        "path": str(path), "status": "unavailable", "privileged": False,
        "authorization": "not-applicable", "full_disk_access": "unknown",
        "protocol_version": None, "version": None,
        "note": "This is a user-level native Trash adapter, not a privileged system service. Its presence does not grant file access.",
    }
    if not is_macos:
        return dict(result, status="unsupported-platform", reason="Native Trash requires macOS.")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return dict(result, status="missing", reason="Build the optional native helper explicitly before applying a Trash plan.", build_script=str(_skill_root() / "native" / "build.sh"))
    except OSError as error:
        return dict(result, status="unreadable", reason="The native helper cannot be inspected.", errno=error.errno)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o022 or metadata.st_uid not in (os.getuid(), 0):
        return dict(result, status="unsafe-path", reason="The helper must be a regular, trusted-owner file without group or world write permission.")
    if not os.access(path, os.X_OK):
        return dict(result, status="not-executable", reason="The native helper is not executable.")
    try:
        process = subprocess.run([str(path), "info"], capture_output=True, text=True, timeout=3, check=False)
        if process.returncode != 0 or len(process.stdout) > 65536:
            return dict(result, status="unresponsive", reason="The native helper did not return a successful bounded capability response.")
        info = json.loads(process.stdout)
        if not isinstance(info, dict) or type(info.get("protocol_version")) is not int or info.get("protocol_version") != 1 or not isinstance(info.get("capabilities"), list) or "trash" not in info["capabilities"] or info.get("privileged") is not False or not isinstance(info.get("version"), str) or not info["version"]:
            return dict(result, status="incompatible", reason="The native helper does not implement the supported user-level Trash protocol.")
        return dict(result, status="available", protocol_version=1, version=info.get("version"), reason="The native helper responded to a read-only handshake. Individual actions still require access and validation.")
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        return dict(result, status="unresponsive", reason="The native helper could not complete its read-only capability handshake.")


def _module_available(name: str) -> bool:
    if not Path(__file__).with_name(name + ".py").is_file():
        return False
    try:
        importlib.import_module("." + name, package=__package__)
        return True
    except Exception:
        # Broken optional adapters are unavailable, not false positive support.
        # Module exception text can expose paths or configuration; keep it out
        # of the ordinary capability summary.
        return False


def _feature_capabilities(is_macos: bool, helper: dict[str, Any]) -> list[dict[str, Any]]:
    path = _skill_root() / "references" / "features.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        features = document["features"]
        if not isinstance(features, list):
            raise ValueError("features is not a list")
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise CleanMacError("The feature catalog is missing or invalid.", code="invalid_catalog") from error
    output = []
    for feature in features:
        feature_id = feature["id"]
        supported = _FEATURE_SUPPORT.get(feature_id)
        item = {
            "id": feature_id, "feature_id": feature_id, "name": feature["title"],
            "status": "unavailable", "implementation_status": "planned",
            "permitted_actions": [], "command": None,
            "reason": "This roadmap feature has no validated runtime adapter in this release.",
        }
        if supported:
            module, command, actions, limits = supported
            item.update(implementation_status="partial", command=command, limitations=limits)
            if command is None:
                item["invocation"] = []
            if not is_macos:
                item["reason"] = "The user-facing workflow supports macOS only. No macOS commands were invoked."
            elif not _module_available(module):
                item["reason"] = "The required runtime module is absent from this installation."
            elif feature_id == "CM-005" and helper["status"] != "available":
                item["reason"] = "Local cleanup is unavailable until the native helper is available; scanning and review remain usable."
            else:
                item.update(status="available", permitted_actions=actions, reason=limits)
        output.append(item)
    return output


def capabilities(roots: list[str] | tuple[str, ...] | None = None, *, native_path: os.PathLike[str] | str | None = None) -> dict[str, Any]:
    """Return capabilities without requesting grants or scanning implicit roots."""
    if isinstance(roots, (str, bytes)):
        raise CleanMacError("Pass selected roots as a list of paths.", code="invalid_root")
    selected = list(roots or [])
    if len(selected) > 128:
        raise CleanMacError("At most 128 selected roots can be checked at once.", code="invalid_root")
    system = platform.system()
    is_macos = system == "Darwin"
    helper = _helper_status(native_path, is_macos)
    access = [_probe_root(root) for root in dict.fromkeys(selected)] if is_macos else []
    runtime_ok = sys.version_info >= (3, 10)
    features = _feature_capabilities(is_macos and runtime_ok, helper)
    return {
        "schema_version": 1, "created_at": utc_now(),
        "status": "available" if is_macos and runtime_ok else "unavailable",
        "platform": {"system": system, "release": platform.release(), "macos_version": platform.mac_ver()[0] if is_macos else None, "machine": platform.machine(), "supported": is_macos},
        "runtime": {"python_version": platform.python_version(), "minimum_python": "3.10", "supported": runtime_ok, "host": _host_context()},
        "capabilities": features, "access": access, "helper": helper,
        "permission_state": {
            "app_sandbox": "unknown", "full_disk_access": "unknown",
            "filesystem_access": "per-selected-root", "privileged_service": "not-implemented",
            "note": "Global permission grants and inherited sandbox restrictions are not inferred from successful probes or administrator privileges.",
        },
        "permission_guidance": {
            "probe_scope": "Only explicitly supplied directory roots are checked, never sentinel protected files.",
            "popups": "macOS may show a consent dialog when a selected location is first accessed. The user chooses Allow or Don't Allow; CleanMac does not click the dialog or change grants.",
            "denial_choices": ["Continue with accessible items", "Review the relevant privacy setting", "Retry this location after the user changes access"],
            "full_disk_access": "Full Disk Access is optional and broad. Offer it only for a selected blocked feature that needs it; it does not override App Sandbox, ordinary file permissions, or system protection.",
            "settings_urls": dict(SETTINGS_URLS),
            "settings_fallback": "System Settings > Privacy & Security > Files & Folders or Full Disk Access. On older macOS, use System Preferences > Security & Privacy > Privacy.",
            "after_change": "If macOS requests a restart of the responsible app, let the user do so. Resume the saved session and retry only the originally selected roots.",
            "host": _host_context(), "sources": PERMISSION_SOURCES,
        },
    }


def open_permission_settings(pane: str = "files-and-folders") -> dict[str, Any]:
    """Open a fixed Settings pane only when explicitly requested by the caller."""
    if pane not in SETTINGS_URLS:
        raise CleanMacError("Choose files-and-folders or full-disk-access.", code="invalid_settings_pane")
    if platform.system() != "Darwin":
        raise CleanMacError("macOS privacy settings are unavailable on this platform.", code="unsupported_platform")
    try:
        process = subprocess.run(["/usr/bin/open", SETTINGS_URLS[pane]], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CleanMacError("Could not open System Settings. Open Privacy & Security manually.", code="settings_unavailable") from error
    if process.returncode:
        raise CleanMacError("Could not open System Settings. Open Privacy & Security manually.", code="settings_unavailable")
    return {"schema_version": 1, "status": "opened", "pane": pane, "url": SETTINGS_URLS[pane], "permission_changed": False, "message": "Settings opened. Choose whether to grant access yourself, then retry the selected location."}

"""Profile-scoped browser review and approved operations via Chrome APIs.

Safari/Firefox privacy and system recent lists have explicit native-UI handoffs.
Their private databases are never rewritten. Browser tabs are not filenames and
cannot enter the filesystem Trash executor.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit

from .common import CleanMacError, digest, display_path, new_id, utc_now
from . import browser_bridge as bridge

TOOL_SPECS = [
    {"name": "browser-bridge", "title": "Chrome companion setup and connected profiles", "feature_ids": ["CM-030", "CM-059", "CM-060"],
     "actions": ["inspect", "plan", "apply"], "requirements": ["Chrome 120+", "user-loaded bundled extension"],
     "configuration": {"extension_id": "32-character ID from chrome://extensions; required for setup plan"}},
    {"name": "browser-privacy", "title": "Browser privacy by profile and category", "feature_ids": ["CM-030"],
     "actions": ["inspect", "plan", "apply"], "configuration": {"browser": "chrome|safari|firefox", "connection": "connected profile ID",
         "category": "history|downloads|cookies|cache|autofill|cache-storage|indexed-db|local-storage|service-workers|file-systems",
         "since_ms": "explicit milliseconds since Unix epoch; zero means all time", "origins": "optional exact HTTP(S) origins where category permits"}},
    {"name": "recent-items", "title": "Recent apps, documents and servers", "feature_ids": ["CM-031"],
     "actions": ["inspect"], "description": "Metadata-only store review and per-list native UI clearing guidance; never deletes referenced documents."},
    {"name": "browser-tabs", "title": "Review, discard, resume, close and reopen tabs", "feature_ids": ["CM-059", "CM-060"],
     "actions": ["inspect", "plan", "apply"], "requirements": ["Connected Chrome companion profile"],
     "configuration": {"connection": "connected profile ID", "operation": "close|discard|resume|reopen",
         "old_after_seconds": "optional age threshold, using observed creation only", "allow_form_loss": "explicit Boolean for potentially lost in-page state",
         "allow_active": "explicit decision for active tabs", "allow_pinned": "explicit decision for pinned tabs", "allow_audible": "explicit decision for audible tabs",
         "retain_reopen_url": "optional explicit retention of a query/fragment-free HTTP(S) URL for recovery"}},
    {"name": "browser-resources", "title": "Browser process CPU and memory", "feature_ids": ["CM-061"],
     "actions": ["inspect"], "configuration": {"browser": "chrome|safari|firefox"},
     "description": "Process totals only. Shared renderers are never counted once per tab."},
]

SOURCES = {
    "tabs": "https://developer.chrome.com/docs/extensions/reference/api/tabs",
    "privacy": "https://developer.chrome.com/docs/extensions/reference/api/browsingData",
    "native_messaging": "https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging",
    "processes": "https://developer.chrome.com/docs/extensions/reference/api/processes",
    "safari_history": "https://support.apple.com/guide/safari/sfri47acf5d6/mac",
    "recent_items": "https://support.apple.com/guide/mac-help/mchlp2724/mac",
}
_CATEGORIES = {"history": "history", "downloads": "downloads", "cookies": "cookies", "cache": "cache",
               "autofill": "formData", "cache-storage": "cacheStorage", "indexed-db": "indexedDB",
               "local-storage": "localStorage", "service-workers": "serviceWorkers", "file-systems": "fileSystems"}
_ORIGIN_TYPES = {"cookies", "cache", "cacheStorage", "indexedDB", "localStorage", "serviceWorkers", "fileSystems"}
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_ROOT_FILES = {
    "chrome": {"history": ["History"], "downloads": ["History"], "cookies": ["Network/Cookies"], "autofill": ["Web Data"],
               "site-storage": ["Local Storage", "IndexedDB", "Service Worker"], "previous-tabs": ["Sessions"], "passwords": ["Login Data"]},
    "firefox": {"history": ["places.sqlite"], "downloads": ["places.sqlite"], "cookies": ["cookies.sqlite"], "autofill": ["formhistory.sqlite"],
                "site-storage": ["storage"], "previous-tabs": ["sessionstore-backups"], "passwords": ["logins.json"]},
    "safari": {"history": ["History.db"], "downloads": ["Downloads.plist"], "cookies": [], "autofill": [],
               "site-storage": ["LocalStorage", "Databases"], "previous-tabs": ["LastSession.plist"], "passwords": []},
}


def _configuration(options):
    config = options.get("configuration", {})
    if not isinstance(config, dict):
        raise CleanMacError("Browser configuration must be an object.", code="invalid_configuration")
    return config


def _browser(config):
    browser = config.get("browser", "chrome")
    if browser not in _ROOT_FILES:
        raise CleanMacError("Choose chrome, safari, or firefox.", code="invalid_configuration")
    return browser


def _macos():
    if sys.platform != "darwin":
        raise CleanMacError("Browser native integration requires macOS.", code="unsupported_platform")


def _skill_root():
    return Path(__file__).resolve().parents[2]


def _registration_path():
    return Path.home() / "Library/Application Support/Google/Chrome/NativeMessagingHosts" / (bridge.HOST_NAME + ".json")


def _setup_details(store, extension_id):
    if not isinstance(extension_id, str) or not re.fullmatch(r"[a-p]{32}", extension_id):
        raise CleanMacError("Use the extension ID shown by Chrome after loading the bundled companion.", code="browser_extension_id_required")
    if len(os.fsencode(store.root / "browser-bridge" / ("c-" + "0" * 16 + ".sock"))) > 100:
        raise CleanMacError("Choose a shorter private CleanMac data directory before installing the browser companion host.", code="browser_bridge_path_length")
    launcher = store.root / "browser-bridge" / "native-host"
    script = _skill_root() / "scripts" / "cleanmac_browser_host.py"
    command = " ".join(shlex.quote(str(item)) for item in (sys.executable, script, "--data-dir", store.root, "--extension-id", extension_id))
    content = "#!/bin/sh\nexec " + command + ' "$@"\n'
    manifest = {"name": bridge.HOST_NAME, "description": "CleanMac local Chrome profile bridge", "path": str(launcher),
                "type": "stdio", "allowed_origins": [f"chrome-extension://{extension_id}/"]}
    return {"extension_id": extension_id, "launcher_path": str(launcher), "launcher_content": content,
            "manifest_path": str(_registration_path()), "manifest": manifest}


def _check_setup(details, store):
    if details != _setup_details(store, details.get("extension_id")):
        raise CleanMacError("Browser setup paths or runtime changed; prepare a new plan.", code="action_scope_mismatch")
    for name in ("manifest_path", "launcher_path"):
        if os.path.lexists(details[name]):
            raise CleanMacError("A browser host registration or launcher already exists. It was preserved; inspect it before choosing another setup action.",
                                code="browser_setup_conflict", details={"path": details[name]})


def _create_private_file(path: Path, content: bytes, mode: int):
    """Create new owned path components and a new file; never follow links."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:-1]:
            try:
                os.mkdir(component, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            info = os.fstat(child)
            if stat.S_IMODE(info.st_mode) & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
                os.close(child)
                raise CleanMacError("A browser setup parent is writable by another user.", code="browser_bridge_unsafe")
            os.close(fd); fd = child
        out = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=fd)
        try:
            with os.fdopen(out, "wb", closefd=False) as destination:
                destination.write(content); destination.flush(); os.fsync(out)
        finally:
            os.close(out)
        os.fsync(fd)
    finally:
        os.close(fd)


def _setup(name, action, options, store, cancel, progress):
    from .actions import create_action_plan, execute_action_plan, checkpoint_action
    config = _configuration(options)
    if action == "inspect":
        profiles = bridge.connections(store)
        return {"tool": name, "status": "available" if profiles else "setup-needed", "profiles": profiles,
                "extension_directory": str(_skill_root() / "assets/browser-extension"), "native_host_manifest": str(_registration_path()),
                "instructions": ["In chrome://extensions enable Developer mode and choose Load unpacked for the bundled extension directory.",
                                 "Copy its extension ID into a browser-bridge setup plan, review the exact host paths, and approve that plan.",
                                 "Open the companion, enter a label for this profile, and press Connect. Select the returned connection ID in CleanMac.",
                                 "Disconnect in the companion or disable/remove it to stop access. No connection starts automatically."],
                "permissions": ["tabs metadata", "browsingData", "local extension storage", "nativeMessaging"],
                "private_browsing": "excluded", "sources": [SOURCES["native_messaging"]]}
    if action == "plan":
        _macos()
        details = _setup_details(store, config.get("extension_id"))
        _check_setup(details, store)
        return create_action_plan(store, name, "setup", details, {"operation": "Register the local Chrome companion host",
            "files": [details["launcher_path"], details["manifest_path"]], "extension_id": details["extension_id"],
            "connection": "Manual Connect in the extension; no startup or privileged service."})
    if action == "apply":
        _macos()
        def validate(plan):
            _check_setup(plan["payload"], store)
        def execute(plan):
            details = plan["payload"]
            checkpoint_action({"setup_paths": [details["launcher_path"], details["manifest_path"]], "phase": "creating-launcher"})
            _create_private_file(Path(details["launcher_path"]), details["launcher_content"].encode(), 0o700)
            checkpoint_action({"created_launcher": details["launcher_path"], "phase": "creating-registration"})
            _create_private_file(Path(details["manifest_path"]), (json.dumps(details["manifest"], sort_keys=True) + "\n").encode(), 0o600)
            return {"status": "completed", "registered": True, "manifest_path": details["manifest_path"],
                    "next": "Press Connect in the companion for the chosen profile."}
        return execute_action_plan(store, options.get("record_id"), options.get("approve"), name, "setup", validate, execute, cancel=cancel, progress=progress)
    raise CleanMacError("Unknown setup action.", code="invalid_action")


def _metadata_roots(roots, browser, cancel=None):
    from .permissions import _open_directory, describe_access_error
    profiles, errors = [], []
    if not isinstance(roots, list) or len(roots) > 32:
        raise CleanMacError("Choose at most 32 explicit browser profile roots.", code="invalid_scope")
    for root in roots:
        if cancel and cancel(): break
        if not isinstance(root, str) or not root or "\x00" in root:
            raise CleanMacError("Choose a valid profile directory.", code="invalid_scope")
        path = Path(os.path.abspath(os.path.expanduser(root)))
        try:
            fd = _open_directory(str(path))
            try:
                entries = []
                for category, relatives in _ROOT_FILES[browser].items():
                    for relative in relatives:
                        parent = os.dup(fd)
                        try:
                            parts = relative.split("/")
                            for part in parts[:-1]:
                                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                                os.close(parent); parent = child
                            info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                            if stat.S_ISLNK(info.st_mode): continue
                            entries.append({"category": category, "store": relative, "logical_bytes": info.st_size if stat.S_ISREG(info.st_mode) else None,
                                            "size_basis": "file metadata only; stores shared by multiple categories must not be summed", "content_read": False})
                        except FileNotFoundError:
                            continue
                        finally:
                            os.close(parent)
                profiles.append({"root": str(path), "display_root": display_path(path), "browser": browser, "stores": entries, "status": "metadata-only"})
            finally:
                os.close(fd)
        except OSError as error:
            errors.append(describe_access_error(str(path), error))
    return profiles, errors


def privacy_matrix(browser="chrome", ready=False):
    categories = [*_CATEGORIES, "search-history", "previous-tabs", "passwords"]
    matrix = []
    for category in categories:
        supported = browser == "chrome" and category in _CATEGORIES
        matrix.append({"category": category, "capability": "supported" if supported and ready else "manual",
                       "automated_status": "available" if supported and ready else ("setup-needed" if supported else "unavailable"),
                       "time_filter": supported, "origin_filter": _CATEGORIES.get(category) in _ORIGIN_TYPES,
                       "reason": ("Password deletion is not supported by current Chrome extensions; use the browser password manager."
                                  if category == "passwords" and browser == "chrome" else
                                  "Review selected tabs separately; session files are never rewritten." if category == "previous-tabs" else
                                  "Search history is part of browser history, not an independently removable store." if category == "search-history" else
                                  "Use the browser's profile-specific privacy controls." if not supported else "Requires the connected profile companion.")})
    return matrix


def _origin(value):
    if not isinstance(value, str):
        raise CleanMacError("Origin must be a URL string.", code="invalid_configuration")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.path or parsed.query
                or parsed.fragment or parsed.username or parsed.password):
            raise ValueError()
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if ":" in host: host = "[" + host + "]"
        port = parsed.port
        default = 80 if parsed.scheme == "http" else 443
        canonical = f"{parsed.scheme}://{host}" + (f":{port}" if port and port != default else "")
        if value != canonical: raise ValueError()
        return value
    except (ValueError, UnicodeError) as error:
        raise CleanMacError("Use canonical HTTP(S) origins without paths, credentials, query, fragment, or default ports.", code="invalid_configuration") from error


def _connection(config, store):
    connection = config.get("connection")
    if not connection:
        raise CleanMacError("Connect and explicitly select a Chrome profile first.", code="browser_connection_required",
                            details={"next": "browser-bridge inspect"})
    return connection, bridge.call_bridge(store, connection, "describe")


def _safe_url(url):
    if not isinstance(url, str) or len(url) > 32768:
        raise CleanMacError("Browser returned an unsupported URL.", code="browser_bridge_protocol")
    try:
        parts = urlsplit(url)
        if parts.scheme in {"http", "https"} and parts.hostname:
            # Do not expose authentication query strings, fragments or userinfo.
            host = parts.hostname + (f":{parts.port}" if parts.port else "")
            shown = f"{parts.scheme}://{host}{parts.path}"
            if parts.query: shown += "?[redacted]"
            if parts.fragment: shown += "#[redacted]"
            safe = not parts.username and not parts.password and not parts.query and not parts.fragment
            return display_path(shown), url if safe else None
    except ValueError:
        pass
    return "[non-web URL]", None


def _sanitize_tabs(value, retain=False, threshold=None):
    if not isinstance(value.get("items"), list) or len(value["items"]) > 2500:
        raise CleanMacError("Invalid tab response.", code="browser_bridge_protocol")
    items = []
    now_ms = time.time() * 1000
    for tab in value["items"]:
        if (not isinstance(tab, dict) or type(tab.get("tab_id")) is not int or type(tab.get("window_id")) is not int
                or tab.get("incognito") is not False or not isinstance(tab.get("url"), str)
                or not isinstance(tab.get("identity"), str) or not re.fullmatch(r"[0-9a-f-]{36}", tab["identity"])
                or type(tab.get("revision")) is not int or tab["revision"] < 0):
            raise CleanMacError("Tab metadata did not establish a non-private profile scope.", code="browser_bridge_protocol")
        url_digest = hashlib.sha256(tab["url"].encode()).hexdigest()
        if tab.get("url_digest") != url_digest:
            raise CleanMacError("Browser URL fingerprint is inconsistent.", code="browser_bridge_protocol")
        shown, reopen = _safe_url(tab["url"])
        created = tab.get("observed_at")
        known_age = tab.get("age_source") == "creation-observed" and type(created) in (int, float) and math.isfinite(created) and 0 <= created <= now_ms
        age = (now_ms - created) / 1000 if known_age else None
        item = {"id": f"tab-{tab['tab_id']}", "tab_id": tab["tab_id"], "window_id": tab["window_id"], "url_digest": url_digest,
                "identity": tab["identity"], "revision": tab["revision"],
                "display_url": shown, "title": display_path(str(tab.get("title", ""))[:300]),
                "active": tab.get("active") is True, "pinned": tab.get("pinned") is True, "audible": tab.get("audible") is True,
                "discarded": tab.get("discarded") is True, "auto_discardable": tab.get("auto_discardable") is True,
                "form_state": "unknown", "age_seconds": age, "age_source": "creation-observed" if known_age else "unknown-before-observation",
                "old": age >= threshold if age is not None and threshold is not None else None, "selected": False}
        if retain and reopen:
            item["reopen_url"] = reopen
        items.append(item)
    groups = {}
    for item in items:
        groups.setdefault(item["url_digest"], []).append(item["id"])
    return items, [{"url_digest": key, "item_ids": ids, "selected_keeper": None} for key, ids in groups.items() if len(ids) > 1]


def _tabs_inspect(config, store, cancel=None):
    connection, profile = _connection(config, store)
    if cancel and cancel(): return {"tool": "browser-tabs", "status": "cancelled", "items": []}
    threshold = config.get("old_after_seconds")
    if threshold is not None and (type(threshold) not in (int, float) or not math.isfinite(threshold) or threshold < 0):
        raise CleanMacError("Old-tab threshold must be nonnegative seconds.", code="invalid_configuration")
    value = bridge.call_bridge(store, connection, "tabs-inspect", {"max_tabs": config.get("max_tabs", 1000)})
    if any(value.get(key) != profile.get(key) for key in ("profile_id", "session_id")):
        raise CleanMacError("The connected browser profile changed.", code="browser_target_changed")
    items, groups = _sanitize_tabs(value, retain=config.get("retain_reopen_url") is True, threshold=threshold)
    return store.put("scans", {"schema_version": 1, "id": new_id("scan-browser"), "created_at": utc_now(), "tool": "browser-tabs",
        "status": "complete" if value.get("complete") is True else "partial", "connection": connection,
        "profile_id": profile["profile_id"], "session_id": profile["session_id"], "profile_label": display_path(profile.get("label", "Chrome profile")),
        "items": items, "duplicate_groups": groups, "private_tabs": "excluded", "preexisting_age": "unknown",
        "url_retention": "safe reopen URLs only, by explicit request" if config.get("retain_reopen_url") else "URL fingerprints; query strings and fragments omitted",
        "recovery_limits": "Reopening a URL cannot restore unsaved forms, page state or closed window context."})


def _plan_tabs(options, store):
    from .actions import create_action_plan, validate_action_receipt
    config = _configuration(options)
    operation = config.get("operation")
    if operation not in {"close", "discard", "resume", "reopen"}:
        raise CleanMacError("Choose close, discard, resume, or reopen.", code="invalid_configuration")
    command = "tab-" + operation
    if operation == "reopen":
        stored_receipt = store.get("receipts", options.get("record_id"))
        original_plan = store.get("plans", stored_receipt.get("plan_id"))
        receipt = validate_action_receipt(stored_receipt, original_plan)
        if original_plan.get("tool") != "browser-tabs" or original_plan.get("action") != "tab-close":
            raise CleanMacError("Choose a verified browser closure receipt.", code="browser_recovery_unavailable")
        if receipt.get("status") != "completed":
            raise CleanMacError("Only a verified completed closure can be reopened.", code="browser_recovery_unavailable")
        saved = receipt["plan_snapshot"]["payload"]
        original = saved["arguments"]
        url = original.get("reopen_url")
        if not isinstance(url, str) or _safe_url(url)[1] != url:
            raise CleanMacError("No safe URL was retained for this closure. Use Chrome's Recently Closed history manually.", code="browser_recovery_unavailable")
        connection = saved["connection"]
        profile = bridge.call_bridge(store, connection, "describe")
        if any(profile.get(k) != original.get(k) for k in ("profile_id", "session_id")):
            raise CleanMacError("The original browser session is unavailable; do not reopen into another profile.", code="browser_target_changed")
        arguments = {"profile_id": profile["profile_id"], "session_id": profile["session_id"], "url": url, "window_id": original["tab"]["window_id"]}
        summary = {"operation": "Reopen this retained URL in its original profile/window", "display_url": _safe_url(url)[0], "recovery": "URL only; unsaved state is not restored."}
    else:
        scan = store.get("scans", options.get("record_id"))
        if scan.get("tool") != "browser-tabs":
            raise CleanMacError("Choose a saved browser-tabs inspection.", code="invalid_selection")
        selected = options.get("selected", [])
        if not isinstance(selected, list) or len(selected) != 1:
            raise CleanMacError("Choose one exact tab per plan so every operation has its own recoverable receipt.", code="invalid_selection")
        item = next((item for item in scan["items"] if item["id"] == selected[0]), None)
        if item is None: raise CleanMacError("The selected tab is absent from that inspection.", code="invalid_selection")
        decisions = {key: config.get(key) is True for key in ("allow_active", "allow_pinned", "allow_audible", "allow_form_loss")}
        for field in ("active", "pinned", "audible"):
            if item[field] and not decisions["allow_" + field]:
                raise CleanMacError(f"Explicitly decide whether to act on this {field} tab.", code="browser_state_decision_required")
        if not decisions["allow_form_loss"]:
            raise CleanMacError("Acknowledge possible loss of unsaved forms and in-page state for the selected tab.", code="browser_state_decision_required")
        if operation == "discard" and (item["active"] or item["discarded"] or not item["auto_discardable"]):
            raise CleanMacError("Chrome cannot discard this selected tab in its current state.", code="browser_action_unavailable")
        if operation == "resume" and not item["discarded"]:
            raise CleanMacError("The selected tab is not discarded.", code="browser_action_unavailable")
        connection = scan["connection"]
        arguments = {"profile_id": scan["profile_id"], "session_id": scan["session_id"],
                     "tab": {key: item[key] for key in ("tab_id", "window_id", "url_digest", "identity", "revision")}, "decisions": decisions}
        if operation == "close" and item.get("reopen_url"):
            arguments["reopen_url"] = item["reopen_url"]
        summary = {"operation": operation, "profile": scan["profile_label"], "item": item,
                   "decisions": decisions, "recovery": "Only the retained safe URL can be reopened; in-page state cannot be restored.",
                   "resume_activates_tab": operation == "resume"}
    return create_action_plan(store, "browser-tabs", command, {"connection": connection, "command": command, "arguments": arguments}, summary)


def _privacy(name, action, options, store, cancel, progress):
    from .actions import create_action_plan
    config = _configuration(options)
    browser = _browser(config)
    if action == "inspect":
        profiles, errors = _metadata_roots(options.get("roots", []), browser, cancel)
        status, connected = "manual", None
        if browser == "chrome" and config.get("connection"):
            connection, _ = _connection(config, store)
            connected = bridge.call_bridge(store, connection, "privacy-inspect")
            status = "available"
        matrix = privacy_matrix(browser, connected is not None)
        if connected:
            permitted = connected.get("removal_permitted", {})
            for row in matrix:
                if isinstance(permitted, dict) and permitted.get(_CATEGORIES.get(row["category"])) is False:
                    row.update(capability="unavailable", automated_status="unavailable", reason="The browser's current policy does not permit this removal.")
        return {"tool": name, "status": "cancelled" if cancel and cancel() else "partial" if errors else status, "browser": browser, "profiles": profiles, "errors": errors,
                "categories": matrix, "connected_profile": connected,
                "handoff": {"chrome": "Open Settings > Privacy and security > Delete browsing data in the selected profile.",
                            "safari": "Use Safari History > Clear History or Settings > Privacy > Manage Website Data; review the time range and sync effects.",
                            "firefox": "Use Firefox Settings > Privacy & Security in the selected profile; choose the exact categories and time range."}[browser],
                "consequences": ["Cookies and site storage can sign you out or remove offline site state.", "Synced history changes can propagate to other devices.",
                                 "Download history cleanup does not delete downloaded files.", "Bookmarks, passwords, extensions and protected web apps are excluded from automated actions."],
                "sources": [SOURCES["privacy"], SOURCES["safari_history"]]}
    if action == "plan":
        if browser != "chrome":
            raise CleanMacError("This browser's privacy cleanup uses its native controls; no private databases are rewritten.", code="browser_action_unavailable")
        connection, profile = _connection(config, store)
        category = config.get("category")
        if category not in _CATEGORIES:
            raise CleanMacError("Choose one supported privacy category; passwords and session state require separate browser controls.", code="browser_action_unavailable")
        since = config.get("since_ms")
        if type(since) not in (int, float) or not math.isfinite(since) or not 0 <= since <= time.time() * 1000:
            raise CleanMacError("Specify since_ms explicitly; zero means all time.", code="invalid_configuration")
        origins = config.get("origins", [])
        if not isinstance(origins, list) or len(origins) > 100 or (origins and _CATEGORIES[category] not in _ORIGIN_TYPES):
            raise CleanMacError("This category does not support the requested origin filter.", code="browser_action_unavailable")
        for origin in origins:
            _origin(origin)
        arguments = {"profile_id": profile["profile_id"], "session_id": profile["session_id"], "types": [_CATEGORIES[category]], "since_ms": since, "origins": origins}
        return create_action_plan(store, name, "privacy-clear", {"connection": connection, "command": "privacy-clear", "arguments": arguments},
            {"operation": "Clear one browser privacy category", "profile": display_path(profile.get("label", "Chrome profile")), "category": category,
             "since_ms": since, "origins": origins or "all unprotected web origins", "irreversible": True,
             "consequences": ["No undo is provided for removed browser history/site data.", "Site logins and offline state may be lost; sync can propagate changes.",
                              "Cookie origin selection includes cookies for the registrable domain.", "Downloaded files, bookmarks, passwords and extension data remain outside this action."]})
    if action == "apply": return _apply_browser(name, options, store, cancel, progress)
    raise CleanMacError("Unknown browser privacy action.", code="invalid_action")


def _apply_browser(name, options, store, cancel, progress):
    from .actions import execute_action_plan, validate_action_plan
    candidate = store.get("plans", options.get("record_id"))
    command = candidate.get("action")
    if command not in bridge.MUTATIONS or (name == "browser-privacy") != (command == "privacy-clear"):
        raise CleanMacError("The selected plan belongs to another browser action.", code="action_scope_mismatch")
    def validate(plan):
        value = validate_action_plan(plan, tool=name, action=command)["payload"]
        if set(value) != {"connection", "command", "arguments"} or value["command"] != command:
            raise CleanMacError("Invalid browser plan payload.", code="action_scope_mismatch")
        profile = bridge.call_bridge(store, value["connection"], "describe")
        arguments = value["arguments"]
        if any(profile.get(key) != arguments.get(key) for key in ("profile_id", "session_id")):
            raise CleanMacError("The browser profile restarted or changed. Inspect it again.", code="browser_target_changed")
        if command in {"tab-close", "tab-discard", "tab-resume"}:
            response = bridge.call_bridge(store, value["connection"], "tabs-inspect", {"max_tabs": 2500})
            items, _ = _sanitize_tabs(response)
            expected = arguments["tab"]
            found = next((tab for tab in items if tab["tab_id"] == expected["tab_id"]), None)
            if not found or any(found[k] != expected[k] for k in ("window_id", "url_digest", "identity", "revision")):
                raise CleanMacError("The selected tab navigated or was replaced.", code="browser_target_changed")
            for field in ("active", "pinned", "audible"):
                if found[field] and arguments["decisions"].get("allow_" + field) is not True:
                    raise CleanMacError("The selected tab's protected state changed.", code="browser_target_changed")
    def execute(plan):
        payload = plan["payload"]
        return bridge.call_bridge(store, payload["connection"], command, payload["arguments"],
                                  approval={"plan_id": plan["id"], "digest": plan["digest"]})
    return execute_action_plan(store, options.get("record_id"), options.get("approve"), name, command, validate, execute, cancel=cancel, progress=progress)


def _recent(options, cancel=None):
    from .permissions import _open_directory, describe_access_error
    entries, errors, truncated = [], [], False
    roots = options.get("roots", [])
    if not isinstance(roots, list) or len(roots) > 32: raise CleanMacError("Select at most 32 recent-list directories.", code="invalid_scope")
    for raw in roots:
        if cancel and cancel(): break
        if not isinstance(raw, str) or not raw or "\x00" in raw: raise CleanMacError("Invalid recent-list root.", code="invalid_scope")
        path = os.path.abspath(os.path.expanduser(raw))
        try:
            fd = _open_directory(path)
            try:
                with os.scandir(fd) as children:
                    for index, child in enumerate(children):
                        if index >= 10000:
                            truncated = True
                            break
                        if not child.name.endswith((".sfl", ".sfl2", ".sfl3")): continue
                        info = child.stat(follow_symlinks=False)
                        if stat.S_ISREG(info.st_mode):
                            entries.append({"id": digest([path, child.name, info.st_ino]), "display_name": display_path(child.name),
                                            "logical_bytes": info.st_size, "content_read": False, "format": "private-versioned-store; not rewritten"})
            finally: os.close(fd)
        except OSError as error: errors.append(describe_access_error(path, error))
    return {"tool": "recent-items", "status": "cancelled" if cancel and cancel() else "partial" if errors or truncated else "manual",
            "stores": entries, "errors": errors, "truncated": truncated, "coverage": "selected-root metadata only",
            "lists": [{"name": "System recent applications/documents/servers", "clear": "Apple menu > Recent Items > Clear Menu"},
                      {"name": "One application's recent documents", "clear": "In that application, File > Open Recent > Clear Menu, when available"},
                      {"name": "Finder recent folders", "clear": "Finder > Go > Recent Folders > Clear Menu, when available"}],
            "automation": "unavailable; no undocumented store rewriting", "documents_changed": False, "sources": [SOURCES["recent_items"]]}


def _process_metrics(config):
    _macos()
    browser = _browser(config)
    try:
        result = subprocess.run(["/bin/ps", "-axo", "pid=,ppid=,%cpu=,rss=,comm="], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CleanMacError("Process totals could not be sampled.", code="browser_metrics_unavailable") from error
    if result.returncode != 0 or len(result.stdout) > 8 * 1024 * 1024:
        raise CleanMacError("Process totals were incomplete.", code="browser_metrics_unavailable")
    tokens = {"chrome": ("/Google Chrome.app/",), "firefox": ("/Firefox.app/",), "safari": ("/Safari.app/",)}[browser]
    processes = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 4)
        if len(parts) != 5 or not any(token in parts[4] for token in tokens): continue
        try:
            pid, parent, cpu, rss = int(parts[0]), int(parts[1]), float(parts[2]), int(parts[3])
        except ValueError: continue
        if not math.isfinite(cpu) or cpu < 0 or rss < 0: continue
        processes.append({"pid": pid, "parent_pid": parent, "cpu_percent": cpu, "resident_bytes": rss * 1024,
                          "process": display_path(Path(parts[4]).name), "tab_attribution": "unknown"})
    unique = {item["pid"]: item for item in processes}
    return {"tool": "browser-resources", "status": "available", "browser": browser, "sampled_at": utc_now(), "scope": "matching browser application bundle processes",
            "processes": list(unique.values()), "cpu_percent": sum(item["cpu_percent"] for item in unique.values()),
            "resident_bytes_sum": sum(item["resident_bytes"] for item in unique.values()),
            "cpu_window": "ps lifetime-decayed CPU estimate, not a fixed instantaneous sampling interval",
            "memory_basis": "RSS summed once per PID; shared pages may appear in multiple processes; not unique physical bytes",
            "per_tab": "unavailable", "safari_shared_webkit": "shared WebKit processes outside the Safari bundle are excluded, not guessed",
            "handoff": "Use Chrome Task Manager or macOS Activity Monitor for interactive attribution.", "sources": [SOURCES["processes"]]}


def dispatch(name, action, options, *, store, cancel=None, progress=None):
    if not isinstance(options, dict): raise CleanMacError("Tool options must be an object.", code="invalid_configuration")
    spec = next((item for item in TOOL_SPECS if item["name"] == name), None)
    if not spec or action not in spec["actions"]: raise CleanMacError("Unknown browser tool action.", code="invalid_action")
    if name == "browser-bridge": return _setup(name, action, options, store, cancel, progress)
    if name == "browser-privacy": return _privacy(name, action, options, store, cancel, progress)
    if name == "recent-items": return _recent(options, cancel)
    if name == "browser-resources": return _process_metrics(_configuration(options))
    if action == "inspect": return _tabs_inspect(_configuration(options), store, cancel)
    if action == "plan": return _plan_tabs(options, store)
    return _apply_browser(name, options, store, cancel, progress)

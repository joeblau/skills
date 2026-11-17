"""Scoped local cleanup adapters and supported model/developer operations.

No discovery here runs at import time. File mutations go through the existing
content-bound Trash executor; managed resources use their owning service API.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import shutil
import stat
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from xml.parsers.expat import ExpatError

from .common import CleanMacError, digest, display_path, new_id, utc_now
from . import inventory, rules


_MAX_METADATA = 8 * 1024 * 1024
_UUID = re.compile(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")


def _spec(name, title, ids, actions=("inspect",), **extra):
    return {"name": name, "title": title, "feature_ids": ids, "actions": list(actions), **extra}


TOOL_SPECS = [
    _spec("user-caches", "Review user caches", ["CM-011"], ("inspect", "plan", "apply"), scope_required=True,
          description="Reviewed cache rules; unknown formats and offline app data remain report-only."),
    _spec("system-caches", "Inspect system cache scope", ["CM-012"], scope_required=True,
          description="Metadata-only: no validated OS-specific removal rule or privileged helper is installed."),
    _spec("user-logs", "Review archived user diagnostics", ["CM-013"], ("inspect", "plan", "apply"), scope_required=True,
          configuration={"retention_days": "Minimum 30; default 30", "cutoff": "Optional ISO 8601 cutoff with timezone"},
          description="Old, inactive, documented JSON crash reports support recoverable Trash; ordinary and active logs stay protected."),
    _spec("system-logs", "Inspect archived system diagnostics", ["CM-014"], scope_required=True,
          description="System diagnostics are report-only; unified, audit, and security log stores are excluded."),
    _spec("preferences", "Validate preference files", ["CM-016"], scope_required=True,
          description="Bounded XML/binary plist validation; reset uses the owning app's supported controls."),
    _spec("home-archives", "Review archived user homes", ["CM-017"], scope_required=True,
          description="Metadata-only disk-image review with current account checks; backups are never automatic junk."),
    _spec("device-backups", "Review local device backups", ["CM-019"], scope_required=True,
          description="Read backup metadata without decrypting payloads; preserve newest and incomplete backups and hand off to Finder."),
    _spec("xcode-cleanup", "Review developer resources", ["CM-022"], ("inspect", "plan", "apply"),
          configuration={"developer_dir": "Optional absolute selected Developer directory", "simulators": "true to inspect simctl-managed devices"},
          description="Scoped category inventory and one selected unavailable, shutdown simulator deletion through verified simctl."),
    _spec("ai-data", "Review AI tool storage", ["CM-023"], scope_required=True,
          description="Preserves current agent state, conversations, credentials, skills, and unknown layouts; no generic hidden-directory deletion."),
    _spec("models", "Review local AI models", ["CM-024"], ("inspect", "plan", "apply"),
          configuration={"provider": "ollama (default), lmstudio, or huggingface", "endpoint": "Optional Ollama literal loopback HTTP URL"},
          description="Ollama inventory and explicit unloaded-model removal through its API; other providers have inspect/manual workflows."),
]


def _configuration(options):
    value = options.get("configuration", {})
    if not isinstance(value, dict):
        raise CleanMacError("Tool configuration must be an object.", "invalid_configuration")
    return value


def _roots(options, *, required=True):
    values = options.get("roots", [])
    if not isinstance(values, list) or any(not isinstance(v, str) or not v or "\x00" in v for v in values):
        raise CleanMacError("Choose explicit filesystem roots.", "invalid_scope")
    result = list(dict.fromkeys(inventory._canonical_root(v) for v in values))
    if required and not result:
        raise CleanMacError("Choose the exact folders to inspect; no personal locations are scanned implicitly.", "scope_required")
    return result


def _record(tool):
    return {"id": new_id("scan"), "schema_version": 1, "created_at": utc_now(), "tool": tool,
            "status": "complete", "items": [], "findings": [], "errors": [], "limitations": [],
            "roots": [], "cleanup_performed": False}


def _finish(store, record):
    if record["errors"] and record["status"] == "complete":
        record["status"] = "partial" if record["items"] else "unavailable"
    record.setdefault("summary", {})["items_found"] = len(record["items"])
    return store.put("scans", record)


def _error(path, error):
    if isinstance(error, OSError):
        from .permissions import describe_access_error
        return describe_access_error(path, error)
    return {"path": path, "code": getattr(error, "code", "inspection_failed"), "message": str(error)}


def _cancel(record, cancel):
    if cancel and cancel():
        record["status"] = "cancelled"
        return True
    return False


def _retention(configuration):
    days = configuration.get("retention_days", 30)
    if type(days) is not int or not 30 <= days <= 36500:
        raise CleanMacError("retention_days must be an integer from 30 to 36500.", "invalid_configuration")
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    value = configuration.get("cutoff")
    if value is not None:
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed > cutoff:
                raise ValueError()
            cutoff = parsed.astimezone(dt.timezone.utc)
        except (AttributeError, TypeError, ValueError):
            raise CleanMacError("cutoff must include a timezone and retain at least retention_days of diagnostics.", "invalid_configuration")
    return cutoff


def _scan_files(name, options, store, cancel, progress):
    roots = _roots(options)
    configuration = _configuration(options)
    limit = configuration.get("max_items", 100000)
    if type(limit) is not int or not 1 <= limit <= 1000000:
        raise CleanMacError("max_items must be from 1 to 1000000.", "invalid_configuration")
    excluded = [str(store.root), *store.get_settings().get("ignore_paths", [])]
    scan = inventory.scan_roots(roots, max_items=limit, cancel=cancel, progress=progress, exclude_paths=excluded)
    scan.update(tool=name, created_at=utc_now(), schema_version=1, cleanup_performed=False, limitations=[])
    findings = rules.classify_items(scan["items"], cancel=cancel) if scan["status"] != "cancelled" else []
    by_item = {f["item_id"]: f for f in findings}
    cutoff = _retention(configuration) if name in {"user-logs", "system-logs"} else None
    candidates = []
    for item in scan["items"]:
        if _cancel(scan, cancel):
            break
        path = Path(item["path"])
        eligible = bool(by_item.get(item["id"], {}).get("eligible"))
        reason = by_item.get(item["id"], {}).get("reason", item.get("protection_reason"))
        if name in {"user-logs", "system-logs"}:
            if item["kind"] != "file" or not (path.suffix.casefold() in {".ips", ".crash", ".log", ".gz", ".diag"} or ".log." in path.name):
                continue
            eligible = eligible and by_item.get(item["id"], {}).get("rule_id") == "user-ips-crash-report"
            if item["mtime_ns"] / 1e9 >= cutoff.timestamp():
                eligible, reason = False, "Retained by the selected cutoff; dates use UTC."
            if name == "system-logs":
                eligible, reason = False, "No OS-specific removal handler is validated; system, audit, and unified logs remain under macOS control."
        if name == "system-caches":
            eligible, reason = False, "No OS-specific system cache regeneration/removal rule is validated."
        if name == "ai-data":
            eligible, reason = False, "Unknown or non-regenerable agent data remains protected; use the owning tool's storage controls."
        if item["kind"] != "file" and name not in {"ai-data", "system-caches"}:
            continue
        item.update(eligible=eligible, selected=False, cleanup_reason=reason or "Review only; no validated removal rule.")
        candidates.append(item)
    scan["items"] = candidates
    ids = {i["id"] for i in candidates}
    scan["findings"] = [f for f in findings if f["item_id"] in ids]
    # Retain the adapter's narrower selection policy in the saved scan as well
    # as the UI flags. Generic `plan SCAN_ID` must not bypass report-only or
    # stricter-retention decisions by ignoring eligible_ids.
    eligibility = {i["id"]: i for i in candidates}
    for finding in scan["findings"]:
        item = eligibility[finding["item_id"]]
        if not item["eligible"]:
            finding.update(eligible=False, reason=item["cleanup_reason"], confidence="report-only")
    scan["eligible_ids"] = [i["id"] for i in candidates if i["eligible"]]
    scan["summary"] = {"eligible_files": len(scan["eligible_ids"]), "reclaimable_bytes": None,
                       "space_note": "Trash retains bytes; allocation is not uniquely reclaimable storage."}
    if cutoff:
        scan["retention_cutoff_utc"] = cutoff.isoformat()
    if name in {"system-caches", "system-logs"}:
        scan["limitations"].append("Protected or inaccessible system trees are reported as unscanned; no escalation or blanket erase is attempted.")
    return _finish(store, scan)


def _file_plan(name, options, store, cancel, progress):
    from .plans import make_plan
    scan = store.get("scans", options.get("record_id"))
    if scan.get("tool") != name:
        raise CleanMacError("Use a scan from this cleanup tool.", "wrong_record")
    selected = options.get("selected", [])
    if not isinstance(selected, list) or not selected or any(v not in scan.get("eligible_ids", []) for v in selected):
        raise CleanMacError("Select exact eligible files from this scan; report-only entries cannot be planned.", "ineligible_item")
    plan = make_plan(scan, selected, cancel=cancel, progress=progress)
    return store.put("plans", plan)


def _file_apply(name, options, store, cancel):
    from .executor import apply_plan
    plan = store.get("plans", options.get("record_id"))
    scan = store.get("scans", plan.get("scan_id"))
    if scan.get("tool") != name or any(i["id"] not in scan.get("eligible_ids", []) for i in plan.get("items", [])):
        raise CleanMacError("The approved plan does not belong to this cleanup tool.", "wrong_record")
    return apply_plan(plan, approval_digest=options.get("approve"), store=store,
                      native_path=os.environ.get("CLEANMAC_NATIVE_PATH"), cancel=cancel)


def _read_file_at(fd, name, *, max_bytes=_MAX_METADATA):
    before = os.stat(name, dir_fd=fd, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > max_bytes or getattr(before, "st_flags", 0) & inventory.DATALESS_FLAG:
        raise CleanMacError("Metadata is linked, oversized, cloud-only, or not a regular file.", "unsupported_metadata")
    file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        if inventory._stat_values(os.fstat(file_fd)) != inventory._stat_values(before):
            raise CleanMacError("Metadata changed during open.", "stale_item")
        parts, length = [], 0
        while length <= before.st_size:
            block = os.read(file_fd, min(65536, before.st_size - length + 1))
            if not block:
                break
            parts.append(block)
            length += len(block)
        if length != before.st_size or inventory._stat_values(os.fstat(file_fd)) != inventory._stat_values(before):
            raise CleanMacError("Metadata changed during inspection.", "stale_item")
        return b"".join(parts)
    finally:
        os.close(file_fd)


def _preference_report(options, store, cancel, progress):
    scan = _scan_files("preferences", options, store, cancel, progress)
    result = []
    for item in scan["items"]:
        if _cancel(scan, cancel):
            break
        path = Path(item["path"])
        if path.suffix != ".plist":
            continue
        entry = dict(item, eligible=False, validation="unverified", owning_app="unverified",
                     reset="Use the owning app's supported reset after quitting it and preserving an original backup; no direct CFPreferences file edits.")
        if item.get("protected") or path.name.startswith("com.apple.") or any(word in path.name.casefold() for word in ("auth", "security", "keychain", "account", "profile", "mdm")):
            entry["validation"] = "protected"
        else:
            fd = None
            try:
                fd, _ = inventory._open_parent(item["path"], item["ancestors"])
                raw = _read_file_at(fd, path.name)
                # The read above guards format reads; compare to the inventory
                # afterward as well so a replaced preference is not diagnosed.
                inventory.assert_item_identity(item)
                try:
                    plistlib.loads(raw)
                    entry["validation"] = "valid"
                except (plistlib.InvalidFileException, ValueError, TypeError, OverflowError, UnicodeError, ExpatError, RecursionError) as error:
                    entry.update(validation="malformed", parser_error=type(error).__name__, content_sha256=hashlib.sha256(raw).hexdigest(),
                                 cleanup_reason="Malformed plist; corruption alone does not establish app ownership or authorize reset.")
            except (OSError, CleanMacError) as error:
                scan["errors"].append(_error(item["path"], error))
            finally:
                if fd is not None:
                    os.close(fd)
        result.append(entry)
    scan["items"], scan["eligible_ids"], scan["findings"] = result, [], []
    scan["summary"] = {"malformed": sum(i["validation"] == "malformed" for i in result), "reset_supported": False}
    return _finish(store, scan)


def _account_homes():
    try:
        return {entry.pw_name: os.path.normpath(entry.pw_dir) for entry in pwd.getpwall() if entry.pw_dir.startswith("/")}
    except (OSError, KeyError):
        return None


def _archive_report(options, store, cancel):
    from .permissions import _open_directory
    report = _record("home-archives")
    report["roots"] = _roots(options)
    accounts = _account_homes()
    report["account_metadata"] = "available" if accounts is not None else "unverified"
    for root in report["roots"]:
        if _cancel(report, cancel):
            break
        fd = None
        try:
            fd = _open_directory(root)
            with os.scandir(fd) as children:
                for index, child in enumerate(children):
                    if _cancel(report, cancel):
                        break
                    if index >= 10000:
                        report["status"] = "partial"
                        report["limitations"].append("Archive entry limit reached.")
                        break
                    info = child.stat(follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or Path(child.name).suffix.casefold() not in {".dmg", ".sparseimage"}:
                        continue
                    path = os.path.join(root, child.name)
                    stem = Path(child.name).stem
                    matches = sorted(name for name, home in (accounts or {}).items()
                                     if stem == name or path == home or home.startswith(path + os.sep))
                    report["items"].append({"id": "archive-" + digest({"path": path, **inventory._stat_values(info)})[:24],
                        "path": path, "display_path": display_path(path), "uid": info.st_uid,
                        "logical_size": info.st_size, "allocated_size": info.st_blocks * 512,
                        "mtime_ns": info.st_mtime_ns, "account_matches": matches,
                        "account_status": "matching-account" if matches else "no-name-match" if accounts is not None else "unverified",
                        "eligible": False, "selected": False, "reason": "User-home backup: no mounting, content inspection, or automatic deletion; losing it may permanently lose personal data."})
        except (OSError, CleanMacError) as error:
            report["errors"].append(_error(root, error))
        finally:
            if fd is not None:
                os.close(fd)
    report["limitations"] = ["A filename/account mismatch does not prove a former account was deleted. Review the owner and backup independently before using Finder."]
    return _finish(store, report)


def _plist_value(fd, name):
    value = plistlib.loads(_read_file_at(fd, name))
    if not isinstance(value, dict):
        raise ValueError("Backup metadata is not a plist dictionary")
    return value


def _date(value):
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value.astimezone(dt.timezone.utc)
    return None


def _backup_report(options, store, cancel):
    from .permissions import _open_directory
    report = _record("device-backups")
    report["roots"] = _roots(options)
    for root in report["roots"]:
        if _cancel(report, cancel):
            break
        fd = None
        try:
            fd = _open_directory(root)
            root_info = os.fstat(fd)
            with os.scandir(fd) as children:
                for index, child in enumerate(children):
                    if _cancel(report, cancel):
                        break
                    if index >= 10000:
                        report["status"] = "partial"
                        break
                    info = child.stat(follow_symlinks=False)
                    if not stat.S_ISDIR(info.st_mode) or info.st_dev != root_info.st_dev or getattr(info, "st_flags", 0) & inventory.DATALESS_FLAG:
                        continue
                    path = os.path.join(root, child.name)
                    entry = {"id": "backup-" + digest({"path": path, "device": info.st_dev, "inode": info.st_ino})[:24],
                             "path": path, "display_path": display_path(path), "eligible": False, "selected": False,
                             "state": "unverified", "latest_known": False, "protected": True}
                    child_fd = None
                    try:
                        child_fd = os.open(child.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                        if (os.fstat(child_fd).st_dev, os.fstat(child_fd).st_ino) != (info.st_dev, info.st_ino):
                            raise CleanMacError("Backup root changed during open.", "stale_item")
                        metadata = _plist_value(child_fd, "Info.plist")
                        manifest = _plist_value(child_fd, "Manifest.plist")
                        status = _plist_value(child_fd, "Status.plist")
                        device_id = metadata.get("Target Identifier", metadata.get("Unique Identifier"))
                        when = _date(metadata.get("Last Backup Date")) or _date(status.get("Date"))
                        complete = status.get("SnapshotState") == "finished" and when is not None and isinstance(device_id, str) and bool(device_id)
                        entry.update(device_id=device_id if isinstance(device_id, str) else None,
                                     device_name=metadata.get("Device Name") if isinstance(metadata.get("Device Name"), str) else None,
                                     backup_date=when.isoformat() if when else None, encrypted=manifest.get("IsEncrypted") if type(manifest.get("IsEncrypted")) is bool else None,
                                     state="metadata-finished" if complete else "incomplete-or-unknown",
                                     reason="Backup formats and active-backup detection are not a public mutation contract; use Finder Manage Backups. No payload was opened or decrypted.")
                    except (OSError, CleanMacError, ValueError, plistlib.InvalidFileException, OverflowError, ExpatError, RecursionError) as error:
                        report["errors"].append(_error(path, error))
                    finally:
                        if child_fd is not None:
                            os.close(child_fd)
                    report["items"].append(entry)
        except (OSError, CleanMacError) as error:
            report["errors"].append(_error(root, error))
        finally:
            if fd is not None:
                os.close(fd)
    latest = {}
    for item in report["items"]:
        if item["state"] == "metadata-finished":
            latest[item["device_id"]] = max(latest.get(item["device_id"], ""), item["backup_date"])
    for item in report["items"]:
        item["latest_known"] = bool(item.get("device_id") and item.get("backup_date") == latest.get(item["device_id"]))
    report["handoff"] = {"application": "Finder", "steps": ["Connect and select the device", "General → Manage Backups", "Review Delete, Archive, or Show in Finder"],
                          "source": "https://support.apple.com/108809"}
    return _finish(store, report)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise CleanMacError("Local model API redirects are not followed.", "unsafe_endpoint")


def _endpoint(configuration):
    value = configuration.get("endpoint", "http://127.0.0.1:11434")
    if not isinstance(value, str):
        raise CleanMacError("Choose a literal loopback Ollama HTTP endpoint.", "unsafe_endpoint")
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"} or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or not (1 <= (parsed.port or 80) <= 65535):
            raise ValueError()
    except ValueError:
        raise CleanMacError("Only literal loopback HTTP endpoints without credentials or paths are supported.", "unsafe_endpoint")
    return value.rstrip("/")


def _http_json(endpoint, path, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(endpoint + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        # Explicitly disable ambient proxies: loopback traffic must stay local.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=8) as response:
            raw = response.read(_MAX_METADATA + 1)
            if len(raw) > _MAX_METADATA:
                raise CleanMacError("The local model API exceeded its response budget.", "response_too_large")
            value = json.loads(raw) if raw else {}
            if not isinstance(value, dict):
                raise ValueError()
            return value
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise CleanMacError("The selected local model API could not return a valid bounded response; no service was started.", "model_api_unavailable", {"error_type": type(error).__name__}) from error


def _ollama_snapshot(endpoint):
    version = _http_json(endpoint, "/api/version").get("version")
    tags, running = _http_json(endpoint, "/api/tags"), _http_json(endpoint, "/api/ps")
    if not isinstance(version, str) or not version or not isinstance(tags.get("models"), list) or not isinstance(running.get("models"), list):
        raise CleanMacError("The Ollama version/model/running-model response is unsupported.", "unsupported_model_api")
    if any(not isinstance(m, dict) or not isinstance(m.get("name", m.get("model")), str) for m in running["models"]):
        raise CleanMacError("Loaded-model state is unverified.", "unsupported_model_api")
    loaded = {m.get("name", m.get("model")) for m in running["models"]}
    loaded_digests = {m["digest"] for m in running["models"] if isinstance(m.get("digest"), str)}
    items, seen = [], set()
    for model in tags["models"]:
        if not isinstance(model, dict) or not isinstance(model.get("name"), str) or not _MODEL.fullmatch(model["name"]) or model["name"] in seen or not isinstance(model.get("digest"), str) or not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", model["digest"]) or type(model.get("size")) is not int or model["size"] < 0:
            raise CleanMacError("The Ollama model inventory has ambiguous or unsupported identifiers.", "unsupported_model_api")
        seen.add(model["name"])
        active = model["name"] in loaded or model["digest"] in loaded_digests
        item = {"name": model["name"], "model_digest": model["digest"], "logical_size": model["size"], "loaded": active}
        item.update(id="model-" + digest({"endpoint": endpoint, **item})[:24], eligible=not active, selected=False,
                    reclaimable_bytes=None, reason="Loaded models and shared digests are protected." if active else "Deletion uses Ollama's reference-aware API; local/custom models may not be downloadable again.")
        items.append(item)
    return {"version": version, "items": items}


def _run(argv, *, environment=None):
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace", timeout=20, check=False, env=environment)
    except (OSError, subprocess.SubprocessError) as error:
        raise CleanMacError("The selected developer/model tool is unavailable.", "tool_unavailable", {"error_type": type(error).__name__}) from error
    if result.returncode or len(result.stdout) > _MAX_METADATA:
        raise CleanMacError("The tool failed or returned an oversized response; no fallback command was run.", "tool_failed", {"returncode": result.returncode})
    return result.stdout


def _model_report(options, store, cancel):
    report = _record("models")
    configuration = _configuration(options)
    provider = configuration.get("provider", "ollama")
    report["provider"] = provider
    if _cancel(report, cancel):
        return _finish(store, report)
    if provider == "ollama":
        endpoint = _endpoint(configuration)
        report["endpoint"] = endpoint
        try:
            snapshot = _ollama_snapshot(endpoint)
            report.update(items=snapshot["items"], provider_version=snapshot["version"])
        except CleanMacError as error:
            report["errors"].append(error.as_dict())
        report["limitations"] = ["Provider-reported sizes can include shared blobs and do not promise reclaimable bytes.",
                                 "No daemon is started, model unloaded, or model contents read. Loaded-state checks are snapshots; stop clients before removal."]
    elif provider == "lmstudio":
        executable = shutil.which("lms")
        if not executable:
            report["errors"].append({"code": "dependency_missing", "message": "LM Studio's lms CLI is not installed or on PATH."})
        else:
            try:
                payload = json.loads(_run([executable, "ls", "--json", "--host", "127.0.0.1"]))
                values = payload.get("models") if isinstance(payload, dict) else payload
                if not isinstance(values, list) or any(not isinstance(v, dict) for v in values):
                    raise ValueError()
                for index, model in enumerate(values[:10000]):
                    label = model.get("modelKey", model.get("path", model.get("name")))
                    if not isinstance(label, str):
                        continue
                    report["items"].append({"id": "lms-" + digest({"index": index, "label": label})[:24], "name": label,
                                            "eligible": False, "loaded": "unverified", "reason": "Use LM Studio model storage controls; this installed CLI's safe removal interface has not been validated."})
                if len(values) > 10000:
                    report["status"] = "partial"
            except (CleanMacError, ValueError) as error:
                report["errors"].append(_error(executable, error))
        report["handoff"] = "Open LM Studio → My Models to review loaded state and remove exact models; do not erase shared blob folders."
    elif provider == "huggingface":
        # Metadata of the explicitly selected cache only. Snapshot symlinks and
        # reference-aware deletion are left to the installed HF implementation.
        report = _scan_files("models", options, store, cancel, None)
        report.update(provider=provider)
        report["eligible_ids"] = []
        for item in report["items"]:
            item.update(eligible=False, reason="Shared Hub cache: use Hugging Face's reference-aware revision deletion; generic file removal is unavailable.")
        report["handoff"] = {"source": "https://huggingface.co/docs/huggingface_hub/guides/manage-cache", "instruction": "Use hf cache tooling for the installed version or scan_cache_dir(...).delete_revisions(...); inspect its deletion strategy before executing."}
    else:
        raise CleanMacError("provider must be ollama, lmstudio, or huggingface.", "invalid_configuration")
    return _finish(store, report)


def _model_plan(options, store):
    from .actions import create_action_plan
    scan = store.get("scans", options.get("record_id"))
    if scan.get("tool") != "models" or scan.get("provider") != "ollama":
        raise CleanMacError("Only reviewed Ollama model removal is supported.", "unsupported_action")
    selected = options.get("selected", [])
    if not isinstance(selected, list) or len(selected) != 1:
        raise CleanMacError("Select one exact unloaded model per removal plan.", "invalid_selection")
    item = next((m for m in scan["items"] if m["id"] == selected[0]), None)
    if not item or not item.get("eligible"):
        raise CleanMacError("This model was not eligible in the selected inventory.", "ineligible_item")
    endpoint = _endpoint({"endpoint": scan.get("endpoint")})
    snapshot = _ollama_snapshot(endpoint)
    current = next((m for m in snapshot["items"] if m["id"] == item["id"]), None)
    if current != item or snapshot["version"] != scan.get("provider_version"):
        raise CleanMacError("Model identity, loaded state, or provider version changed; inspect again.", "stale_item")
    return create_action_plan(store, "models", "delete-model", {"provider": "ollama", "endpoint": endpoint, "model": item, "version": snapshot["version"]},
                              {"effect": "Permanently remove this local model through Ollama; shared blobs remain provider-managed.",
                               "model": item["name"], "recovery": "No Trash recovery. A downloadable model needs another network download; locally created models may be irrecoverable.",
                               "reclaimable_bytes": None, "prerequisite": "Stop clients that may load or replace this model before approval."})


def _model_apply(options, store, cancel, progress):
    from .actions import ActionNotStarted, execute_action_plan

    def validate(plan):
        payload = plan["payload"]
        endpoint = _endpoint({"endpoint": payload.get("endpoint")})
        if payload.get("provider") != "ollama" or not isinstance(payload.get("model"), dict):
            raise CleanMacError("Invalid model deletion plan.", "invalid_plan")
        snapshot = _ollama_snapshot(endpoint)
        item = payload["model"]
        current = next((m for m in snapshot["items"] if m["id"] == item.get("id")), None)
        if current != item or not current.get("eligible") or snapshot["version"] != payload.get("version"):
            raise CleanMacError("Model, version, or loaded state changed; no model was deleted.", "stale_item")

    def execute(plan):
        payload = plan["payload"]
        # Validate immediately before this single provider mutation as well.
        try:
            validate(plan)
        except CleanMacError as error:
            raise ActionNotStarted(error.message, error.code, error.details) from error
        _http_json(payload["endpoint"], "/api/delete", "DELETE", {"model": payload["model"]["name"]})
        remaining = _ollama_snapshot(payload["endpoint"])
        if any(m["name"] == payload["model"]["name"] for m in remaining["items"]):
            raise CleanMacError("The provider did not confirm model removal; inspect before any retry.", "verification_failed")
        return {"status": "completed", "provider": "ollama", "removed_model": payload["model"]["name"], "restorable": False, "reclaimable_bytes": None}

    return execute_action_plan(store, options.get("record_id"), options.get("approve"), "models", "delete-model", validate, execute, cancel=cancel, progress=progress)


def _sim_snapshot(configuration):
    from .permissions import _open_directory
    developer = configuration.get("developer_dir")
    if developer is None:
        developer = _run(["/usr/bin/xcode-select", "-p"]).strip()
    if not isinstance(developer, str) or not os.path.isabs(developer) or "\x00" in developer:
        raise CleanMacError("Select an absolute Developer directory.", "invalid_configuration")
    fd = _open_directory(developer)
    try:
        info = os.fstat(fd)
        if info.st_uid not in {0, os.getuid()} or info.st_mode & 0o022:
            raise CleanMacError("The selected Developer directory is not trusted.", "unsafe_tool")
    finally:
        os.close(fd)
    environment = dict(os.environ, DEVELOPER_DIR=developer)
    executable = _run(["/usr/bin/xcrun", "--find", "simctl"], environment=environment).strip()
    if not os.path.isabs(executable):
        raise CleanMacError("xcrun returned an invalid simctl location.", "unsafe_tool")
    executable_info = os.lstat(executable)
    if not stat.S_ISREG(executable_info.st_mode) or executable_info.st_uid not in {0, os.getuid()} or executable_info.st_mode & 0o022:
        raise CleanMacError("The selected simctl executable is not trusted.", "unsafe_tool")
    help_text = _run([executable, "help", "delete"], environment=environment)
    if "delete" not in help_text or "unavailable" not in help_text or "device" not in help_text.casefold():
        raise CleanMacError("This simctl delete interface is unsupported; use Xcode's device manager.", "unsupported_simctl")
    try:
        devices = json.loads(_run([executable, "list", "devices", "--json"], environment=environment))
    except (ValueError, RecursionError) as error:
        raise CleanMacError("Unsupported simulator inventory JSON.", "unsupported_simctl") from error
    if not isinstance(devices, dict) or not isinstance(devices.get("devices"), dict):
        raise CleanMacError("Unsupported simulator inventory response.", "unsupported_simctl")
    active = rules.detect_active_apps()
    builds = active is None or bool({"xcode", "xcodebuild", "swift", "swiftc", "clang", "clang++"}.intersection(active))
    items, seen = [], set()
    for runtime, group in devices["devices"].items():
        if not isinstance(group, list):
            raise CleanMacError("Unsupported simulator device list.", "unsupported_simctl")
        for device in group:
            if not isinstance(device, dict) or not isinstance(device.get("udid"), str) or not _UUID.fullmatch(device["udid"]) or device["udid"] in seen:
                raise CleanMacError("Ambiguous simulator identity.", "unsupported_simctl")
            seen.add(device["udid"])
            item = {"udid": device["udid"], "runtime": runtime, "name": device.get("name"), "state": device.get("state"), "available": device.get("isAvailable")}
            item.update(id="simulator-" + digest(item)[:24], eligible=device.get("isAvailable") is False and device.get("state") == "Shutdown" and not builds,
                        reason="Only explicitly selected unavailable, shutdown devices with no active builds can be removed; device app data is lost.")
            items.append(item)
    return {"developer_dir": developer, "developer_identity": {"device": info.st_dev, "inode": info.st_ino},
            "simctl": executable, "simctl_identity": inventory._stat_values(executable_info), "delete_help_digest": digest(help_text), "items": items}


def _xcode_report(options, store, cancel):
    from .adapters import scan_xcode
    roots = _roots(options, required=False)
    configuration = _configuration(options)
    if not roots and configuration.get("simulators") is not True:
        raise CleanMacError("Choose Developer roots or explicitly request simulator inventory.", "scope_required")
    report = _record("xcode-cleanup")
    report["roots"] = roots
    if roots:
        categories = scan_xcode(roots, cancel=cancel)
        report.update(items=categories.get("items", []), errors=categories.get("errors", []), limitations=categories.get("limitations", []), status=categories.get("status", "partial"))
    if configuration.get("simulators") is True and not _cancel(report, cancel):
        try:
            snapshot = _sim_snapshot(configuration)
            report["simulator_environment"] = {k: v for k, v in snapshot.items() if k != "items"}
            report["items"].extend(snapshot["items"])
        except (CleanMacError, OSError, ValueError) as error:
            report["errors"].append(_error(configuration.get("developer_dir", "selected developer tools"), error))
    return _finish(store, report)


def _xcode_plan(options, store):
    from .actions import create_action_plan
    scan = store.get("scans", options.get("record_id"))
    selected = options.get("selected", [])
    if scan.get("tool") != "xcode-cleanup" or not isinstance(selected, list) or len(selected) != 1:
        raise CleanMacError("Select one unavailable simulator from this inventory.", "invalid_selection")
    item = next((i for i in scan["items"] if i["id"] == selected[0] and i.get("eligible") and i.get("udid")), None)
    if item is None:
        raise CleanMacError("This developer resource is report-only or active.", "ineligible_item")
    expected = scan.get("simulator_environment", {})
    snapshot = _sim_snapshot({"developer_dir": expected.get("developer_dir")})
    current_environment = {k: v for k, v in snapshot.items() if k != "items"}
    if current_environment != expected or item not in snapshot["items"]:
        raise CleanMacError("The selected developer environment or simulator changed.", "stale_item")
    return create_action_plan(store, "xcode-cleanup", "delete-simulator", {"environment": expected, "device": item},
                              {"effect": "Permanently delete this unavailable, shutdown simulator through simctl.", "udid": item["udid"],
                               "recovery": "No Trash recovery; simulator applications and data are lost. Archives, runtimes, and available devices are not selected."})


def _xcode_apply(options, store, cancel, progress):
    from .actions import ActionNotStarted, execute_action_plan

    def validate(plan):
        payload = plan["payload"]
        snapshot = _sim_snapshot({"developer_dir": payload["environment"]["developer_dir"]})
        if {k: v for k, v in snapshot.items() if k != "items"} != payload["environment"] or payload["device"] not in snapshot["items"] or not payload["device"].get("eligible"):
            raise CleanMacError("Developer selection, resource state, or active-build state changed.", "stale_item")

    def execute(plan):
        try:
            validate(plan)
        except CleanMacError as error:
            raise ActionNotStarted(error.message, error.code, error.details) from error
        payload = plan["payload"]
        environment = dict(os.environ, DEVELOPER_DIR=payload["environment"]["developer_dir"])
        _run([payload["environment"]["simctl"], "delete", payload["device"]["udid"]], environment=environment)
        snapshot = _sim_snapshot({"developer_dir": payload["environment"]["developer_dir"]})
        if any(i["udid"] == payload["device"]["udid"] for i in snapshot["items"]):
            raise CleanMacError("Simulator removal could not be verified; inspect before retrying.", "verification_failed")
        return {"status": "completed", "removed_udid": payload["device"]["udid"], "restorable": False}

    return execute_action_plan(store, options.get("record_id"), options.get("approve"), "xcode-cleanup", "delete-simulator", validate, execute, cancel=cancel, progress=progress)


def dispatch(name, action, options, *, store, cancel=None, progress=None):
    spec = next((s for s in TOOL_SPECS if s["name"] == name), None)
    if spec is None or action not in spec["actions"]:
        raise CleanMacError("This cleanup tool/action is unsupported.", "unsupported_action")
    if not isinstance(options, dict):
        raise CleanMacError("Tool options must be an object.", "invalid_configuration")
    if name == "models":
        if action == "inspect":
            return _model_report(options, store, cancel)
        return _model_plan(options, store) if action == "plan" else _model_apply(options, store, cancel, progress)
    if name == "xcode-cleanup":
        if action == "inspect":
            return _xcode_report(options, store, cancel)
        return _xcode_plan(options, store) if action == "plan" else _xcode_apply(options, store, cancel, progress)
    if action == "plan":
        return _file_plan(name, options, store, cancel, progress)
    if action == "apply":
        return _file_apply(name, options, store, cancel)
    if name == "preferences":
        return _preference_report(options, store, cancel, progress)
    if name == "home-archives":
        return _archive_report(options, store, cancel)
    if name == "device-backups":
        return _backup_report(options, store, cancel)
    return _scan_files(name, options, store, cancel, progress)

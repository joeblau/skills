"""Explicit native API workflows for documents, images, Photos, and screen cleaning.

Permission queries never request access. Native execution is optional and local;
the app bundle provides the Photos usage description and stable Settings identity.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

from .common import CleanMacError, digest, new_id, utc_now


ALGORITHM = "vision-revision-1-ahash64-imageio-512-v1"
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".bmp", ".gif", ".webp", ".dng", ".cr2", ".nef", ".arw"}
TOOL_SPECS = [
    {"name": "document-versions", "title": "Document version history", "feature_ids": ["CM-018"],
     "actions": ["inspect", "plan", "apply"], "scope_required": True,
     "description": "Review local NSFileVersion history for one selected document up to 64 MiB; current, earliest, conflict, and pinned versions are protected.",
     "requirements": ["native-features-helper"],
     "configuration": {"pinned": "Version IDs retained in addition to default protections.", "confirm_history_loss": "Set true only after reviewing irreversible revision removal."}},
    {"name": "similar-images", "title": "Similar image review", "feature_ids": ["CM-050"],
     "actions": ["inspect", "plan", "apply"], "scope_required": True,
     "description": "ImageIO thumbnails (512 px) and versioned Vision distances; files up to 64 MiB and 300 MP. Related images require visual review and explicit keepers.",
     "requirements": ["native-features-helper", "native-trash-helper-for-apply"],
     "configuration": {"limit": "1–200 images (default 200).", "threshold": "Vision revision-1 distance cutoff, 1–40 (default 8).", "keep": "Exact retained image IDs.", "confirm_visual_review": "True only after visually reviewing selected images and keepers."}},
    {"name": "photos", "title": "Photos library review", "feature_ids": ["CM-052"],
     "actions": ["inspect", "request-access", "plan", "apply"], "scope_required": False,
     "description": "PhotoKit metadata and explicit whole-asset deletion; iCloud synchronization and Recently Deleted remain controlled by Photos.",
     "requirements": ["native-features-helper", "photos-permission"],
     "configuration": {"limit": "1–1000 metadata records (default 200).", "offset": "Page offset for metadata review, 0–100000.", "include_similar": "Explicitly analyze up to 100 locally available Photos thumbnails in the page; never downloads originals.", "request_permission": "True to show the system Photos permission prompt after the user requests it.", "confirm_synced_deletion": "True after reviewing entire-asset deletion and its iCloud effects."}},
    {"name": "screen-cleaning", "title": "Screen cleaning mode", "feature_ids": ["CM-062"],
     "actions": ["inspect", "plan", "apply"], "scope_required": False,
     "description": "Foreground black overlays and temporary pointer/keyboard suppression with Escape, independent watchdog, and at most 120 seconds.",
     "requirements": ["native-features-helper", "accessibility-permission", "interactive-desktop"],
     "configuration": {"duration": "1–120 seconds, default 60. Media keys, power buttons, and secure system gestures cannot be guaranteed suppressed."}},
]


def _fail(message, code="invalid_arguments", **details):
    raise CleanMacError(message, code, details or None)


def _integer(config, key, default, minimum, maximum):
    value = config.get(key, default)
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(f"{key} must be an integer from {minimum} through {maximum}.")
    return value


def _ids(value):
    if not isinstance(value, list) or len(value) > 1000 or any(not isinstance(item, str) or not item or len(item) > 4096 or "\0" in item for item in value) or len(set(value)) != len(value):
        _fail("Provide distinct, exact identifiers.")
    return value


def _helper_path():
    from .inventory import _canonical_root
    configured = os.environ.get("CLEANMAC_FEATURES_PATH")
    path = Path(configured).expanduser() if configured else Path(__file__).resolve().parents[2] / "native/bin/CleanMac Features.app/Contents/MacOS/cleanmac-features"
    return Path(_canonical_root(path))


def _trusted_helper():
    if sys.platform != "darwin":
        _fail("Native Mac features require macOS.", "unsupported_platform")
    path = _helper_path()
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022 or not os.access(path, os.X_OK):
            _fail("The native features helper is not a trusted executable.", "unsafe_native_helper")
        for parent in path.parents:
            metadata = parent.lstat()
            if not stat.S_ISDIR(metadata.st_mode):
                _fail("A native helper ancestor is symbolic or not a directory.", "unsafe_native_helper")
            if metadata.st_mode & 0o022 and not (metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX):
                _fail("A native helper ancestor is writable by other users.", "unsafe_native_helper")
    except FileNotFoundError:
        _fail("Build the optional helper with sh native/build-features.sh, then retry. Inspection of ordinary files still works without it.", "native_helper_missing")
    return path


def _call(command, payload, *, cancel=None, timeout=90):
    path = _trusted_helper()
    process = subprocess.Popen([str(path), command], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("ascii")
    if len(encoded) > 1024 * 1024:
        process.kill()
        process.communicate()
        _fail("The native request is too large.")
    started = time.monotonic()
    first = True
    while True:
        try:
            stdout, stderr = process.communicate(input=encoded if first else None, timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            first = False
            if (cancel and cancel()) or time.monotonic() - started >= timeout:
                process.terminate()
                try:
                    process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                _fail("Native work stopped; a started mutation may require recovery review.", "cancelled" if cancel and cancel() else "native_timeout")
    if len(stdout) > 8 * 1024 * 1024:
        _fail("The native result exceeded its limit.", "invalid_native_response")
    try:
        value = json.loads(stdout)
    except (ValueError, UnicodeDecodeError) as error:
        raise CleanMacError("The native helper did not return valid JSON.", "invalid_native_response") from error
    if not isinstance(value, dict):
        _fail("The native helper returned an invalid result.", "invalid_native_response")
    if process.returncode or value.get("status") == "error":
        error = value.get("error", {})
        _fail(error.get("message", "The native operation failed."), error.get("code", "native_error"))
    return value


def _handshake(cancel=None):
    info = _call("info", {}, cancel=cancel, timeout=5)
    if info.get("protocol_version") != 1 or info.get("version") != "1.0.0" or info.get("privileged") is not False or not isinstance(info.get("capabilities"), list):
        _fail("The native features helper is incompatible; rebuild it.", "native_helper_incompatible")
    return info


def _scan_record(store, name, result, **extra):
    record = {"id": new_id("scan"), "schema_version": 1, "created_at": utc_now(), "tool": name, **result, **extra}
    store.put("scans", record)
    return record


def _record(store, record_id, name):
    if not record_id:
        _fail("Choose the saved inspection record before selecting items.", "scan_required")
    record = store.get("scans", record_id)
    if record.get("tool") != name:
        _fail("This inspection belongs to another tool.", "invalid_scan")
    return record


def _selected(record, selected, key="items"):
    selected = _ids(selected)
    if not selected:
        _fail("Select exact items; nothing is selected automatically.", "selection_required")
    by_id = {item["id"]: item for item in record.get(key, []) if isinstance(item, dict) and isinstance(item.get("id"), str)}
    if not set(selected).issubset(by_id):
        _fail("A selected ID was not present in this inspection.", "invalid_selection")
    values = [copy.deepcopy(by_id[identifier]) for identifier in selected]
    if any(item.get("protected") for item in values):
        _fail("The selection contains a protected item.", "protected_item")
    return values


def _document_item(roots, store):
    from .inventory import scan_roots
    if not isinstance(roots, list) or len(roots) != 1:
        _fail("Choose exactly one local document file with --root.", "invalid_scope")
    scan = scan_roots(roots, max_items=2, exclude_paths=[str(store.root), *store.get_settings().get("ignore_paths", [])])
    eligible = [item for item in scan["items"] if item.get("kind") == "file" and not item.get("protected") and not item.get("is_placeholder")]
    if len(eligible) != 1 or len(scan["items"]) != 1 or scan["status"] != "complete":
        _fail("Select one accessible local document, outside app/library packages, ignored paths, and protected locations.", "invalid_scope", errors=scan.get("errors", []), exclusions=scan.get("exclusions", []))
    return eligible[0]


def _inspect(name, roots, selected, config, store, cancel, progress):
    info = _handshake(cancel)
    if name not in info["capabilities"]:
        _fail("This helper does not implement the requested tool.", "unsupported_capability")
    if name == "document-versions":
        item = _document_item(roots, store)
        result = _call("versions-inspect", {"path": item["path"], "pinned": _ids(config.get("pinned", []))}, cancel=cancel)
        return _scan_record(store, name, result, document_item=item)
    if name == "similar-images":
        from .inventory import scan_roots, assert_item_identity
        if not roots:
            _fail("Choose local image folders or files explicitly.", "invalid_scope")
        limit = _integer(config, "limit", 200, 1, 200)
        threshold = _integer(config, "threshold", 8, 1, 40)
        settings = store.get_settings()
        inventory = scan_roots(roots, max_items=100000, exclude_paths=[str(store.root), *settings.get("ignore_paths", [])], cancel=cancel, progress=progress)
        items = [item for item in inventory["items"] if item.get("kind") == "file" and not item.get("protected") and not item.get("is_placeholder") and Path(item["path"]).suffix.lower() in _IMAGE_SUFFIXES]
        if cancel and cancel():
            return _scan_record(store, name, {"status": "cancelled", "items": [], "pairs": []})
        result = _call("images-inspect", {"paths": [item["path"] for item in items[:limit]], "threshold": threshold}, cancel=cancel)
        native_ids = {}
        inventory_by_path = {item["path"]: item for item in items[:limit]}
        for value in result.get("items", []):
            item = inventory_by_path.get(value.get("path"))
            if not item:
                _fail("A native image result escaped the selected scope.", "invalid_native_response")
            assert_item_identity(item)
            native_ids[value["id"]] = item["id"]
            value["id"] = item["id"]
        for pair in result.get("pairs", []):
            pair["item_ids"] = [native_ids[identifier] for identifier in pair["item_ids"]]
        if len(items) > limit or inventory["status"] != "complete":
            result["status"] = "partial"
        return _scan_record(store, name, result, inventory=inventory, limit=limit, available_image_count=len(items))
    if name == "photos":
        if roots:
            _fail("Photos uses library asset IDs, never a filesystem library root.", "invalid_scope")
        if type(config.get("include_similar", False)) is not bool:
            _fail("include_similar must be a boolean.")
        result = _call("photos-inspect", {"limit": _integer(config, "limit", 200, 1, 1000), "offset": _integer(config, "offset", 0, 0, 100000), "selected": _ids(selected), "include_similar": config.get("include_similar", False)}, cancel=cancel)
        return _scan_record(store, name, result)
    if name == "screen-cleaning":
        result = _call("screen-status", {}, cancel=cancel)
        result.update({"tool": name, "permission_settings": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
                       "exit": "Escape, automatic timeout, process termination, sleep, or display change.",
                       "limitations": "Power buttons, media keys, secure system gestures, and another app's secure input are outside this mode's guarantee."})
        return result
    _fail("Unknown native tool.", "unsupported_tool")


def _plan(name, options, store, cancel, progress):
    from .actions import create_action_plan
    selected = options.get("selected", [])
    config = options.get("configuration", {})
    if name == "screen-cleaning":
        duration = _integer(config, "duration", 60, 1, 120)
        status = _inspect(name, [], [], {}, store, cancel, progress)
        if status.get("accessibility") is not True or status.get("secure_input") is not False:
            _fail("Screen cleaning needs Accessibility access and an unlocked desktop. Grant it yourself if desired, then retry.", "permission_needed")
        return create_action_plan(store, name, "start", {"duration": duration},
            {"effect": "Temporarily suppress ordinary keyboard and pointer input on all displays.", "seconds": duration,
             "exit": "Escape or timeout", "watchdog": "Separate process enforces timeout even if the event loop stalls.",
             "limitations": status["limitations"]})
    record = _record(store, options.get("record_id"), name)
    if name == "document-versions":
        if config.get("confirm_history_loss") is not True:
            _fail("Review permanent loss of the selected document history before planning removal.", "confirmation_required")
        versions = _selected(record, selected, "versions")
        payload = {key: copy.deepcopy(record[key]) for key in ("path", "document_sha256", "document_identity", "pinned", "document_item")}
        payload.update({"versions": versions, "selected": _ids(selected), "confirm_history_loss": True})
        return create_action_plan(store, name, "remove", payload,
            {"document": record["path"], "selected_versions": versions, "permanent_history_loss": True,
             "protected": "Current, earliest, conflict, and pinned versions remain."})
    if name == "photos":
        if config.get("confirm_synced_deletion") is not True:
            _fail("Review whole-asset deletion, iCloud synchronization, and Photos Recently Deleted before planning.", "confirmation_required")
        assets = _selected(record, selected)
        return create_action_plan(store, name, "delete", {"assets": assets, "confirm_synced_deletion": True},
            {"selected_assets": assets, "effect": record["deletion_effect"], "recovery": "Photos Recently Deleted, while available; no filesystem restoration."})
    if name == "similar-images":
        from .plans import make_plan
        if config.get("confirm_visual_review") is not True:
            _fail("Visually review the images and explicitly choose keepers first; similarity is not duplication.", "confirmation_required")
        _selected(record, selected)
        keep = _ids(config.get("keep", []))
        if not keep or set(keep).intersection(selected):
            _fail("Choose retained image IDs separately from the images to Trash.", "missing_keeper")
        kept_rows = _selected(record, keep)
        pairs = [set(pair["item_ids"]) for pair in record.get("pairs", [])]
        if any(not any(identifier in pair and pair.intersection(keep) for pair in pairs) for identifier in selected):
            _fail("Every selected image needs a reviewed similar image retained as a keeper.", "missing_keeper")
        if record.get("algorithm") != ALGORITHM:
            _fail("The image algorithm changed; rescan and review the images.", "stale_plan")
        file_plan = make_plan(record["inventory"], selected, cancel=cancel, progress=progress)
        by_id = {item["id"]: item for item in record["inventory"]["items"]}
        keepers = []
        for row in kept_rows:
            keeper = copy.deepcopy(by_id[row["id"]])
            keeper["content_sha256"] = row["content_sha256"]
            keepers.append(keeper)
        # The file executor requires the original durable file plan. Never
        # recreate a forgotten plan during apply from an outer action payload.
        store.put("plans", file_plan)
        return create_action_plan(store, name, "trash", {"file_plan": file_plan, "keepers": keepers, "algorithm": ALGORITHM},
            {"selected_files": file_plan["summary"], "retained_images": [item["path"] for item in keepers], "similarity_is_not_equivalence": True})
    _fail("This tool does not support planning.", "unsupported_action")


def _apply(name, options, store, cancel):
    from .actions import execute_action_plan
    action = {"document-versions": "remove", "photos": "delete", "screen-cleaning": "start", "similar-images": "trash"}[name]

    def validate(plan):
        info = _handshake(cancel)
        if name not in info["capabilities"]:
            _fail("The native helper no longer provides this tool.", "unsupported_capability")
        payload = plan["payload"]
        if name == "document-versions":
            from .inventory import assert_item_identity
            assert_item_identity(payload["document_item"])
            current = _call("versions-inspect", {"path": payload["path"], "pinned": payload["pinned"]}, cancel=cancel)
            if current.get("document_sha256") != payload["document_sha256"] or current.get("document_identity") != payload["document_identity"]:
                _fail("The selected document changed; review a new plan.", "stale_plan")
            current_by_id = {row["id"]: row for row in current["versions"]}
            for prior in payload["versions"]:
                fresh = current_by_id.get(prior["id"])
                if fresh is not None and (fresh != prior or fresh.get("protected")):
                    _fail("A selected revision changed or became protected.", "stale_plan")
        elif name == "photos":
            ids = [asset["id"] for asset in payload["assets"]]
            current = _call("photos-inspect", {"selected": ids, "limit": len(ids)}, cancel=cancel)
            if current.get("authorization") not in ("authorized", "limited"):
                _fail("Photos permission is unavailable.", "permission_needed")
            if {asset["id"]: asset for asset in current.get("items", [])} != {asset["id"]: asset for asset in payload["assets"]}:
                _fail("Selected Photos assets changed or disappeared.", "stale_plan")
        elif name == "screen-cleaning":
            status = _call("screen-status", {}, cancel=cancel)
            if status.get("accessibility") is not True or status.get("secure_input") is not False:
                _fail("Screen cleaning cannot safely establish input control.", "permission_needed")
        elif name == "similar-images":
            from .executor import _validate_live
            from .plans import validate_plan
            if info.get("image_algorithm") != payload["algorithm"] or payload["algorithm"] != ALGORITHM:
                _fail("Image algorithm version changed; rescan and review.", "stale_plan")
            validate_plan(payload["file_plan"])
            for keeper in payload["keepers"]:
                _validate_live(keeper, cancel=cancel)

    def execute(plan):
        payload = plan["payload"]
        if name == "similar-images":
            from .executor import apply_plan, _validate_live
            file_plan = payload["file_plan"]
            for keeper in payload["keepers"]:
                _validate_live(keeper, cancel=cancel)
            receipt = apply_plan(file_plan, approval_digest=file_plan["digest"], store=store,
                native_path=os.environ.get("CLEANMAC_NATIVE_PATH"), cancel=cancel)
            if receipt.get("status") not in ("completed", "complete"):
                _fail("Review the file receipt before continuing or restoring.", "file_action_incomplete", receipt_id=receipt["id"])
            return {"status": "completed", "file_receipt_id": receipt["id"], "restore_handle": receipt["id"]}
        command = {"document-versions": "versions-remove", "photos": "photos-delete", "screen-cleaning": "screen-start"}[name]
        request = copy.deepcopy(payload)
        if name == "screen-cleaning":
            request["approved"] = True
        result = _call(command, request, cancel=cancel, timeout=payload.get("duration", 180) + 10)
        if result.get("status") != "complete":
            _fail("The native action was not verified complete; inspect its state before retrying.", "native_action_incomplete")
        return {**result, "status": "completed"}

    return execute_action_plan(store, options.get("record_id"), options.get("approve"), name, action, validate, execute, cancel=cancel)


def dispatch(name, action, options, *, store, cancel=None, progress=None):
    spec = next((item for item in TOOL_SPECS if item["name"] == name), None)
    if spec is None or action not in spec["actions"]:
        _fail("This native tool/action is unavailable.", "unsupported_action")
    if not isinstance(options, dict) or not isinstance(options.get("configuration", {}), dict):
        _fail("Native options must contain an object configuration.")
    config = options.get("configuration", {})
    if cancel and cancel():
        return {"status": "cancelled", "tool": name}
    if action == "inspect":
        try:
            return _inspect(name, options.get("roots", []), options.get("selected", []), config, store, cancel, progress)
        except CleanMacError as error:
            if error.code in ("native_helper_missing", "unsupported_platform", "native_helper_incompatible", "unsafe_native_helper"):
                return {"tool": name, "status": "unavailable", "coverage": "unscanned", "error": error.as_dict()}
            raise
    if action == "request-access":
        if name != "photos" or config.get("request_permission") is not True:
            _fail("Request the Photos permission prompt explicitly.", "confirmation_required")
        _handshake(cancel)
        return _call("photos-authorize", {"request_permission": True}, cancel=cancel, timeout=180)
    if action == "plan":
        return _plan(name, options, store, cancel, progress)
    return _apply(name, options, store, cancel)

"""My Tools entry points over the existing local-file workflow.

This module deliberately reuses the inventory, duplicate verification, plans,
and executor; favorites do not introduce an alternate cleanup path.
"""
from __future__ import annotations

from .common import CleanMacError


TOOL_SPECS = [{
    "name": "duplicates", "title": "Exact duplicate review",
    "feature_ids": ["CM-004", "CM-049"], "actions": ["inspect", "plan", "apply"],
    "scope_kind": "local", "description": "Scan explicit local roots, review exact duplicate groups, retain chosen keepers, and prepare recoverable file-Trash plans.",
    "requirements": ["macOS", "Explicit roots or a saved duplicate scan", "Native Trash helper for apply only"],
    "configuration": {
        "max_items": "Inspection inventory limit, 1–1000000; default 100000.",
        "duplicate_min_size": "Minimum bytes compared, positive integer; default 1.",
        "keep": "Exact retained file IDs when preparing a plan; these override keeper suggestions.",
        "offset": "Saved review page offset; default 0.",
        "limit": "Saved review page size, 1–100000; default 20.",
    },
    "action_usage": {
        "inspect": "Provide --root PATH to scan, or --record SCAN_ID to review a saved scan without rescanning.",
        "plan": "Provide --record SCAN_ID --select ITEM_ID and optional --config '{\"keep\":[\"KEEPER_ID\"]}'.",
        "apply": "Provide --record PLAN_ID --approve DIGEST after reviewing exact selected files and retained keepers.",
        "recovery": "Local file receipts use cleanmac restore RECEIPT_ID --approve RECEIPT_ID after reviewing recovery targets.",
    },
    "examples": ["tool duplicates --root ~/Downloads", "tool favorites --action add --config '{\"name\":\"duplicates\"}'"],
}]


def dispatch(name, action, options, *, store, cancel=None, progress=None):
    from .cli import dispatch as core_dispatch
    if name != "duplicates":
        raise CleanMacError("Unknown local-file tool.", code="tool_unavailable")
    config = options.get("configuration", {})
    if set(config) - {"max_items", "duplicate_min_size", "keep", "offset", "limit"}:
        raise CleanMacError("Choose configuration fields published by tools duplicates.", code="invalid_tool_configuration")
    record_id = options.get("record_id")
    roots = options.get("roots", [])
    if action == "inspect":
        if roots and record_id:
            raise CleanMacError("Choose a new root scan or a saved review, not both.", code="invalid_scope")
        if record_id:
            argv = ["review", record_id, "--category", "duplicates"]
            for field, flag, default in (("offset", "--offset", 0), ("limit", "--limit", 20)):
                value = config.get(field, default)
                if type(value) is not int:
                    raise CleanMacError("Review limits must be integers.", code="invalid_limit")
                argv += [flag, str(value)]
        elif roots:
            argv = ["scan", "--areas", "duplicates,storage"]
            for root in roots:
                argv += ["--root", root]
            for field, flag, default in (("max_items", "--max-items", 100000), ("duplicate_min_size", "--duplicate-min-size", 1)):
                value = config.get(field, default)
                if type(value) is not int:
                    raise CleanMacError("Scan limits must be integers.", code="invalid_limit")
                argv += [flag, str(value)]
        else:
            return {"status": "scope-needed", "coverage": "unscanned", "items": [],
                    "message": "Choose local folders with --root PATH or resume a saved duplicate scan with --record SCAN_ID. Nothing has been scanned."}
    elif action == "plan":
        selected = options.get("selected", [])
        if not record_id or not selected:
            raise CleanMacError("Review a saved duplicate scan, then select exact IDs and retained keepers.", code="empty_selection")
        scan = store.get("scans", record_id)
        duplicate_ids = {value for group in scan.get("duplicate_groups", []) for value in group.get("item_ids", [])}
        if any(value not in duplicate_ids for value in selected):
            raise CleanMacError("The duplicate tool selects only members of verified duplicate groups.", code="invalid_selection")
        keep = config.get("keep", [])
        if not isinstance(keep, list) or any(not isinstance(value, str) or not value for value in keep):
            raise CleanMacError("keep must be an array of exact keeper IDs.", code="invalid_selection")
        argv = ["plan", record_id]
        for value in selected:
            argv += ["--select", value]
        for value in keep:
            argv += ["--keep", value]
    elif action == "apply":
        if not record_id or not options.get("approve"):
            raise CleanMacError("Apply requires an exact reviewed plan and approval digest.", code="approval_required")
        argv = ["apply", record_id, "--approve", options["approve"]]
    else:
        raise CleanMacError("Unsupported local-file action.", code="unsupported_tool_action")
    return core_dispatch(argv, store=store, cancel=cancel, progress=progress)

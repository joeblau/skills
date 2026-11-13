"""Exact, immutable selections for the local Trash executor.

A plan records a user's explicit file selection. Creating or validating a plan
does not approve it and never changes a scanned file.
"""

from __future__ import annotations

import copy
import os
import re
import stat
from collections import defaultdict

from .common import CleanMacError, digest, new_id, utc_now
from .duplicates import hash_item
from .inventory import assert_item_identity
from .rules import classify_items

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SHA512 = re.compile(r"[0-9a-f]{128}\Z")
_PLAN_KEYS = {
    "id", "schema_version", "created_at", "scan_id", "action", "items",
    "keepers", "rule_snapshots", "digest", "summary", "status",
}


def _fail(message, code="invalid_plan", **details):
    raise CleanMacError(message, code=code, details=details or None)


def _identifier(value):
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _integer(value, *, nonnegative=True):
    return type(value) is int and (not nonnegative or value >= 0)


def _absolute_path(value):
    return (
        isinstance(value, str) and value.startswith("/")
        and not value.startswith("//") and "\x00" not in value
        and os.path.normpath(value) == value
    )


def _item_shape(item, *, require_rule=True):
    """Reject unsafe shapes even when somebody recomputes a plan's digest."""
    if not isinstance(item, dict) or not _identifier(item.get("id")):
        _fail("A selected item has an invalid identity.")
    item_id = item["id"]
    if not _absolute_path(item.get("path")) or not _absolute_path(item.get("root")):
        _fail("Item paths must be absolute and normalized.", item_id=item_id)
    if os.path.commonpath([item["path"], item["root"]]) != item["root"]:
        _fail("A selected item is outside its scan root.", item_id=item_id)
    if item.get("kind") != "file" or not _integer(item.get("mode")):
        _fail("Only ordinary files can enter a Trash plan.", item_id=item_id)
    if not stat.S_ISREG(item["mode"]):
        _fail("A selected item is not a regular file.", item_id=item_id)
    for key in ("logical_size", "allocated_size", "device", "inode", "flags"):
        if not _integer(item.get(key)):
            _fail("A selected item has invalid filesystem metadata.", item_id=item_id, field=key)
    for key in ("mtime_ns", "ctime_ns"):
        if not _integer(item.get(key), nonnegative=False):
            _fail("A selected item has invalid timestamps.", item_id=item_id, field=key)
    if type(item.get("nlink")) is not int or item["nlink"] != 1:
        _fail("Files with multiple hard links cannot enter a Trash plan.", item_id=item_id)
    if type(item.get("protected")) is not bool or item["protected"]:
        _fail("Protected or unclassified files cannot enter a Trash plan.", item_id=item_id)
    if type(item.get("is_placeholder")) is not bool or item["is_placeholder"]:
        _fail("Cloud placeholders cannot enter a Trash plan.", item_id=item_id)
    if item.get("requires_privilege") or item.get("privilege_required"):
        _fail("Privileged file operations are unavailable.", item_id=item_id)
    if item.get("active") or item.get("in_use") or item.get("is_symlink"):
        _fail("Active files and symlinks cannot enter a Trash plan.", item_id=item_id)
    ancestors = item.get("ancestors")
    expected = []
    parent = os.path.dirname(item["path"])
    while True:
        expected.append(parent)
        if parent == "/":
            break
        parent = os.path.dirname(parent)
    expected.reverse()
    if not isinstance(ancestors, list) or len(ancestors) != len(expected):
        _fail("The complete ancestor identity chain is required.", item_id=item_id)
    for ancestor, path in zip(ancestors, expected):
        if (
            not isinstance(ancestor, dict) or ancestor.get("path") != path
            or not _integer(ancestor.get("device"))
            or not _integer(ancestor.get("inode"))
        ):
            _fail("The ancestor identity chain is invalid.", item_id=item_id)
    matched = item.get("matched_rule")
    if matched is not None:
        if (
            not isinstance(matched, dict) or set(matched) != {"rule_id", "rule_version", "rule_digest"}
            or not _identifier(matched.get("rule_id"))
            or not isinstance(matched.get("rule_version"), str) or not matched["rule_version"]
            or len(matched["rule_version"]) > 128
            or not isinstance(matched.get("rule_digest"), str) or not _SHA256.fullmatch(matched["rule_digest"])
        ):
            _fail("A rule snapshot is invalid.", item_id=item_id)
    if require_rule and (item.get("is_app_data") or item.get("category_hint") == "app-data") and matched is None:
        _fail("App data requires an eligible cleanup rule.", item_id=item_id)
    if "content_sha256" in item and (not isinstance(item["content_sha256"], str) or not _SHA256.fullmatch(item["content_sha256"])):
        _fail("A selected content fingerprint is invalid.", item_id=item_id)
    if "rule_content_sha512" in item:
        if matched is None or not isinstance(item["rule_content_sha512"], str) or not _SHA512.fullmatch(item["rule_content_sha512"]):
            _fail("A rule content fingerprint is invalid.", item_id=item_id)


def _selection(values, name):
    if not isinstance(values, (list, tuple, set, frozenset)):
        _fail(f"{name} must be a list of item IDs.", code="invalid_selection")
    result = list(values)
    if any(not _identifier(value) for value in result):
        _fail(f"{name} contains an invalid item ID.", code="invalid_selection")
    if len(result) != len(set(result)):
        _fail(f"{name} contains the same item more than once.", code="invalid_selection")
    return result


def _rule_findings(findings, item_ids):
    result = defaultdict(list)
    if not isinstance(findings, list):
        _fail("Cleanup rule findings must be a list.", code="invalid_scan")
    for finding in findings:
        if not isinstance(finding, dict):
            _fail("A cleanup rule finding is invalid.", code="invalid_scan")
        item_id = finding.get("item_id")
        if not _identifier(item_id):
            _fail("A cleanup rule finding has an invalid target.", code="invalid_scan")
        if item_id in item_ids and finding.get("rule_id"):
            if not _identifier(finding.get("rule_id")) or not isinstance(finding.get("rule_version"), str) or not finding["rule_version"]:
                _fail("A cleanup rule finding has an invalid revision.", code="invalid_scan")
            if finding.get("eligible") is True and (not isinstance(finding.get("rule_digest"), str) or not _SHA256.fullmatch(finding["rule_digest"])):
                _fail("An eligible cleanup rule requires its exact content digest.", code="invalid_scan")
            if "content_sha512" in finding and (not isinstance(finding["content_sha512"], str) or not _SHA512.fullmatch(finding["content_sha512"])):
                _fail("A cleanup rule content fingerprint is invalid.", code="invalid_scan")
            result[finding["item_id"]].append(finding)
    return result


def _prepare_rules(items, prior_findings, cancel=None):
    item_ids = {item["id"] for item in items}
    previous = _rule_findings(prior_findings, item_ids)
    current = _rule_findings(classify_items(items, cancel=cancel), item_ids)
    snapshots = []
    for item in items:
        item_id = item["id"]
        before, now = previous[item_id], current[item_id]
        if any(finding.get("eligible") is not True for finding in before + now):
            _fail("The selected file is report-only under its cleanup rule.",
                  code="ineligible_item", item_id=item_id)
        if before and not now:
            _fail("The file no longer matches its scanned cleanup rule; rescan it.",
                  code="rule_changed", item_id=item_id)
        old_versions = {(f.get("rule_id"), f.get("rule_version"), f.get("rule_digest")) for f in before}
        versions = {(f.get("rule_id"), f.get("rule_version"), f.get("rule_digest")) for f in now}
        if old_versions and old_versions != versions:
            _fail("A cleanup rule changed after scanning; rescan the selected file.",
                  code="rule_changed", item_id=item_id)
        if len(versions) > 1:
            _fail("The file has ambiguous cleanup rules.", code="ineligible_item", item_id=item_id)
        if now:
            finding = now[0]
            item["matched_rule"] = {key: finding[key] for key in ("rule_id", "rule_version", "rule_digest")}
            snapshot = {
                "item_id": item_id, "rule_id": finding["rule_id"],
                "rule_version": finding["rule_version"], "rule_digest": finding["rule_digest"], "eligible": True,
            }
            for key in ("category", "reason", "confidence", "prerequisites", "provenance"):
                if key in finding:
                    snapshot[key] = copy.deepcopy(finding[key])
            if "content_sha512" in finding:
                item["rule_content_sha512"] = finding["content_sha512"]
                snapshot["content_sha512"] = finding["content_sha512"]
            snapshots.append(snapshot)
        if (item.get("is_app_data") or item.get("category_hint") == "app-data") and not now:
            _fail("App data has no validated cleanup rule and is report-only.",
                  code="ineligible_item", item_id=item_id)
    return snapshots


def _summary(items):
    logical = sum(item["logical_size"] for item in items)
    allocated = sum(item["allocated_size"] for item in items)
    return {
        "selected_count": len(items), "logical_size": logical,
        "allocated_size": allocated,
        "trash_retained_bytes_estimate": allocated,
        "reclaimable_bytes": None,
        "space_note": "Moving files to Trash retains their storage; allocated size is an estimate, not uniquely reclaimable bytes.",
    }


def make_plan(scan, selected_ids, *, keep_ids=None, cancel=None, progress=None):
    """Build a read-only proposal, hashing only explicit selected files/keepers.

    Content verification may take time for large selections. It binds every
    selected byte stream, so a same-size rewrite with restored modification time
    cannot become a different file's cleanup after capture changes ctime.
    """
    def check_cancel():
        if cancel and cancel():
            _fail("Stopped while verifying the selected files; no plan was created.", code="cancelled")

    check_cancel()
    selected = _selection(selected_ids, "Selection")
    requested_keepers = _selection(keep_ids or [], "Keepers")
    if not selected:
        _fail("Select at least one file before creating a plan.", code="empty_selection")
    if not isinstance(scan, dict) or not _identifier(scan.get("id")):
        _fail("A valid saved scan is required.", code="invalid_scan")
    if scan.get("status") not in ("complete", "partial", "cancelled"):
        _fail("This scan has no reviewable completed work.", code="invalid_scan")
    records = scan.get("items")
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        _fail("The scan's item inventory is invalid.", code="invalid_scan")
    by_id = {}
    for item in records:
        if not _identifier(item.get("id")) or item["id"] in by_id:
            _fail("The scan contains invalid or repeated item IDs.", code="invalid_scan")
        by_id[item["id"]] = item
    if any(item_id not in by_id for item_id in selected + requested_keepers):
        _fail("Selection includes an item that is not in this scan.", code="invalid_selection")
    if set(selected) & set(requested_keepers):
        _fail("A retained file cannot also be selected for Trash.", code="invalid_selection")
    items = [copy.deepcopy(by_id[item_id]) for item_id in selected]
    for item in items:
        # Rule-bound fields are recomputed from trusted current rule evaluation.
        item.pop("matched_rule", None)
        item.pop("rule_content_sha512", None)
        item.pop("content_sha256", None)
        _item_shape(item, require_rule=False)
    snapshots = _prepare_rules(items, scan.get("findings", []), cancel=cancel)
    check_cancel()
    physical_ids = set()
    for position, item in enumerate(items):
        check_cancel()
        _item_shape(item)
        identity = item["device"], item["inode"]
        if identity in physical_ids:
            _fail("The selection contains multiple aliases of one file.", code="invalid_selection")
        physical_ids.add(identity)
        assert_item_identity(item)
        item["content_sha256"] = hash_item(item, cancel=cancel)
        if progress:
            progress({"phase": "planning", "verified_files": position + 1, "selected_files": len(items)})

    keepers = {}
    accounted_keepers = set()
    selected_set = set(selected)
    selected_items = {item["id"]: item for item in items}
    seen_groups = set()
    grouped_items = set()
    groups = scan.get("duplicate_groups", [])
    if not isinstance(groups, list):
        _fail("Duplicate groups are invalid.", code="invalid_scan")
    hash_cache = {item["id"]: item["content_sha256"] for item in items}

    def checked_hash(item, expected):
        check_cancel()
        item_id = item["id"]
        assert_item_identity(item)
        if item_id not in hash_cache:
            hash_cache[item_id] = hash_item(item, cancel=cancel)
        if hash_cache[item_id] != expected:
            _fail("Duplicate content changed after scanning; rescan before planning.",
                  code="file_changed", item_id=item_id)

    for group in groups:
        if (
            not isinstance(group, dict) or not isinstance(group.get("item_ids"), list)
            or any(not _identifier(item_id) for item_id in group["item_ids"])
        ):
            _fail("A duplicate group is invalid.", code="invalid_scan")
        group_ids = group["item_ids"]
        if not selected_set.intersection(group_ids):
            continue
        group_id = group.get("id")
        if (
            not _identifier(group_id) or group_id in seen_groups or len(group_ids) < 2
            or len(set(group_ids)) != len(group_ids)
            or any(item_id not in by_id for item_id in group_ids)
            or not isinstance(group.get("sha256"), str) or not _SHA256.fullmatch(group["sha256"])
        ):
            _fail("A selected duplicate group is invalid.", code="invalid_scan")
        seen_groups.add(group_id)
        targets = selected_set.intersection(group_ids)
        if grouped_items & targets:
            _fail("A selected file belongs to conflicting duplicate groups.", code="invalid_scan")
        grouped_items.update(targets)
        retained = set(group_ids) - selected_set
        if not retained:
            _fail("At least one verified copy must be kept in each duplicate group.",
                  code="missing_keeper", group_id=group_id)
        explicit = retained.intersection(requested_keepers)
        if explicit:
            chosen = sorted(explicit)
        elif group.get("keeper_id") in retained:
            chosen = [group["keeper_id"]]
        else:
            chosen = [min(retained, key=lambda item_id: (by_id[item_id]["path"], item_id))]
        for keeper_id in chosen:
            keeper = copy.deepcopy(by_id[keeper_id])
            keeper.pop("content_sha256", None)
            _item_shape(keeper)
            assert_item_identity(keeper)
            checked_hash(keeper, group["sha256"])
            keeper["sha256"] = group["sha256"]
            keepers[keeper_id] = keeper
            accounted_keepers.add(keeper_id)
        for item_id in targets:
            item = selected_items[item_id]
            checked_hash(item, group["sha256"])
            item["sha256"] = group["sha256"]
            item["duplicate_group_id"] = group_id
            item["keeper_id"] = chosen[0]
    if set(requested_keepers) - accounted_keepers:
        _fail("A requested keeper does not belong to a selected duplicate group.",
              code="invalid_selection")
    check_cancel()
    plan = {
        "id": new_id("plan"), "schema_version": 1, "created_at": utc_now(),
        "scan_id": scan["id"], "action": "trash", "items": items,
        "keepers": list(keepers.values()), "rule_snapshots": snapshots,
        "summary": _summary(items), "status": "ready",
    }
    plan["digest"] = digest(plan)
    validate_plan(plan)
    return plan


def validate_plan(plan):
    """Validate saved plan structure and integrity; the executor rechecks live files."""
    if not isinstance(plan, dict) or set(plan) != _PLAN_KEYS:
        _fail("The saved plan has an unsupported structure.")
    if plan.get("schema_version") != 1 or type(plan.get("schema_version")) is not int:
        _fail("The saved plan schema is unsupported.")
    if not _identifier(plan.get("id")) or not plan["id"].startswith("plan-"):
        _fail("The saved plan ID is invalid.")
    if not _identifier(plan.get("scan_id")) or not isinstance(plan.get("created_at"), str):
        _fail("The saved plan provenance is invalid.")
    if plan.get("action") != "trash" or plan.get("status") != "ready":
        _fail("Only ready local Trash plans are supported.")
    try:
        expected = digest({key: value for key, value in plan.items() if key != "digest"})
    except (TypeError, ValueError, RecursionError) as exc:
        raise CleanMacError("The saved plan contains invalid JSON data.", code="invalid_plan") from exc
    if not isinstance(plan.get("digest"), str) or plan["digest"] != expected:
        _fail("The plan changed after review; create and review a new plan.", code="plan_changed")
    items, keepers, snapshots = plan["items"], plan["keepers"], plan["rule_snapshots"]
    if not isinstance(items, list) or not items or not isinstance(keepers, list) or not isinstance(snapshots, list):
        _fail("The plan must contain explicit selected files and valid keeper/rule lists.")
    all_ids, all_physical = set(), set()
    for item in items + keepers:
        _item_shape(item)
        if item["id"] in all_ids or (item["device"], item["inode"]) in all_physical:
            _fail("The plan repeats a selected file or keeper.")
        all_ids.add(item["id"])
        all_physical.add((item["device"], item["inode"]))
    kept = {item["id"]: item for item in keepers}
    used_keepers = set()
    for item in items:
        if not isinstance(item.get("content_sha256"), str) or not _SHA256.fullmatch(item["content_sha256"]):
            _fail("Each selected file requires its exact content fingerprint.")
        duplicate_fields = {"sha256", "duplicate_group_id", "keeper_id"}.intersection(item)
        if duplicate_fields:
            if duplicate_fields != {"sha256", "duplicate_group_id", "keeper_id"}:
                _fail("A duplicate target is missing its verified keeper.")
            if not _identifier(item["keeper_id"]):
                _fail("A duplicate target has an invalid keeper ID.")
            keeper = kept.get(item["keeper_id"])
            if (
                not isinstance(item["sha256"], str) or not _SHA256.fullmatch(item["sha256"])
                or item["sha256"] != item["content_sha256"]
                or not _identifier(item["duplicate_group_id"]) or keeper is None
                or keeper.get("sha256") != item["sha256"]
                or keeper["logical_size"] != item["logical_size"]
            ):
                _fail("A duplicate target and its retained copy are inconsistent.")
            used_keepers.add(keeper["id"])
    for keeper in keepers:
        if not isinstance(keeper.get("sha256"), str) or not _SHA256.fullmatch(keeper["sha256"]):
            _fail("A keeper requires a verified content hash.")
        if keeper["id"] not in used_keepers:
            # Additional explicitly retained copies may share the same content.
            if not any(target.get("sha256") == keeper["sha256"] for target in items):
                _fail("The plan contains an unrelated keeper.")
    item_map = {item["id"]: item for item in items}
    matched_snapshots = set()
    for snapshot in snapshots:
        if not isinstance(snapshot, dict) or snapshot.get("eligible") is not True:
            _fail("A saved cleanup rule is not eligible.")
        item_id = snapshot.get("item_id")
        if not _identifier(item_id):
            _fail("A cleanup rule snapshot has an invalid item ID.")
        target = item_map.get(item_id)
        if target is None or item_id in matched_snapshots:
            _fail("A cleanup rule snapshot has no unique selected item.")
        rule = target.get("matched_rule")
        if rule != {key: snapshot.get(key) for key in ("rule_id", "rule_version", "rule_digest")}:
            _fail("A selected item's cleanup rule does not match its snapshot.")
        if target.get("rule_content_sha512") != snapshot.get("content_sha512"):
            _fail("A selected item's content fingerprint does not match its cleanup rule snapshot.")
        matched_snapshots.add(item_id)
    if matched_snapshots != {item["id"] for item in items if item.get("matched_rule") is not None}:
        _fail("The plan is missing a selected item's rule snapshot.")
    if digest(plan["summary"]) != digest(_summary(items)):
        _fail("The plan summary does not match its exact selection.")

"""Command orchestration for CleanMac's scan/review/plan/apply workflow.

The dispatcher returns JSON-compatible records and never prints or asks questions.
Human interaction belongs to review.py; the agent can drive the same commands.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any

from .common import CleanMacError, display_path, new_id, utc_now

AREAS = ("files", "large", "duplicates", "caches", "storage")
DEFAULT_AREAS = ("files", "large", "storage")
DEFAULT_MIN_SIZE = 50 * 1024 * 1024
MAX_REVIEW_LIMIT = 100000
COOPERATIVE_COMMANDS = {"scan", "apply", "restore", "apps", "xcode", "overview"}


class _TerminalProgress:
    """Throttled counters only: paths and arbitrary scanner text never reach it."""
    _PHASES = {
        "inventory": "Scanning file metadata",
        "classifying": "Checking cleanup rules",
        "duplicates_grouping": "Grouping duplicate candidates",
        "duplicates_sampling": "Sampling duplicate candidates",
        "duplicates_hashing": "Hashing duplicate candidates",
        "duplicates_comparing": "Comparing duplicate contents",
        "duplicates_complete": "Duplicate scan complete",
        "duplicates_finished": "Duplicate scan complete",
        "duplicates_cancelled": "Duplicate scan stopped",
    }

    def __init__(self, stream=None, *, clock=None, interval=1.0):
        self.stream = stream if stream is not None else sys.stderr
        self.clock = clock or time.monotonic
        self.interval = interval
        self.started = self.clock()
        self.last_at = None
        self.last_phase = None
        self.disabled = False

    def __call__(self, event: dict) -> None:
        if self.disabled or not isinstance(event, dict):
            return
        now = self.clock()
        phase = event.get("phase")
        if not isinstance(phase, str):
            phase = "working"
        if self.last_at is not None and phase == self.last_phase and now - self.last_at < self.interval:
            return
        label = self._PHASES.get(phase, "Working")
        counts = event.get("counts", event)
        counts = counts if isinstance(counts, dict) else {}
        counters = []
        for key, name in (("files", "files"), ("entries_seen", "entries inspected"),
                          ("sampled", "sampled"), ("hashed", "hashed"),
                          ("compared", "compared"), ("groups", "groups")):
            value = counts.get(key)
            if type(value) is int and value >= 0:
                counters.append(f"{value} {name}")
        line = " · ".join([label, *counters, f"{max(0, now - self.started):.1f}s elapsed"])
        try:
            self.stream.write(line + "\n")
            self.stream.flush()
        except OSError:
            self.disabled = True
        self.last_at = now
        self.last_phase = phase


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CleanMacError(message, code="invalid_arguments")


def _global_options(parser: argparse.ArgumentParser) -> None:
    # SUPPRESS lets flags work before or after the subcommand without clobbering.
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="emit machine-readable JSON")
    parser.add_argument("--data-dir", default=argparse.SUPPRESS,
                        help="private state directory (never the skill source)")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="cleanmac", description="Inspect selected Mac files, review exact choices, and use recoverable Trash.")
    _global_options(parser)
    parser.set_defaults(json=False, data_dir=None)
    commands = parser.add_subparsers(dest="command")

    def command(name: str, help_text: str) -> argparse.ArgumentParser:
        child = commands.add_parser(name, help=help_text)
        _global_options(child)
        return child

    cap = command("capabilities", "report runtime, helper, and selected-root access")
    cap.add_argument("--root", action="append", default=[])
    scan = command("scan", "inventory only the named roots; never delete")
    scan.add_argument("--root", action="append", required=True)
    scan.add_argument("--areas", default=",".join(DEFAULT_AREAS))
    scan.add_argument("--max-items", type=int, default=100000)
    scan.add_argument("--min-size", type=int, default=DEFAULT_MIN_SIZE,
                      help="minimum logical bytes for large-file findings")
    scan.add_argument("--duplicate-min-size", type=int, default=1)
    review = command("review", "page through a saved scan")
    review.add_argument("scan_id")
    review.add_argument("--offset", type=int, default=0)
    review.add_argument("--limit", type=int, default=20)
    review.add_argument("--category")
    review.add_argument("--search")
    plan = command("plan", "prepare the exact selected files for review")
    plan.add_argument("scan_id")
    plan.add_argument("--select", action="append", nargs="+", required=True)
    plan.add_argument("--keep", action="append", nargs="+", default=[])
    apply = command("apply", "apply an unchanged plan with its exact approval digest")
    apply.add_argument("plan_id")
    apply.add_argument("--approve", required=True)
    restore = command("restore", "restore recoverable items without overwriting newer files")
    restore.add_argument("receipt_id")
    restore.add_argument("--approve", required=True)
    history = command("history", "list saved scans, plans, and receipts")
    history.add_argument("--id", dest="record_id", help="show one exact saved record for review")
    forget = command("forget", "remove one saved record, preserving recovery-bearing receipts")
    forget.add_argument("record_id")
    permissions = command("permissions", "explain permission handoffs; optionally open Settings")
    permissions.add_argument("--root", action="append", default=[])
    permissions.add_argument("--open-settings", action="store_true")
    permissions.add_argument("--pane", choices=("files-and-folders", "full-disk-access"), default="files-and-folders")
    command("rules", "show versioned cleanup-rule evidence and limitations")
    apps = command("apps", "read-only application inventory")
    apps.add_argument("--root", action="append")
    command("overview", "read-only system measurements")
    xcode = command("xcode", "report-only developer resource inventory")
    xcode.add_argument("--root", action="append")
    ignore = command("ignore", "manage exclusions for future scans")
    ignores = ignore.add_subparsers(dest="ignore_command", required=True)
    for name in ("list", "add", "remove"):
        child = ignores.add_parser(name)
        _global_options(child)
        if name != "list":
            child.add_argument("--path", required=True)
    return parser


def _native_path() -> str:
    configured = os.environ.get("CLEANMAC_NATIVE_PATH")
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    return str(Path(__file__).resolve().parents[2] / "native" / "bin" / "cleanmac-native")


def _roots(values: list[str]) -> list[str]:
    result = []
    for value in values:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise CleanMacError("Provide a nonempty filesystem location.", code="invalid_scope")
        path = os.path.abspath(os.path.expanduser(value))
        if path not in result:
            result.append(path)
    return result


def _ids(groups: list[list[str]]) -> list[str]:
    values = []
    for group in groups:
        for value in group:
            for part in value.split(","):
                part = part.strip()
                if part and part not in values:
                    values.append(part)
    if any("\x00" in value or len(value) > 200 for value in values):
        raise CleanMacError("Selection contains an invalid item ID.", code="invalid_selection")
    return values


def _macos_only() -> None:
    if sys.platform != "darwin":
        raise CleanMacError("CleanMac operations require macOS. No macOS command or cleanup was run.", code="unsupported_platform")


def menu_payload() -> dict:
    from .review import menu_payload as review_menu
    return review_menu()


def _merge_status(first: str, second: str) -> str:
    if "cancelled" in (first, second):
        return "cancelled"
    if first == "unavailable":
        return "unavailable"
    if first != "complete" or second != "complete":
        return "partial"
    return "complete"


def _scan(args: argparse.Namespace, store: Any, cancel=None, progress=None) -> dict:
    from .inventory import scan_roots
    from .rules import classify_items

    areas = list(dict.fromkeys(part.strip() for part in args.areas.split(",") if part.strip()))
    invalid = set(areas) - set(AREAS)
    if not areas or invalid:
        raise CleanMacError("Choose supported areas: " + ", ".join(AREAS), code="invalid_area",
                            details={"unsupported": sorted(invalid)})
    if not 1 <= args.max_items <= 1000000 or args.min_size < 0 or args.duplicate_min_size < 1:
        raise CleanMacError("max-items must be 1–1000000; min-size must be nonnegative; duplicate-min-size must be positive.", code="invalid_limit")
    roots = _roots(args.root)
    settings = store.get_settings()
    ignored = list(settings.get("ignore_paths", []))
    ignored.append(str(store.root))
    scan = scan_roots(roots, cancel=cancel, progress=progress, max_items=args.max_items, exclude_paths=ignored)
    scan.setdefault("schema_version", 1)
    scan.setdefault("id", new_id("scan"))
    scan.setdefault("created_at", utc_now())
    scan.setdefault("roots", roots)
    scan.setdefault("items", [])
    scan.setdefault("errors", [])
    scan.setdefault("status", "partial")
    scan["areas"] = areas
    scan["limits"] = {"max_items": args.max_items, "min_size": args.min_size, "duplicate_min_size": args.duplicate_min_size}
    scan["ignore_paths"] = settings.get("ignore_paths", [])
    # Classification always runs: selecting a generic-file area cannot evade cache protections.
    if scan["status"] in ("cancelled", "unavailable") or (cancel and cancel()):
        findings = []
    else:
        if progress:
            progress({"phase": "classifying"})
        findings = classify_items(scan["items"], cancel=cancel)
    if cancel and cancel():
        scan["status"] = "cancelled"
    scan["findings"] = findings
    classified = {finding.get("item_id") for finding in findings}
    for item in scan["items"]:
        if item.get("kind") != "file":
            continue
        if "large" in areas and item.get("logical_size", 0) >= args.min_size:
            findings.append({
                "id": "large-" + item["id"], "item_id": item["id"], "category": "large",
                "reason": "File meets the chosen size threshold; size alone does not make it disposable.",
                "confidence": "metadata", "eligible": False, "rule_id": None, "rule_version": None,
                "requires_explicit_selection": True,
            })
        if "files" in areas and item["id"] not in classified:
            findings.append({
                "id": "file-" + item["id"], "item_id": item["id"], "category": "files",
                "reason": "Regular file in the selected scope; no cleanup recommendation is implied.",
                "confidence": "metadata", "eligible": False, "rule_id": None, "rule_version": None,
                "requires_explicit_selection": True,
            })
    scan["duplicate_groups"] = []
    if "duplicates" in areas and scan["status"] not in ("cancelled", "unavailable"):
        from .duplicates import find_duplicates
        duplicates = find_duplicates(scan["items"], cancel=cancel, progress=progress, min_size=args.duplicate_min_size)
        scan["duplicate_groups"] = duplicates.get("groups", [])
        scan["errors"].extend(duplicates.get("errors", []))
        scan["status"] = _merge_status(scan["status"], duplicates.get("status", "partial"))
        scan["duplicate_status"] = duplicates.get("status", "partial")
    scan["completed_at"] = utc_now()
    unique_files = {}
    for item in scan["items"]:
        if item.get("kind") == "file":
            unique_files.setdefault((item.get("device"), item.get("inode")), item)
    scan["summary"] = {
        "item_count": len(scan["items"]),
        "file_count": sum(item.get("kind") == "file" for item in scan["items"]),
        "duplicate_group_count": len(scan["duplicate_groups"]),
        "finding_count": len(findings),
        "unique_file_count": len(unique_files),
        "logical_bytes": sum(item.get("logical_size", 0) for item in unique_files.values()),
        "allocated_bytes_estimate": sum(item.get("allocated_size", 0) for item in unique_files.values()),
        "selected_count": 0,
        "reclaimable_bytes": None,
        "accounting_note": "Hard-link aliases are counted once. Logical and allocated totals do not represent uniquely reclaimable storage; APFS clones and shared allocation remain uncertain.",
    }
    store.put("scans", scan)
    return scan


def _review(scan: dict, args: argparse.Namespace) -> dict:
    if args.offset < 0 or not 1 <= args.limit <= MAX_REVIEW_LIMIT:
        raise CleanMacError("offset must be nonnegative and limit must be 1–100000.", code="invalid_limit")
    items = scan.get("items", [])
    findings = scan.get("findings", [])
    groups = scan.get("duplicate_groups", [])
    if args.category:
        category = args.category.casefold()
        if category == "duplicates":
            included = {identifier for group in groups for identifier in group.get("item_ids", [])}
        elif category in ("storage", "all"):
            included = {item["id"] for item in items}
        else:
            included = {finding.get("item_id") for finding in findings
                        if finding.get("category", "").casefold() == category
                        or (category == "caches" and "cache" in finding.get("category", "").casefold())}
        items = [item for item in items if item["id"] in included]
    if args.search:
        needle = args.search.casefold()
        items = [item for item in items if needle in item.get("path", "").casefold()]
    total = len(items)
    page = items[args.offset:args.offset + args.limit]
    identifiers = {item["id"] for item in page}
    return {
        "schema_version": 1, "id": scan["id"], "scan_id": scan["id"],
        "created_at": scan.get("created_at"), "status": scan.get("status"),
        "roots": scan.get("roots", []), "areas": scan.get("areas", []),
        "counts": scan.get("counts", {}), "summary": scan.get("summary", {}),
        "errors": scan.get("errors", []), "items": page,
        "findings": [finding for finding in findings if finding.get("item_id") in identifiers],
        "duplicate_groups": [group for group in groups if identifiers.intersection(group.get("item_ids", []))],
        "pagination": {"offset": args.offset, "limit": args.limit, "total": total,
                       "next_offset": args.offset + len(page) if args.offset + len(page) < total else None},
    }


def _history(store: Any) -> dict:
    result = {"schema_version": 1, "status": "complete", "settings": store.get_settings()}
    for kind in ("scans", "plans", "receipts"):
        records = store.list(kind)
        result[kind] = [{key: record[key] for key in ("id", "created_at", "status", "scan_id", "plan_id", "action", "summary")
                         if key in record} for record in records]
    return result


def dispatch(argv: list[str] | None = None, *, store=None, cancel=None, progress=None) -> dict:
    """Run one command and return its record; never interact with stdin/stdout."""
    args = build_parser().parse_args(argv)
    command = args.command
    if command is None:
        return menu_payload()
    if command not in COOPERATIVE_COMMANDS and cancel and cancel():
        return {"schema_version": 1, "status": "cancelled", "items": [], "errors": [],
                "message": "Stopped before this operation began."}
    if command in ("capabilities", "permissions"):
        from .permissions import capabilities
        result = capabilities(_roots(args.root), native_path=_native_path())
        if command == "permissions" and args.open_settings:
            from .permissions import open_permission_settings
            result["settings_action"] = open_permission_settings(args.pane)
        return result
    if command == "rules":
        from .rules import load_rules
        return load_rules()
    if command in ("scan", "apply", "restore", "apps", "overview", "xcode"):
        _macos_only()
    if command in ("apps", "overview", "xcode"):
        from . import adapters
        if command == "apps":
            return adapters.scan_applications(_roots(args.root) if args.root else None, cancel=cancel)
        if command == "xcode":
            return adapters.scan_xcode(_roots(args.root) if args.root else None, cancel=cancel)
        return adapters.system_overview(cancel=cancel)
    if store is None:
        from .state import StateStore
        store = StateStore(args.data_dir)
    elif args.data_dir is not None and Path(args.data_dir).absolute() != Path(store.root).absolute():
        raise CleanMacError("This session already has a state directory; start a new session to switch it.", code="state_scope_mismatch")
    if command == "scan":
        return _scan(args, store, cancel, progress)
    if command == "review":
        return _review(store.get("scans", args.scan_id), args)
    if command == "plan":
        from .plans import make_plan
        selected = _ids(args.select)
        if not selected:
            raise CleanMacError("Select at least one exact item ID after reviewing the scan.", code="empty_selection")
        plan = make_plan(store.get("scans", args.scan_id), selected, keep_ids=_ids(args.keep) or None)
        store.put("plans", plan)
        return plan
    if command == "apply":
        from .executor import apply_plan
        return apply_plan(store.get("plans", args.plan_id), approval_digest=args.approve,
                          store=store, native_path=_native_path(), cancel=cancel)
    if command == "restore":
        if args.approve != args.receipt_id:
            raise CleanMacError("Restore approval must identify this exact receipt.", code="approval_mismatch")
        from .executor import restore_receipt
        return restore_receipt(store.get("receipts", args.receipt_id), store=store,
                               native_path=_native_path(), cancel=cancel)
    if command == "history":
        if args.record_id:
            matches = [record for kind in ("scans", "plans", "receipts")
                       for record in store.list(kind) if record.get("id") == args.record_id]
            if len(matches) != 1:
                raise CleanMacError("Saved record was not found or its ID is ambiguous.", code="record_not_found")
            return matches[0]
        return _history(store)
    if command == "forget":
        matches = [(kind, record) for kind in ("scans", "plans", "receipts")
                   for record in store.list(kind) if record.get("id") == args.record_id]
        if len(matches) != 1:
            raise CleanMacError("Saved record was not found or its ID is ambiguous.", code="record_not_found")
        kind, record = matches[0]
        # The state backend independently protects all recovery-bearing receipts.
        store.delete(kind, record["id"])
        return {"schema_version": 1, "status": "forgotten", "id": record["id"], "kind": kind,
                "message": "Removed this saved record; no scanned file was changed."}
    if command == "ignore":
        with store.lock("ignore-settings"):
            settings = store.get_settings()
            paths = list(settings.get("ignore_paths", []))
            if args.ignore_command != "list":
                path = _roots([args.path])[0]
                if args.ignore_command == "add" and path not in paths:
                    paths.append(path)
                elif args.ignore_command == "remove":
                    paths = [value for value in paths if value != path]
                settings["ignore_paths"] = sorted(paths)
                store.save_settings(settings)
        return {"schema_version": 1, "status": "complete", "ignore_paths": sorted(paths),
                "message": "Exclusions apply to future scans; saved plans and existing approvals are unchanged."}
    raise CleanMacError("Unknown command.", code="invalid_arguments")


def _human(result: dict, command: str | None) -> str:
    from .review import render_plan, render_review
    if command in ("scan", "review") and "items" in result:
        return render_review(result)
    if command == "plan":
        return render_plan(result)
    return json.dumps(result, indent=2, ensure_ascii=True)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in argv
    previous_handler = None
    requested = False
    operation_active = False

    def cancel() -> bool:
        return requested

    def on_interrupt(signum, frame) -> None:
        nonlocal requested
        if not operation_active:
            raise KeyboardInterrupt
        requested = True

    try:
        args = build_parser().parse_args(argv)
        json_mode = args.json
        operation_active = args.command in COOPERATIVE_COMMANDS
        if hasattr(signal, "SIGINT"):
            try:
                previous_handler = signal.signal(signal.SIGINT, on_interrupt)
            except ValueError:  # main() is also useful from a host's worker thread.
                previous_handler = None
        if args.command is None:
            if json_mode or not sys.stdin.isatty():
                result = menu_payload()
                print(json.dumps(result, ensure_ascii=True))
            else:
                from .review import interactive_wizard
                def wizard_dispatch(values: list[str]) -> dict:
                    nonlocal requested, operation_active
                    requested = False
                    invocation = list(values)
                    if args.data_dir:
                        invocation = ["--data-dir", args.data_dir] + invocation
                    operation_active = build_parser().parse_args(invocation).command in COOPERATIVE_COMMANDS
                    try:
                        reporter = _TerminalProgress() if sys.stderr.isatty() else None
                        return dispatch(invocation, cancel=cancel, progress=reporter)
                    finally:
                        operation_active = False
                interactive_wizard(wizard_dispatch, is_tty=True)
            return 0
        reporter = _TerminalProgress() if not json_mode and sys.stderr.isatty() else None
        result = dispatch(argv, cancel=cancel, progress=reporter)
        print(json.dumps(result, ensure_ascii=True) if json_mode else _human(result, args.command))
        return 130 if result.get("status") == "cancelled" else 0
    except CleanMacError as error:
        payload = {"schema_version": 1, "status": "error", "error": {
            "code": getattr(error, "code", "invalid_request"), "message": str(error),
            "details": getattr(error, "details", None)}}
        print(json.dumps(payload, ensure_ascii=True), file=sys.stdout if json_mode else sys.stderr)
        return 2
    except KeyboardInterrupt:
        payload = {"schema_version": 1, "status": "cancelled", "error": {
            "code": "cancelled", "message": "Stopped; inspect history before retrying an interrupted action."}}
        print(json.dumps(payload, ensure_ascii=True), file=sys.stdout if json_mode else sys.stderr)
        return 130
    except (OSError, ValueError, TypeError, KeyError) as error:
        payload = {"schema_version": 1, "status": "error", "error": {
            "code": "operation_failed", "message": "The operation could not be completed. No successful cleanup is implied.",
            "details": {"error_type": type(error).__name__}}}
        print(json.dumps(payload, ensure_ascii=True), file=sys.stdout if json_mode else sys.stderr)
        return 2
    finally:
        if previous_handler is not None:
            signal.signal(signal.SIGINT, previous_handler)

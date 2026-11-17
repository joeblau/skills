"""Display-safe review and a terminal conversation over the structured CLI.

The wizard never performs filesystem operations itself. Its dispatch callback
uses the same saved scans, immutable plans, and executor as noninteractive use.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any, Callable

from .common import CleanMacError, display_path


AREAS = ("files", "large", "duplicates", "caches", "storage")
PAGE_SIZE = 20


def _safe(value: Any) -> str:
    return display_path(str(value))


def format_bytes(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return "unknown"
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return "unknown"


def menu_payload() -> dict:
    return {
        "schema_version": 1,
        "status": "menu",
        "title": "CleanMac — choose what to inspect",
        "requires_input": True,
        "choices": [
            {"id": "quick", "label": "Quick check", "description": "Review Downloads files, large files, exact duplicates, and storage."},
            {"id": "areas", "label": "Choose areas", "description": "Choose available local scanners and an explicit folder."},
            {"id": "folder", "label": "One folder", "description": "Inspect files and storage in a chosen folder."},
            {"id": "history", "label": "History", "description": "Resume a saved review or inspect recovery records."},
            {"id": "tools", "label": "My Tools", "description": "Choose cleanup, app, browser, cloud, mail, maintenance, and health tools; inspect requirements before use."},
        ],
        "note": "No scan or cleanup has started. Select a scope; cleanup requires a reviewed plan.",
    }


def _findings(scan: dict) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for finding in scan.get("findings", []):
        result.setdefault(str(finding.get("item_id", "")), []).append(finding)
    return result


def _filtered_items(scan: dict, category: str | None = None, search: str | None = None) -> list[dict]:
    findings = _findings(scan)
    duplicates = {item_id for group in scan.get("duplicate_groups", []) for item_id in group.get("item_ids", [])}
    items = []
    for item in scan.get("items", []):
        if category and category not in ("all", "storage"):
            categories = {str(f.get("category", "")) for f in findings.get(str(item.get("id", "")), [])}
            categories.add(str(item.get("category", "")))
            if item.get("kind") == "file":
                categories.add("files")
            if item.get("id") in duplicates:
                categories.add("duplicates")
            if category not in categories:
                continue
        if search and search.casefold() not in str(item.get("path", "")).casefold():
            continue
        items.append(item)
    return items


def render_review(scan: dict, *, offset: int = 0, limit: int = PAGE_SIZE,
                  category: str | None = None, search: str | None = None,
                  selected_ids: set[str] | list[str] | None = None) -> str:
    """Render either a complete scan or a CLI review page without raw filenames."""
    if offset < 0 or limit < 1:
        raise CleanMacError("Review offset must be nonnegative and limit positive.")
    selected = set(selected_ids or [])
    items = _filtered_items(scan, category, search)
    pagination = scan.get("pagination", {})
    base = int(pagination.get("offset", 0)) if pagination and offset == 0 else offset
    page = items[offset:offset + limit]
    total = pagination.get("total", len(items)) if pagination and not category and not search else len(items)
    suggestions = {g.get("keeper_id") for g in scan.get("duplicate_groups", [])}
    findings = _findings(scan)
    lines = [f"Scan {_safe(scan.get('id', scan.get('scan_id', 'unknown')))} · {_safe(scan.get('status', 'unknown'))}"]
    if scan.get("roots"):
        lines.append("Scope: " + ", ".join(_safe(p) for p in scan["roots"]))
    if scan.get("errors"):
        lines.append(f"Coverage: {len(scan['errors'])} reported errors/skipped locations; inaccessible areas are not empty.")
    lines.append(f"Items {base + 1 if page else 0}–{base + len(page)} of {total}; {len(selected)} explicitly selected")
    lines.append("No.  Selected  Logical size  Item")
    for row, item in enumerate(page, start=base + 1):
        selected_marker = "yes" if item.get("id") in selected else "—"
        notes = []
        if item.get("kind") != "file":
            notes.append("inventory only")
        if item.get("protected"):
            notes.append("protected")
        if item.get("is_placeholder"):
            notes.append("cloud-only")
        if item.get("id") in suggestions:
            notes.append("suggested keeper")
        rules = findings.get(str(item.get("id", "")), [])
        if rules:
            notes.extend(str(f.get("category", "finding")) for f in rules)
        suffix = " [" + "; ".join(_safe(n) for n in notes) + "]" if notes else ""
        lines.append(f"{row:>3}  {selected_marker:<8}  {format_bytes(item.get('logical_size')):>11}  {_safe(item.get('path', ''))}{suffix}")
        lines.append(f"     ID: {_safe(item.get('id', ''))}")
    lines.append("Displayed sizes are logical file sizes, not uniquely reclaimable space.")
    return "\n".join(lines)


def render_plan(plan: dict) -> str:
    if plan.get("record_type") == "action_plan":
        return render_action_record(plan)
    items = plan.get("items", [])
    lines = [f"Plan {_safe(plan.get('id', 'unknown'))}: move {len(items)} selected file(s) to Trash."]
    for item in items:
        lines.append(f"  {_safe(item.get('path', ''))} · {format_bytes(item.get('logical_size'))}")
    if plan.get("keepers"):
        lines.append("Retain these duplicate keepers:")
        for item in plan["keepers"]:
            lines.append(f"  {_safe(item.get('path', ''))}")
    allocated = [item.get("allocated_size") for item in items]
    if all(isinstance(n, int) and n >= 0 for n in allocated):
        lines.append(f"Selected allocated estimate: {format_bytes(sum(allocated))}; shared allocation can make this uncertain.")
    else:
        lines.append("Selected allocated estimate: unknown.")
    lines.extend([
        "Trash retains these files and their storage until separately emptied. This action does not empty Trash.",
        "Changed, protected, or inaccessible files will be skipped or rejected; no permanent deletion fallback.",
        f"Approval digest: {_safe(plan.get('digest', 'missing'))}",
    ])
    return "\n".join(lines)


def _receipt_items(receipt: dict) -> list[dict]:
    values = receipt.get("items", receipt.get("operations", []))
    return list(values.values()) if isinstance(values, dict) else values


def render_receipt(receipt: dict) -> str:
    if receipt.get("record_type") == "action_receipt":
        return render_action_record(receipt)
    lines = [f"Receipt {_safe(receipt.get('id', 'unknown'))} · {_safe(receipt.get('status', 'unknown'))}"]
    for operation in _receipt_items(receipt):
        path = operation.get("original_path", operation.get("path", operation.get("item", {}).get("path", "")))
        lines.append(f"  {_safe(operation.get('status', 'unknown'))}: {_safe(path)}")
        if operation.get("reason") or operation.get("error"):
            lines.append("    " + _safe(operation.get("reason", operation.get("error"))))
    if receipt.get("summary"):
        lines.append(_safe(json.dumps(receipt["summary"], ensure_ascii=True, sort_keys=True)))
    lines.append("Trash-retained bytes are not freed space. Recovery is limited to verified recorded destinations.")
    return "\n".join(lines)


def render_action_record(record: dict) -> str:
    """Keep provider/system action semantics visible; never relabel them Trash."""
    return json.dumps(record, ensure_ascii=True, indent=2, sort_keys=True)


def render_tools(catalog: dict) -> str:
    tools = catalog.get("tools", [])
    lines = ["My Tools — installed implementations; access and dependencies are checked when invoked."]
    for tool in tools:
        marker = "★ " if tool.get("favorite") else ""
        lines.append(f"{marker}{_safe(tool.get('name', 'unknown'))}: {_safe(tool.get('title', ''))}")
        lines.append("  Actions: " + ", ".join(_safe(a) for a in tool.get("actions", [])))
        if tool.get("description"):
            lines.append("  " + _safe(tool["description"]))
        if len(tools) == 1:
            for field in ("requirements", "configuration", "examples", "usage", "action_usage", "approval_actions"):
                if tool.get(field):
                    lines.append(f"  {field.replace('_', ' ').capitalize()}: " + json.dumps(tool[field], ensure_ascii=True, sort_keys=True))
    if catalog.get("unavailable_modules"):
        lines.append(f"{len(catalog['unavailable_modules'])} module registration(s) unavailable; no results are implied for them.")
    lines.append("Listing tools does not scan files, connect accounts, request permissions, or start cleanup.")
    return "\n".join(lines)


def format_result(result: dict) -> str:
    if "tools" in result:
        return render_tools(result)
    if result.get("record_type") in ("action_plan", "action_receipt") or result.get("tool"):
        return render_action_record(result)
    if "digest" in result and "items" in result:
        return render_plan(result)
    if "operations" in result or str(result.get("id", "")).startswith("receipt-"):
        return render_receipt(result)
    if "items" in result:
        return render_review(result)
    return json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True)


def _resolve_ids(tokens: str, visible: list[dict], *, offset: int = 0) -> list[str]:
    """Numbers refer only to currently displayed rows; hidden rows need review."""
    by_id = {str(item.get("id")): item for item in visible}
    result = []
    for token in tokens.replace(",", " ").split():
        if token.isdecimal():
            index = int(token) - offset - 1
            if not 0 <= index < len(visible):
                raise CleanMacError("That row is not on the displayed page. Navigate to it first.")
            item_id = str(visible[index]["id"])
        elif token in by_id:
            item_id = token
        else:
            raise CleanMacError("Select a displayed row number or its exact item ID.")
        if item_id not in result:
            result.append(item_id)
    if not result:
        raise CleanMacError("Supply one or more displayed row numbers or item IDs.")
    return result


def interactive_wizard(dispatch: Callable[[list[str]], dict], *, input_fn=None,
                       output=None, is_tty: bool | None = None, home=None) -> dict:
    """Run a terminal review; non-TTY callers receive a menu without side effects."""
    if is_tty is None:
        is_tty = sys.stdin.isatty()
    if not is_tty:
        return menu_payload()
    ask = input_fn or input
    emit = output or print
    home = Path(home or Path.home())

    def prompt(text: str) -> str:
        return ask(text).strip()

    def call(argv: list[str]) -> dict | None:
        try:
            result = dispatch(argv)
            if not isinstance(result, dict):
                raise CleanMacError("The command did not return a structured result.")
            coverage_reports = {"permission-needed", "dependency-needed", "unavailable", "unsupported",
                                "manual-action-required", "scope-needed", "partial", "cancelled", "unscanned"}
            if result.get("status") == "error" or (result.get("error") and result.get("status") not in coverage_reports):
                error = result.get("error", result.get("message", "Command failed."))
                emit("Cannot continue: " + _safe(error.get("message", error) if isinstance(error, dict) else error))
                return None
            return result
        except CleanMacError as exc:
            emit("Cannot continue: " + _safe(str(exc)))
            return None

    def restore(receipt: dict) -> dict | None:
        emit(render_receipt(receipt))
        if receipt.get("record_type") == "action_receipt":
            emit("This is a tool action receipt. Open My Tools and select " + _safe(receipt.get("tool", "its tool")) + "; its supported recovery actions differ from file Trash recovery.")
            return None
        receipt_id = receipt.get("id")
        if not receipt_id:
            emit("This record has no recovery receipt ID.")
            return None
        emit("Restore the recorded files to their original locations. Existing newer files will not be overwritten.")
        if prompt("Type restore to proceed, or back: ").casefold() != "restore":
            return None
        result = call(["restore", str(receipt_id), "--approve", str(receipt_id)])
        if result is not None:
            emit(render_receipt(result))
        return result

    def my_tools() -> dict | None:
        available = call(["tools"])
        if available is None:
            return None
        specs = {spec.get("name"): spec for spec in available.get("tools", [])}
        while True:
            if "favorites" in specs:
                preferred = call(["tool", "favorites", "--action", "inspect"])
                names = preferred.get("favorites", []) if preferred is not None else []
                names = names if isinstance(names, list) else []
                ranked = {name: index for index, name in enumerate(names) if isinstance(name, str)}
                available["tools"] = [dict(spec, favorite=spec.get("name") in ranked) for spec in specs.values()]
                available["tools"].sort(key=lambda spec: (ranked.get(spec.get("name"), len(ranked)), spec.get("name", "")))
            emit(render_tools(available))
            name = prompt("Tool name, search TEXT, or back> ")
            if name.casefold() in ("back", "cancel", ""):
                return None
            if name.casefold().startswith("search "):
                needle = name[7:].casefold()
                matches = [spec for spec in specs.values() if needle in str(spec.get("name", "")).casefold() or needle in str(spec.get("title", "")).casefold()]
                emit(render_tools({"tools": matches}))
                name = prompt("Tool name or back> ")
                if name.casefold() in ("back", "cancel", ""):
                    continue
            spec = specs.get(name)
            if spec is None:
                emit("Choose a registered tool name from the list.")
                continue
            emit(render_tools({"tools": [spec]}))
            roots: list[str] = []
            selected: list[str] = []
            configuration: dict = {}
            record_id = None
            last_plan = None
            last_result: dict = {}
            reviewed_items: list[dict] = []
            emit("Choose scope for local files or config for account/tool options. Supply credential references, never tokens or passwords.")
            while True:
                actions = spec.get("actions", [])
                emit("Actions: " + ", ".join(_safe(action) for action in actions))
                emit("Commands: scope, config, select ROWS, record ID, show, back, done")
                entered = prompt(_safe(name) + "> ")
                verb, _, argument = entered.partition(" ")
                verb = verb.casefold()
                if verb in ("done", "quit", "exit"):
                    return {"status": "reviewed", "exit": True, "tool": name}
                if verb in ("back", "cancel"):
                    break
                try:
                    if verb == "scope":
                        remote = spec.get("scope_kind") == "remote"
                        root = prompt(("Remote folder ID" if remote else "Folder path") + " (blank clears scope; back keeps it)> ")
                        if root.casefold() == "back":
                            continue
                        roots = [root if remote else os.path.abspath(os.path.expanduser(root))] if root else []
                        selected, record_id, last_plan = [], None, None
                        reviewed_items, last_result = [], {}
                        emit("Selected scope: " + (", ".join(_safe(p) for p in roots) if roots else "none"))
                    elif verb == "config":
                        from .extensions import parse_configuration
                        raw = prompt("Configuration JSON, @private-file, or blank for defaults> ")
                        configuration = parse_configuration(path=raw[1:]) if raw.startswith("@") else parse_configuration(raw or "{}")
                        last_plan = None
                        emit("Configuration saved for this tool session.")
                    elif verb == "record":
                        detailed = call(["history", "--id", argument.strip()])
                        if detailed is not None:
                            owner = detailed.get("tool")
                            if owner and owner != name:
                                emit("That record belongs to a different tool.")
                                continue
                            record_id = detailed.get("id")
                            last_result = detailed
                            last_plan = detailed if detailed.get("digest") and detailed.get("id") else None
                            if isinstance(detailed.get("items"), list):
                                reviewed_items = [item for item in detailed["items"] if isinstance(item, dict) and item.get("id")]
                            emit(format_result(detailed))
                    elif verb == "select":
                        selected = _resolve_ids(argument, reviewed_items)
                        last_plan = None
                        emit("Selected exact IDs: " + ", ".join(_safe(value) for value in selected))
                    elif verb == "show":
                        emit(format_result(last_result) if last_result else "No tool result yet.")
                    elif verb in actions:
                        argv = ["tool", name, "--action", verb]
                        for root in roots:
                            argv += ["--root", root]
                        for item_id in selected:
                            argv += ["--select", item_id]
                        if configuration:
                            argv += ["--config", json.dumps(configuration, ensure_ascii=True, allow_nan=False)]
                        approval_action = verb in spec.get("approval_actions", ["apply"])
                        if approval_action:
                            if not last_plan or not last_plan.get("digest") or not last_plan.get("id"):
                                emit("Create and review a plan, or load an exact saved plan with record ID, before apply.")
                                continue
                            emit(format_result(last_plan))
                            emit("This applies the exact action and selection shown above. Review its scope, effects, and recovery limits.")
                            if prompt("Type " + verb + " to approve this unchanged plan, or back> ").casefold() != verb:
                                continue
                            argv += ["--record", str(last_plan["id"]), "--approve", str(last_plan["digest"])]
                        elif record_id:
                            argv += ["--record", str(record_id)]
                        result = call(argv)
                        if result is None:
                            continue
                        last_result = result
                        emit(format_result(result))
                        if result.get("digest") and result.get("id"):
                            last_plan = result
                        else:
                            if result.get("id"):
                                record_id = result["id"]
                            if approval_action:
                                last_plan = None
                        if isinstance(result.get("items"), list) and not result.get("digest"):
                            reviewed_items = [item for item in result["items"] if isinstance(item, dict) and item.get("id")]
                            for number, item in enumerate(reviewed_items, 1):
                                label = item.get("path", item.get("title", item.get("name", item["id"])))
                                emit(f"  {number}. {_safe(label)} · ID {_safe(item['id'])}")
                        if result.get("status") in ("permission-needed", "dependency-needed", "unavailable", "unsupported"):
                            emit("Review the reported requirements; retry only after resolving them, or choose back. OS permission prompts remain under your control.")
                    elif entered:
                        emit("Choose a listed action or command; no command text is executed as a shell.")
                except CleanMacError as exc:
                    emit(_safe(str(exc)))

    def review(scan: dict) -> dict:
        selected: set[str] = set()
        keepers: set[str] = set()
        offset = 0
        category = None
        search = None

        def keep_item(item: dict) -> None:
            if item.get("kind") == "directory":
                prefix = str(item.get("path", "")).rstrip(os.sep) + os.sep
                selected.difference_update(
                    str(child["id"]) for child in scan.get("items", [])
                    if str(child.get("path", "")).startswith(prefix)
                )
            else:
                item_id = str(item["id"])
                selected.discard(item_id)
                if any(item_id in group.get("item_ids", []) for group in scan.get("duplicate_groups", [])):
                    keepers.add(item_id)
        while True:
            filtered = _filtered_items(scan, category, search)
            offset = min(offset, max(0, ((len(filtered) - 1) // PAGE_SIZE) * PAGE_SIZE))
            visible = filtered[offset:offset + PAGE_SIZE]
            emit(render_review(scan, offset=offset, category=category, search=search, selected_ids=selected))
            emit("Commands: select ROWS, keep ROWS, inspect ROW, ignore ROWS, next, prev, search TEXT, category NAME, clear, plan, back, done")
            command = prompt("Review> ")
            verb, _, argument = command.partition(" ")
            verb = verb.casefold()
            try:
                if verb in ("back", "done", "quit", "cancel", "exit"):
                    return {"status": "reviewed", "scan_id": scan.get("id", scan.get("scan_id")), "selected_ids": sorted(selected), "exit": verb != "back"}
                if verb == "next":
                    offset = min(offset + PAGE_SIZE, max(0, ((len(filtered) - 1) // PAGE_SIZE) * PAGE_SIZE))
                elif verb in ("prev", "previous"):
                    offset = max(0, offset - PAGE_SIZE)
                elif verb in ("search", "category"):
                    if verb == "search":
                        search = argument or None
                    else:
                        category = None if argument in ("", "all") else argument
                    offset = 0
                elif verb == "clear":
                    selected.clear()
                elif verb in ("select", "keep", "keeper", "inspect", "ignore"):
                    ids = _resolve_ids(argument, visible, offset=offset)
                    by_id = {str(item["id"]): item for item in visible}
                    if verb == "select":
                        for item_id in ids:
                            item = by_id[item_id]
                            if item.get("kind") != "file" or item.get("protected") or item.get("is_placeholder") or item.get("nlink", 1) != 1:
                                emit("Not selectable: " + _safe(item.get("path", "")))
                                continue
                            selected.add(item_id)
                            keepers.discard(item_id)
                    elif verb in ("keep", "keeper"):
                        for item_id in ids:
                            keep_item(by_id[item_id])
                    elif verb == "inspect":
                        for item_id in ids:
                            item = by_id[item_id]
                            emit(json.dumps({"item": item, "findings": _findings(scan).get(item_id, [])}, ensure_ascii=True, indent=2, sort_keys=True))
                    else:
                        for item_id in ids:
                            item = by_id[item_id]
                            emit("Exclude from future scans: " + _safe(item.get("path", "")))
                            if prompt("Type ignore to save, or keep for this session only: ").casefold() == "ignore":
                                saved = call(["ignore", "add", "--path", str(item["path"])])
                                if saved is not None:
                                    emit("Saved for future scans; the current scan remains unchanged.")
                            keep_item(item)
                elif verb in ("plan", "clean"):
                    if not selected:
                        emit("Nothing is selected. Use select ROWS before creating a plan.")
                        continue
                    scan_id = scan.get("id", scan.get("scan_id"))
                    argv = ["plan", str(scan_id), "--select", ",".join(sorted(selected))]
                    relevant_keepers = {
                        item_id for group in scan.get("duplicate_groups", [])
                        if selected.intersection(group.get("item_ids", []))
                        for item_id in group.get("item_ids", []) if item_id in keepers
                    }
                    if relevant_keepers:
                        argv += ["--keep", ",".join(sorted(relevant_keepers))]
                    plan = call(argv)
                    if plan is None:
                        continue
                    emit(render_plan(plan))
                    if not plan.get("id") or not plan.get("digest") or plan.get("action") != "trash":
                        emit("This plan cannot be applied: missing identity/digest or unsupported action.")
                        continue
                    choice = prompt("Move exactly these files to Trash? Type trash, change, or cancel: ").casefold()
                    if choice not in ("trash", "yes"):
                        continue
                    receipt = call(["apply", str(plan["id"]), "--approve", str(plan["digest"])])
                    if receipt is None:
                        continue
                    emit(render_receipt(receipt))
                    selected.clear()
                    choice = prompt("Restore, review, or done? ").casefold()
                    if choice == "restore":
                        restored = restore(receipt)
                        return restored or receipt
                    if choice == "review":
                        refreshed = call(["review", str(scan_id), "--limit", "100000"])
                        if refreshed is not None:
                            scan = refreshed
                        emit("This is the saved scan. Create a new scan to discover filesystem changes.")
                        continue
                    return receipt
                elif command:
                    emit("Choose a listed command. Filenames are shown as data and are never commands.")
            except CleanMacError as exc:
                emit(_safe(str(exc)))

    def run_scan(roots: list[str], areas: list[str]) -> dict | None:
        emit("Read-only scope: " + ", ".join(_safe(root) for root in roots))
        emit("Areas: " + ", ".join(areas) + ". No files are selected for cleanup.")
        if prompt("Start this scan? [scan/back] ").casefold() not in ("scan", "yes"):
            return None
        emit("macOS may show an access prompt for the selected folder. You choose Allow or Don't Allow; denied locations can be skipped.")
        while True:
            argv = ["capabilities"]
            for root in roots:
                argv += ["--root", root]
            capabilities = call(argv)
            if capabilities is None:
                return None
            if capabilities.get("status") == "unavailable" and not capabilities.get("access"):
                emit(format_result(capabilities))
                return capabilities
            denied = []
            for access in capabilities.get("access", []):
                if access.get("status") in ("denied", "permission-needed", "unavailable", "blocked") or access.get("accessible") is False:
                    denied.append(access)
            if not denied:
                break
            for access in denied:
                emit("Access unavailable: " + _safe(access.get("path", access.get("root", "selected location"))))
                emit(_safe(access.get("message", access.get("reason", "macOS has restricted access."))))
                if access.get("guidance"):
                    emit(_safe(access["guidance"]))
            responsible = capabilities.get("permission_guidance", {}).get("host", {}).get("instruction")
            if responsible:
                emit(_safe(responsible))
            emit("Grant access only to the actual runner/terminal shown by macOS. A helper is not a replacement for Full Disk Access.")
            retry_access = False
            while True:
                choice = prompt("Continue with accessible items, retry after permission changes, settings, or back? ").casefold()
                if choice in ("back", "cancel"):
                    return None
                if choice == "settings":
                    pane = next((a.get("suggested_settings_pane") for a in denied if a.get("suggested_settings_pane") in ("files-and-folders", "full-disk-access")), "files-and-folders")
                    call(["permissions", "--open-settings", "--pane", pane])
                    emit("Change permissions yourself, then choose retry or continue. CleanMac does not accept the OS prompt for you.")
                    continue
                if choice == "retry":
                    retry_access = True
                    break
                if choice in ("continue", "skip"):
                    blocked = {a.get("path", a.get("root")) for a in denied}
                    roots = [root for root in roots if root not in blocked]
                    if not roots:
                        emit("All selected roots were skipped. No scan ran and no clean result is claimed.")
                        return {"status": "unavailable", "roots": [], "skipped": denied, "items": []}
                    break
                emit("Choose continue, retry, settings, or back.")
            if retry_access:
                continue
            break
        argv = ["scan"]
        for root in roots:
            argv += ["--root", root]
        argv += ["--areas", ",".join(areas)]
        scan = call(argv)
        if scan is None:
            return None
        if scan.get("status") == "unavailable":
            emit(format_result(scan))
            return scan
        return review(scan)

    try:
        emit("CleanMac: inspect local files, choose exact items, and review before cleanup.")
        while True:
            emit("1. Quick check (Downloads)\n2. Choose areas\n3. One folder\n4. History\n5. Done\n6. My Tools (cleanup, apps, browsers, cloud, mail, maintenance, health)")
            choice = prompt("Choose> ").casefold()
            result = None
            if choice in ("5", "done", "quit", "cancel", "exit"):
                return {"status": "cancelled", "message": "No further actions requested."}
            if choice in ("1", "quick", "quick check"):
                result = run_scan([str(home / "Downloads")], ["files", "large", "duplicates", "storage"])
            elif choice in ("2", "areas", "choose areas", "3", "folder", "one folder"):
                areas = ["files", "storage"]
                if choice in ("2", "areas", "choose areas"):
                    emit("Available local areas: " + ", ".join(AREAS))
                    emit("Other tool categories and their requirements are available in My Tools.")
                    entered = prompt("Areas (comma-separated; back to return)> ").casefold()
                    if entered in ("back", "cancel"):
                        continue
                    areas = list(dict.fromkeys(entered.replace(",", " ").split()))
                    if not areas or any(area not in AREAS for area in areas):
                        emit("Choose only the available areas listed above.")
                        continue
                folder = prompt("Folder path (back to return)> ")
                if folder.casefold() in ("back", "cancel") or not folder:
                    continue
                root = os.path.abspath(os.path.expanduser(folder))
                result = run_scan([root], areas)
            elif choice in ("6", "tools", "my tools"):
                result = my_tools()
            elif choice in ("4", "history"):
                history = call(["history"])
                if history is None:
                    continue
                for kind in ("scans", "plans", "receipts"):
                    emit(kind.capitalize() + ":")
                    for record in history.get(kind, []):
                        emit(f"  {_safe(record.get('id', ''))} · {_safe(record.get('status', 'unknown'))} · {_safe(record.get('created_at', ''))}")
                command = prompt("review SCAN_ID, show RECORD_ID, restore RECEIPT_ID, or back> ")
                verb, _, record_id = command.partition(" ")
                record_id = record_id.strip()
                if verb.casefold() == "review" and record_id:
                    scan = call(["review", record_id, "--limit", "100000"])
                    if scan is not None:
                        result = review(scan)
                elif verb.casefold() == "show" and record_id:
                    detailed = call(["history", "--id", record_id])
                    if detailed is not None:
                        emit(format_result(detailed))
                elif verb.casefold() == "restore" and record_id:
                    receipt = next((r for r in history.get("receipts", []) if r.get("id") == record_id), None)
                    if receipt is None:
                        emit("Choose a receipt listed in this history.")
                    else:
                        detailed = call(["history", "--id", record_id])
                        if detailed is not None:
                            result = restore(detailed)
            else:
                emit("Choose a menu number or name.")
            if result is not None and (result.get("exit") or result.get("status") not in ("reviewed", "unavailable")):
                result.pop("exit", None)
                return result
    except (EOFError, KeyboardInterrupt):
        emit("Stopped. No further cleanup action will be started.")
        return {"status": "cancelled", "message": "Interactive input ended or was interrupted."}


run_wizard = interactive_wizard

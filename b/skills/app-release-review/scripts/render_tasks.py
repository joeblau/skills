#!/usr/bin/env python3
"""Validate audit coverage and render local GitHub task drafts. No network writes."""

import argparse
from collections import Counter
from datetime import date
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlparse


class AuditError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise AuditError(message)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def strings(value, label, allow_empty=False):
    require(isinstance(value, list) and (allow_empty or bool(value)), f"{label}: expected a list")
    require(all(nonempty(item) for item in value), f"{label}: expected nonempty strings")
    require(len(value) == len(set(value)), f"{label}: duplicate values")


def iso_date(value, label):
    require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value), f"{label}: use YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise AuditError(f"{label}: invalid date") from exc


def records(value, label):
    require(isinstance(value, list), f"{label}: expected an array")
    require(all(isinstance(item, dict) for item in value), f"{label}: expected objects")
    return value


def text_fields(item, fields, label):
    for field in fields:
        require(nonempty(item.get(field)), f"{label}.{field}: required nonempty text")


def index_records(items, label):
    result = {}
    for item in records(items, label):
        ident = item.get("id")
        require(nonempty(ident), f"{label}: missing id")
        require(ident not in result, f"{label}: duplicate id {ident}")
        result[ident] = item
    return result


def http_url(value):
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"https", "http"} and bool(parsed.netloc) and not parsed.username and not parsed.password


def validate(audit, catalog):
    require(isinstance(audit, dict) and audit.get("schema_version") == 1, "schema_version must be 1")
    scope = audit.get("scope")
    require(isinstance(scope, dict), "scope must be an object")
    text_fields(scope, ("app", "version", "repository", "commit"), "scope")
    reviewed = iso_date(scope.get("reviewed_on"), "scope.reviewed_on")
    iso_date(scope.get("release_date"), "scope.release_date")
    strings(scope.get("platforms"), "scope.platforms")
    platforms = set(scope["platforms"])
    require(platforms <= {"ios", "android"}, "scope.platforms: unsupported platform")
    for field in ("builds", "markets", "locales", "devices", "accounts", "features", "limitations"):
        require(field in scope, f"scope.{field}: missing; explicitly record unknown facts")
    require(isinstance(scope["builds"], dict), "scope.builds must be an object")
    for platform in platforms:
        require(nonempty(scope["builds"].get(platform)), f"scope.builds.{platform}: record build or unknown")

    sources = index_records(audit.get("sources"), "sources")
    for ident, source in sources.items():
        text_fields(source, ("title", "section", "effective", "applicability", "interpretation"), f"source {ident}")
        require(http_url(source.get("url")), f"source {ident}: invalid direct HTTP(S) URL")
        require(iso_date(source.get("checked_on"), f"source {ident}.checked_on") <= reviewed,
                f"source {ident}: check date is after audit date")
        require(source.get("status") in {"verified", "unverified"}, f"source {ident}: invalid status")

    definitions = index_records(catalog["checks"], "catalog")
    for item in records(audit.get("additional_checks", []), "additional_checks"):
        ident = item.get("id")
        require(nonempty(ident) and re.fullmatch(r"EXTRA-[A-Z0-9-]+", ident), "additional checks must use EXTRA-* IDs")
        require(ident not in definitions, f"duplicate check definition {ident}")
        text_fields(item, ("title", "area", "trigger", "verify"), ident)
        strings(item.get("platforms"), f"{ident}.platforms")
        require(set(item["platforms"]) <= {"ios", "android"}, f"{ident}: invalid platforms")
        require(item.get("obligation") in {"required", "conditional", "recommended"}, f"{ident}: invalid obligation")
        strings(item.get("source_ids"), f"{ident}.source_ids")
        require(set(item["source_ids"]) <= sources.keys(), f"{ident}: undefined source")
        definitions[ident] = item

    expected = {f"{ident}@{p}" for ident, definition in definitions.items()
                for p in definition["platforms"] if p in platforms}
    checks = {}
    for check in records(audit.get("checks"), "checks"):
        text_fields(check, ("id", "platform", "trigger"), "check")
        ref = f"{check['id']}@{check['platform']}"
        require(ref in expected, f"unexpected check/platform {ref}")
        require(ref not in checks, f"duplicate check/platform {ref}")
        require(check.get("status") in {"pass", "fail", "unverified", "not-applicable"}, f"{ref}: invalid status")
        require(check.get("obligation") in {"required", "conditional", "recommended"}, f"{ref}: invalid obligation")
        strings(check.get("evidence"), f"{ref}.evidence")
        strings(check.get("source_ids"), f"{ref}.source_ids")
        strings(check.get("task_ids"), f"{ref}.task_ids", allow_empty=True)
        require(set(check["source_ids"]) <= sources.keys(), f"{ref}: undefined source")
        if check["status"] in {"pass", "fail"}:
            require(all(sources[s]["status"] == "verified" for s in check["source_ids"]),
                    f"{ref}: pass/fail needs verified policy sources; otherwise use unverified")
        if check["status"] in {"fail", "unverified"}:
            require(bool(check["task_ids"]), f"{ref}: unresolved check has no task")
        if check["status"] == "not-applicable":
            require(definitions[check["id"]]["trigger"] != "Always", f"{ref}: an always-applicable check cannot be excluded")
        checks[ref] = check
    missing = expected - checks.keys()
    require(not missing, "missing check/platform coverage: " + ", ".join(sorted(missing)[:12]))

    tasks = index_records(audit.get("tasks"), "tasks")
    keys = set()
    for ident, task in tasks.items():
        require(re.fullmatch(r"[A-Z0-9][A-Z0-9-]{0,79}", ident), f"unsafe task id {ident}")
        text_fields(task, ("title", "area", "blocker_reason", "owner_role", "remediation", "dedupe_key"), ident)
        for field in ("platforms", "check_refs", "evidence", "source_ids", "acceptance"):
            strings(task.get(field), f"{ident}.{field}")
        for field in ("depends_on", "artifacts", "blocking_platforms"):
            strings(task.get(field), f"{ident}.{field}", allow_empty=True)
        require(set(task["platforms"]) <= platforms, f"{ident}: platform outside scope")
        require(set(task["check_refs"]) <= checks.keys(), f"{ident}: undefined check reference")
        require(set(task["source_ids"]) <= sources.keys(), f"{ident}: undefined source")
        require(set(task["depends_on"]) <= tasks.keys(), f"{ident}: undefined dependency")
        require(task.get("kind") in {"native", "backend", "console", "documentation", "assets", "verification"}, f"{ident}: invalid kind")
        require(task.get("priority") in {"P0", "P1", "P2"}, f"{ident}: invalid priority")
        require(type(task.get("blocker")) is bool, f"{ident}: blocker must be boolean")
        require(set(task["blocking_platforms"]) <= set(task["platforms"]), f"{ident}: blocked platform outside task scope")
        require(bool(task["blocking_platforms"]) == task["blocker"], f"{ident}: blocker and blocking_platforms disagree")
        require(task["dedupe_key"] not in keys, f"{ident}: duplicate dedupe_key")
        require("\n" not in task["dedupe_key"] and "\r" not in task["dedupe_key"], f"{ident}: dedupe_key must be one line")
        keys.add(task["dedupe_key"])
        if "existing_issue" in task:
            require(isinstance(task["existing_issue"], str) and re.fullmatch(r"https://github\.com/[^/\s]+/[^/\s]+/issues/\d+", task["existing_issue"]),
                    f"{ident}: invalid existing_issue URL")
        for ref in task["check_refs"]:
            check = checks[ref]
            require(check["platform"] in task["platforms"], f"{ident}: platform missing for {ref}")
            require(ident in check["task_ids"], f"{ident}: missing check backlink for {ref}")
            require(set(check["source_ids"]) <= set(task["source_ids"]), f"{ident}: missing sources for {ref}")
            if check["status"] in {"fail", "unverified"} and check["obligation"] != "recommended":
                require(check["platform"] in task["blocking_platforms"],
                        f"{ident}: unresolved required/conditional work must remain a release gate for {check['platform']}")
    for ref, check in checks.items():
        for ident in check["task_ids"]:
            require(ident in tasks and ref in tasks[ident]["check_refs"], f"{ref}: missing task or backlink {ident}")

    order, visiting, visited = [], set(), set()

    def visit(ident):
        require(ident not in visiting, f"task dependency cycle at {ident}")
        if ident in visited:
            return
        visiting.add(ident)
        for dependency in sorted(tasks[ident]["depends_on"], key=lambda i: (tasks[i]["priority"], i)):
            visit(dependency)
        visiting.remove(ident)
        visited.add(ident)
        order.append(ident)

    for ident in sorted(tasks, key=lambda i: (tasks[i]["priority"], i)):
        visit(ident)
    inventory = records(audit.get("artifact_inventory"), "artifact_inventory")
    require(bool(inventory), "artifact_inventory must include planned release materials")
    paths = set()
    for artifact in inventory:
        text_fields(artifact, ("path", "owner_role"), "artifact")
        require(artifact["path"] not in paths, f"duplicate artifact {artifact['path']}")
        paths.add(artifact["path"])
        require(artifact.get("status") in {"draft", "missing", "verified", "not-applicable"}, "invalid artifact status")
        strings(artifact.get("task_ids"), "artifact.task_ids", allow_empty=True)
        require(set(artifact["task_ids"]) <= tasks.keys(), "artifact references undefined task")
        require(artifact["status"] != "missing" or bool(artifact["task_ids"]), "missing artifact has no task")
    return definitions, checks, sources, tasks, order


def cell(value):
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")


def issue_body(task, checks, sources):
    linked = [checks[ref] for ref in task["check_refs"]]
    lines = [f"# {task['title']}", "", f"Release task key: {task['dedupe_key']}", "",
             f"Platforms: {', '.join(task['platforms'])} · Area: {task['area']} · Kind: {task['kind']} · Priority: {task['priority']}",
             f"Owner role: {task['owner_role']}",
             f"Release gate: {'yes' if task['blocker'] else 'no'} — {task['blocker_reason']}", "",
             f"Blocked platforms: {', '.join(task['blocking_platforms']) or 'none'}", "",
             "## Obligation and applicability", ""]
    for ref, check in zip(task["check_refs"], linked):
        lines.append(f"- `{ref}`: {check['obligation']} · {check['status']} · {check['trigger']}")
    lines += ["", "## Evidence", ""] + [f"- {e}" for e in task["evidence"]]
    lines += ["", "## Work to complete", "", task["remediation"], "", "## Acceptance criteria", ""]
    lines += [f"- [ ] {item}" for item in task["acceptance"]]
    lines += ["", "## Dependencies", ""] + [f"- {item}" for item in task["depends_on"] or ["None"]]
    lines += ["", "## Artifacts", ""] + [f"- {item}" for item in task["artifacts"] or ["Evidence in the linked task results"]]
    lines += ["", "## Sources", ""]
    for ident in task["source_ids"]:
        source = sources[ident]
        lines += [f"- [{source['title']}]({source['url']}) — {source['section']}; checked {source['checked_on']}; {source['status']}",
                  f"  Effective scope: {source['effective']}. Applicability: {source['applicability']}. {source['interpretation']}"]
    if task.get("existing_issue"):
        lines += ["", f"Existing work: {task['existing_issue']}"]
    return "\n".join(lines) + "\n"


def render(audit, catalog, out, replace=False):
    definitions, checks, sources, tasks, order = validate(audit, catalog)
    scope = audit["scope"]
    checklist = [f"# Release checklist: {scope['app']} {scope['version']}", "",
                 f"Reviewed: {scope['reviewed_on']} · Intended release: {scope['release_date']}", "",
                 "Structural coverage and unresolved work; release readiness requires review of the actual evidence.", "",
                 f"Repository: {scope['repository']} · Commit: {scope['commit']}",
                 f"Builds: {json.dumps(scope['builds'], ensure_ascii=False)}", "",
                 "## Scope and limitations", ""]
    for field in ("markets", "locales", "devices", "accounts", "features", "limitations"):
        checklist.append(f"- {field}: {json.dumps(scope[field], ensure_ascii=False)}")
    checklist += [""]
    for platform in scope["platforms"]:
        selected = {ref: c for ref, c in checks.items() if c["platform"] == platform}
        counts = Counter(c["status"] for c in selected.values())
        gates = sum(c["status"] in {"fail", "unverified"} and c["obligation"] != "recommended" for c in selected.values())
        checklist += [f"## {platform}", "", f"Results: {dict(sorted(counts.items()))}. Unresolved required/conditional checks: {gates}.", "",
                      "| Check | Area / outcome | Obligation / trigger | Result / evidence | Sources | Tasks |",
                      "|---|---|---|---|---|---|"]
        for ref, check in selected.items():
            definition = definitions[check["id"]]
            links = ", ".join(f"[{i}](issues/{i}.md)" for i in check["task_ids"]) or "—"
            citations = "; ".join(f"[{i}]({sources[i]['url']}) ({sources[i]['checked_on']}, {sources[i]['status']})"
                                  for i in check["source_ids"])
            checklist.append("| " + " | ".join(map(cell, [ref, f"{definition['area']}: {definition['title']}",
                f"{check['obligation']}: {check['trigger']}", f"{check['status']}: {'; '.join(check['evidence'])}", citations, links])) + " |")
        checklist += [""]
    checklist += ["## Artifact inventory", "", "| Path | Status | Owner role | Tasks |", "|---|---|---|---|"]
    for artifact in audit["artifact_inventory"]:
        checklist.append("| " + " | ".join(map(cell, [artifact["path"], artifact["status"], artifact["owner_role"], ", ".join(artifact["task_ids"]) or "—"])) + " |")
    index = [f"# GitHub task drafts: {scope['app']} {scope['version']}", "",
             "Local drafts in dependency order. Publishing requires user authorization. No issues have been created by this helper.", "",
             "| Task | Priority | Area / kind | Platforms | Blocked platforms | Prerequisites |", "|---|---|---|---|---|---|"]
    files = {"CHECKLIST.md": "\n".join(checklist) + "\n"}
    for ident in order:
        task = tasks[ident]
        index.append("| " + " | ".join(map(cell, [f"[{ident}: {task['title']}](issues/{ident}.md)", task["priority"], f"{task['area']} / {task['kind']}",
            ", ".join(task["platforms"]), ", ".join(task["blocking_platforms"]) or "none", ", ".join(task["depends_on"]) or "—"])) + " |")
        files[f"issues/{ident}.md"] = issue_body(task, checks, sources)
    files["GITHUB-TASKS.md"] = "\n".join(index) + "\n"
    for name in files:
        path = out / name
        require(not path.is_symlink() and (not path.exists() or (replace and path.is_file())),
                f"refusing to overwrite {path}; use a fresh directory or --replace for generated regular files")
    require(not out.is_symlink() and not (out / "issues").is_symlink(), "output/issue directory must not be a symlink")
    out.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        path = out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return len(checks), len(tasks)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--replace", action="store_true", help="replace known generated regular files")
    args = parser.parse_args()
    try:
        audit = json.loads(args.audit.read_text(encoding="utf-8"))
        catalog_path = Path(__file__).resolve().parent.parent / "references" / "catalog.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        checks, tasks = render(audit, catalog, args.out, args.replace)
    except (AuditError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Audit error: {exc}", file=sys.stderr)
        return 1
    print(f"Validated {checks} check/platform results; rendered {tasks} local task drafts in {args.out}")
    print("Structural validation does not establish release readiness or create GitHub issues.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

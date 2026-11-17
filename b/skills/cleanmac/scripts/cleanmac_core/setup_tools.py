"""Explicit setup diagnostics and a disposable local workflow check."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile

from .common import CleanMacError, utc_now

TOOL_SPECS = [{
    "name": "setup", "title": "Setup and local workflow check",
    "feature_ids": ["CM-001", "CM-008"], "actions": ["inspect", "smoke"],
    "description": "Check runtime/setup requirements or exercise scan, duplicate, and plan behavior using disposable files.",
    "requirements": ["Python 3.10+", "macOS for system actions"],
    "configuration": {},
    "examples": ["tool setup --action inspect", "tool setup --action smoke"],
}]


def _inspect(roots):
    from .permissions import capabilities
    report = capabilities(roots, native_path=os.environ.get("CLEANMAC_NATIVE_PATH"))
    skill = Path(__file__).resolve().parents[2]
    # Filesystem executable discovery alone never launches installers or opens
    # a privacy-protected library. Selected root probes are explicit above.
    report["setup"] = {
        "native_trash": {
            "build_argv": ["/bin/sh", str(skill / "native" / "build.sh")],
            "requires": "Apple Command Line Tools or Xcode; explicit setup authorization",
            "built": report["helper"]["status"] == "available",
        },
        "native_features": {
            "build_argv": ["/bin/sh", str(skill / "native" / "build-features.sh")],
            "requires": "Build the optional app companion before using Photos, Vision, document versions, or screen cleaning.",
            "permission_note": "Each feature checks its own permission; installation does not grant access.",
        },
        "native_cloud": {
            "build_argv": ["/bin/sh", str(skill / "native" / "build-cloud.sh")],
            "requires": "Build the optional iCloud metadata/eviction helper; sign in and select a current, uploaded local item before eviction.",
        },
        "dependencies": {"python": sys.executable, "xcrun": shutil.which("xcrun"),
                         "clamav": shutil.which("clamscan"), "homebrew": shutil.which("brew")},
        "accounts": "Gmail and cloud tools require separately configured credential references. Tokens are not requested or displayed by this check.",
        "browser": "Browser automation requires the optional companion and explicit browser permissions. Inspect browser tool setup instructions.",
        "permissions": "Keep macOS consent dialogs under user control. Open only the requested Settings pane, then retry the same scope after access changes.",
        "native_roundtrip": "Not tested by setup or smoke. A real Trash/restore test needs access to the selected volume's Trash.",
    }
    return report


def _smoke(cancel=None, progress=None):
    from .duplicates import find_duplicates
    from .inventory import scan_roots
    from .plans import make_plan, validate_plan
    from .state import StateStore
    if cancel and cancel():
        return {"schema_version": 1, "status": "cancelled", "checks": []}
    checks = []
    with tempfile.TemporaryDirectory(prefix="cleanmac-smoke-") as temporary:
        base = Path(temporary).resolve()
        root = base / "selected"
        root.mkdir(mode=0o700)
        payload = b"CleanMac disposable validation fixture\n"
        (root / "keeper.txt").write_bytes(payload)
        (root / "copy.txt").write_bytes(payload)
        (root / "distinct.txt").write_bytes(b"Different generated content\n")
        scan = scan_roots([str(root)], cancel=cancel, progress=progress)
        if scan["status"] != "complete":
            return {"schema_version": 1, "status": scan["status"], "checks": checks,
                    "reason": "Disposable metadata inventory did not complete."}
        checks.append("metadata-inventory")
        duplicate = find_duplicates(scan["items"], cancel=cancel, progress=progress)
        if duplicate["status"] != "complete":
            return {"schema_version": 1, "status": duplicate["status"], "checks": checks}
        scan["duplicate_groups"] = duplicate["groups"]
        if len(duplicate["groups"]) != 1:
            raise CleanMacError("Disposable duplicate validation failed.", code="self_check_failed")
        checks.append("exact-duplicates")
        items = {Path(item["path"]).name: item for item in scan["items"]}
        plan = make_plan(scan, [items["copy.txt"]["id"]], keep_ids=[items["keeper.txt"]["id"]], cancel=cancel)
        validate_plan(plan)
        checks.append("selection-plan-and-retained-copy")
        fixture_store = StateStore(base / "state")
        fixture_store.put("scans", scan)
        fixture_store.put("plans", plan)
        if fixture_store.get("plans", plan["id"]) != plan:
            raise CleanMacError("Disposable state validation failed.", code="self_check_failed")
        checks.append("private-state-roundtrip")
        if (root / "copy.txt").read_bytes() != payload or (root / "keeper.txt").read_bytes() != payload:
            raise CleanMacError("Read-only smoke check changed a fixture.", code="self_check_failed")
    return {"schema_version": 1, "created_at": utc_now(), "status": "complete", "checks": checks,
            "scope": "Generated disposable fixtures only; fixture directory removed.",
            "native_trash_tested": False, "provider_accounts_tested": False,
            "permission_grants_changed": False, "user_files_changed": False}


def dispatch(name, action, options, *, store, cancel=None, progress=None):
    if name != "setup" or action not in ("inspect", "smoke"):
        raise CleanMacError("Unsupported setup action.", code="invalid_action")
    if options.get("configuration"):
        raise CleanMacError("Setup does not accept configuration or credentials.", code="invalid_config")
    if options.get("selected") or options.get("record_id") or options.get("approve"):
        raise CleanMacError("Setup does not execute saved action plans.", code="invalid_arguments")
    if action == "smoke":
        if options.get("roots"):
            raise CleanMacError("Smoke uses generated fixtures only; omit --root.", code="invalid_scope")
        return _smoke(cancel=cancel, progress=progress)
    return _inspect(options.get("roots", []))

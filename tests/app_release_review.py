"""Behavioral checks for local release-audit task rendering; all fixtures synthetic."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SKILL = Path(__file__).resolve().parents[1] / "b" / "skills" / "app-release-review"
spec = importlib.util.spec_from_file_location("render_tasks", SKILL / "scripts" / "render_tasks.py")
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)
CATALOG = json.loads((SKILL / "references" / "catalog.json").read_text())
SOURCES = json.loads((SKILL / "references" / "sources.json").read_text())["sources"]


def fixture(platforms=("ios", "android")):
    return {
        "schema_version": 1,
        "scope": {"app": "Synthetic QA fixture", "version": "1", "reviewed_on": "2026-09-30",
                  "release_date": "2026-10-01", "platforms": list(platforms), "repository": "synthetic",
                  "commit": "synthetic", "builds": {p: "synthetic-rc" for p in platforms},
                  "markets": ["US"], "locales": ["en"], "devices": ["synthetic"],
                  "accounts": "synthetic", "features": "synthetic", "limitations": ["Not an actual app audit"]},
        "sources": [dict(source, checked_on="2026-09-30", status="verified", section="Synthetic test input",
                         effective="Synthetic", applicability="Synthetic", interpretation="Synthetic structural test") for source in SOURCES],
        "checks": [dict(id=c["id"], platform=p, status="pass", obligation=c["obligation"], trigger="Synthetic applicability",
                        evidence=["Synthetic evidence fixture; no real compliance assertion"], source_ids=c["source_ids"], task_ids=[])
                   for c in CATALOG["checks"] for p in c["platforms"] if p in platforms],
        "tasks": [],
        "artifact_inventory": [{"path": "materials/synthetic.md", "status": "draft", "owner_role": "QA", "task_ids": []}],
    }


def unresolved(audit, ref, status="unverified", ident=None, blocker=True, depends=()):
    check_id, platform = ref.split("@")
    check = next(c for c in audit["checks"] if c["id"] == check_id and c["platform"] == platform)
    ident = ident or f"REL-{platform.upper()}-{check_id}"
    check.update(status=status, evidence=["Missing release-specific evidence for synthetic fixture"], task_ids=[ident])
    task = dict(id=ident, title=f"Verify {ref}", platforms=[platform], check_refs=[ref], kind="verification",
                area=next(c["area"] for c in CATALOG["checks"] if c["id"] == check_id) if not check_id.startswith("EXTRA-") else "compliance",
                priority="P1" if blocker else "P2", blocker=blocker, blocker_reason="Synthetic release applicability",
                blocking_platforms=[platform] if blocker else [],
                owner_role="QA", evidence=check["evidence"], source_ids=check["source_ids"],
                remediation="Obtain the missing build or test result and evaluate this exact check.",
                acceptance=["Attach release-build evidence and update the linked check result."],
                depends_on=list(depends), artifacts=[f"evidence/{ref}.md"], dedupe_key=f"synthetic:v1:{ref}")
    audit["tasks"].append(task)
    return task


class ReleaseReviewTests(unittest.TestCase):
    def test_bundled_catalog_sources_and_platform_coverage(self):
        ids = {s["id"] for s in SOURCES}
        self.assertEqual(len(CATALOG["checks"]), len({c["id"] for c in CATALOG["checks"]}))
        self.assertTrue(all(set(c["source_ids"]) <= ids for c in CATALOG["checks"]))
        audit = fixture()
        _, checks, _, _, _ = renderer.validate(audit, CATALOG)
        expected = sum(len(c["platforms"]) for c in CATALOG["checks"])
        self.assertEqual(len(checks), expected)
        audit["checks"] = [c for c in audit["checks"] if not (c["id"] == "DATA-08" and c["platform"] == "android")]
        with self.assertRaisesRegex(renderer.AuditError, "missing check/platform coverage"):
            renderer.validate(audit, CATALOG)

    def test_ios_subscription_and_android_deletion_work_stays_distinct(self):
        audit = fixture()
        unresolved(audit, "PAY-03@ios", "fail", ident="REL-IOS-PAYWALL")
        unresolved(audit, "DATA-08@android", ident="REL-ANDROID-BACKEND")
        unresolved(audit, "DATA-09@android", ident="REL-ANDROID-WEB", depends=("REL-ANDROID-BACKEND",))
        unresolved(audit, "A11Y-10@ios", ident="REL-IOS-LABELS", blocker=False)
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            renderer.render(audit, CATALOG, out)
            index = (out / "GITHUB-TASKS.md").read_text()
            self.assertLess(index.index("REL-ANDROID-BACKEND:"), index.index("REL-ANDROID-WEB:"))
            body = (out / "issues" / "REL-IOS-LABELS.md").read_text()
            self.assertIn("recommended", body)
            self.assertIn("Release gate: no", body)
            self.assertIn("- [ ] Attach release-build evidence", body)
            self.assertIn("checked 2026-09-30", body)
            self.assertIn("Release task key:", body)
            before = (out / "CHECKLIST.md").read_text()
            self.assertIn("https://developer.apple.com/", before)
            self.assertIn("Builds:", before)
            self.assertIn("Not an actual app audit", before)
            with self.assertRaises(renderer.AuditError):
                renderer.render(audit, CATALOG, out)
            self.assertEqual((out / "CHECKLIST.md").read_text(), before)
            renderer.render(audit, CATALOG, out, replace=True)

    def test_desktop_only_artifact_keeps_mobile_work_unverified(self):
        audit = fixture()
        audit["scope"]["builds"] = {"ios": "unknown", "android": "unknown"}
        audit["scope"]["limitations"] = ["Only an installed macOS reference bundle was supplied"]
        for c in audit["checks"]:
            unresolved(audit, f"{c['id']}@{c['platform']}", blocker=c["obligation"] != "recommended")
        with tempfile.TemporaryDirectory() as temp:
            count, tasks = renderer.render(audit, CATALOG, Path(temp))
            self.assertEqual(count, len(audit["checks"]))
            self.assertEqual(tasks, count)
            checklist = (Path(temp) / "CHECKLIST.md").read_text()
            self.assertIn("SCOPE-01@ios", checklist)
            self.assertIn("SCOPE-01@android", checklist)
            self.assertNotIn("not-applicable", checklist)

    def test_missing_evidence_unresolved_gate_and_stale_source_rejected(self):
        for mutation in (
            lambda a: a["checks"][0].update(evidence=[]),
            lambda a: a["checks"][0].update(status="unverified"),
            lambda a: a["sources"][0].update(status="unverified"),
            lambda a: a["checks"][0].update(status="not-applicable"),
            lambda a: a["sources"][0].update(checked_on="2026-10-01"),
        ):
            audit = fixture()
            mutation(audit)
            with self.assertRaises(renderer.AuditError):
                renderer.validate(audit, CATALOG)
        audit = fixture()
        unresolved(audit, "IOS-01@ios", blocker=False)
        with self.assertRaisesRegex(renderer.AuditError, "must remain a release gate"):
            renderer.validate(audit, CATALOG)

    def test_additional_policy_checks_are_required_to_be_evaluated(self):
        audit = fixture(("ios",))
        new_check = deepcopy(CATALOG["checks"][0])
        new_check.update(id="EXTRA-REGION-PERMIT", platforms=["ios"])
        audit["additional_checks"] = [new_check]
        with self.assertRaisesRegex(renderer.AuditError, "EXTRA-REGION-PERMIT@ios"):
            renderer.validate(audit, CATALOG)
        audit["checks"].append(dict(id=new_check["id"], platform="ios", status="unverified", obligation="conditional",
                                    trigger="Synthetic region permit", evidence=["Missing permit evidence"],
                                    source_ids=new_check["source_ids"], task_ids=[]))
        unresolved(audit, "EXTRA-REGION-PERMIT@ios")
        renderer.validate(audit, CATALOG)

    def test_shared_task_can_block_only_one_store(self):
        audit = fixture()
        task = unresolved(audit, "DATA-08@ios", ident="REL-SHARED-DELETE")
        task["platforms"] = ["ios", "android"]
        renderer.validate(audit, CATALOG)
        body = renderer.issue_body(task, {f"{c['id']}@{c['platform']}": c for c in audit["checks"]},
                                   {s["id"]: s for s in audit["sources"]})
        self.assertIn("Blocked platforms: ios", body)
        self.assertNotIn("Blocked platforms: ios, android", body)

    def test_dependency_cycles_duplicate_keys_and_unsafe_paths_rejected(self):
        audit = fixture()
        first = unresolved(audit, "DATA-08@android", ident="REL-BACKEND")
        second = unresolved(audit, "DATA-09@android", ident="REL-WEB", depends=("REL-BACKEND",))
        first["depends_on"] = ["REL-WEB"]
        with self.assertRaisesRegex(renderer.AuditError, "cycle"):
            renderer.validate(audit, CATALOG)
        first["depends_on"] = []
        second["dedupe_key"] = first["dedupe_key"]
        with self.assertRaisesRegex(renderer.AuditError, "duplicate dedupe_key"):
            renderer.validate(audit, CATALOG)
        second["dedupe_key"] = "different"
        second["id"] = "../../outside"
        with self.assertRaises(renderer.AuditError):
            renderer.validate(audit, CATALOG)
        second["id"] = "REL-WEB"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            out = root / "output"
            external = root / "external"
            out.mkdir()
            external.mkdir()
            (out / "issues").symlink_to(external, target_is_directory=True)
            with self.assertRaises(renderer.AuditError):
                renderer.render(audit, CATALOG, out)
            self.assertFalse((out / "CHECKLIST.md").exists())
            self.assertEqual(list(external.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

"""Behavioral checks for exact, non-mutating Trash plans."""

import copy
import hashlib
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cleanmac_core.common import CleanMacError, digest
from cleanmac_core.duplicates import find_duplicates
from cleanmac_core.inventory import scan_roots
from cleanmac_core.plans import make_plan, validate_plan
from cleanmac_core import plans


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cleanmac-plans-")
        self.root = Path(self.temp.name).resolve()
        self.a = self.root / "first.txt"
        self.b = self.root / "second.txt"
        self.a.write_bytes(b"first content")
        self.b.write_bytes(b"second content")
        self.rule_patch = patch("cleanmac_core.plans.classify_items", return_value=[])
        self.classify = self.rule_patch.start()
        self.addCleanup(self.rule_patch.stop)
        self.addCleanup(self.temp.cleanup)

    def scan(self):
        return scan_roots([str(self.root)])

    def item(self, scan, path):
        return next(item for item in scan["items"] if item["path"] == str(path))

    def resign(self, plan):
        plan["digest"] = digest({key: value for key, value in plan.items() if key != "digest"})
        return plan

    def duplicates(self):
        self.b.write_bytes(self.a.read_bytes())
        scan = self.scan()
        scan["duplicate_groups"] = find_duplicates(scan["items"])["groups"]
        return scan

    def test_plan_is_exact_and_does_not_change_scan_or_files(self):
        scan = self.scan()
        original = copy.deepcopy(scan)
        a = self.item(scan, self.a)
        plan = make_plan(scan, [a["id"]])
        validate_plan(plan)
        self.assertEqual([item["path"] for item in plan["items"]], [str(self.a)])
        self.assertEqual(scan, original)
        self.assertEqual(self.a.read_bytes(), b"first content")
        self.assertEqual(self.b.read_bytes(), b"second content")
        self.assertEqual(plan["summary"]["selected_count"], 1)
        self.assertIsNone(plan["summary"]["reclaimable_bytes"])
        self.assertNotIn("approval", plan)
        self.assertEqual(plan["items"][0]["content_sha256"], hashlib.sha256(b"first content").hexdigest())
        self.assertNotIn("duplicate_group_id", plan["items"][0])

    def test_content_hashing_reads_only_selected_files_and_required_keepers(self):
        scan = self.scan()
        target = self.item(scan, self.a)
        with patch("cleanmac_core.plans.hash_item", wraps=plans.hash_item) as hashing:
            plan = make_plan(scan, [target["id"]])
        self.assertEqual([call.args[0]["id"] for call in hashing.call_args_list], [target["id"]])
        missing = copy.deepcopy(plan)
        missing["items"][0].pop("content_sha256")
        self.resign(missing)
        with self.assertRaises(CleanMacError):
            validate_plan(missing)

    def test_plan_hashing_observes_cancellation_and_emits_real_progress(self):
        scan = self.scan()
        targets = [self.item(scan, path)["id"] for path in (self.a, self.b)]
        progress = []
        with self.assertRaises(CleanMacError) as caught:
            make_plan(scan, targets, cancel=lambda: bool(progress), progress=progress.append)
        self.assertEqual(caught.exception.code, "cancelled")
        self.assertEqual(progress, [{"phase": "planning", "verified_files": 1, "selected_files": 2}])
        self.assertEqual(self.a.read_bytes(), b"first content")
        self.assertEqual(self.b.read_bytes(), b"second content")

    def test_empty_and_unknown_selections_rejected(self):
        scan = self.scan()
        for selected in ([], ["item-missing"], "all"):
            with self.subTest(selected=selected), self.assertRaises(CleanMacError):
                make_plan(scan, selected)

    def test_repeated_selection_rejected(self):
        scan = self.scan()
        a = self.item(scan, self.a)
        with self.assertRaises(CleanMacError):
            make_plan(scan, [a["id"], a["id"]])

    def test_directory_and_hardlink_rejected(self):
        folder = self.root / "folder"
        folder.mkdir()
        os.link(self.a, self.root / "linked.txt")
        scan = self.scan()
        for path in (self.a, folder):
            with self.subTest(path=path), self.assertRaises(CleanMacError):
                make_plan(scan, [self.item(scan, path)["id"]])

    def test_protected_and_placeholder_selection_rejected(self):
        scan = self.scan()
        for field in ("protected", "is_placeholder"):
            modified = copy.deepcopy(scan)
            item = self.item(modified, self.a)
            item[field] = True
            with self.subTest(field=field), self.assertRaises(CleanMacError):
                make_plan(modified, [item["id"]])

    def test_changed_file_rejected(self):
        scan = self.scan()
        a = self.item(scan, self.a)
        self.a.write_bytes(b"a changed document")
        with self.assertRaises(CleanMacError):
            make_plan(scan, [a["id"]])

    def test_substituted_ancestor_rejected(self):
        parent = self.root / "nested"
        parent.mkdir()
        target = parent / "document.txt"
        target.write_bytes(b"nested document")
        scan = self.scan()
        target_id = self.item(scan, target)["id"]
        renamed = self.root / "moved"
        parent.rename(renamed)
        parent.symlink_to(renamed, target_is_directory=True)
        with self.assertRaises(CleanMacError):
            make_plan(scan, [target_id])
        self.assertEqual((renamed / "document.txt").read_bytes(), b"nested document")

    def test_app_data_without_validated_rule_rejected(self):
        scan = self.scan()
        item = self.item(scan, self.a)
        item["is_app_data"] = True
        with self.assertRaises(CleanMacError):
            make_plan(scan, [item["id"]])

    def test_rule_eligible_snapshot_and_revision_are_bound(self):
        scan = self.scan()
        item = self.item(scan, self.a)
        item["is_app_data"] = True
        finding = {
            "id": "finding-test", "item_id": item["id"], "eligible": True,
            "rule_id": "fixture-cache", "rule_version": "1", "rule_digest": "a" * 64, "category": "cache",
            "reason": "disposable fixture", "confidence": "verified",
        }
        scan["findings"] = [finding]
        self.classify.return_value = [finding]
        plan = make_plan(scan, [item["id"]])
        self.assertEqual(plan["items"][0]["matched_rule"], {"rule_id": "fixture-cache", "rule_version": "1", "rule_digest": "a" * 64})
        self.assertEqual(plan["rule_snapshots"][0]["item_id"], item["id"])
        self.classify.return_value = [{**finding, "rule_version": "2"}]
        with self.assertRaises(CleanMacError):
            make_plan(scan, [item["id"]])

    def test_rule_denial_or_disappeared_match_rejected(self):
        scan = self.scan()
        item = self.item(scan, self.a)
        finding = {"item_id": item["id"], "rule_id": "fixture-cache", "rule_version": "1", "rule_digest": "a" * 64, "eligible": True}
        scan["findings"] = [finding]
        for result in ([], [{**finding, "eligible": False}]):
            self.classify.return_value = result
            with self.subTest(result=result), self.assertRaises(CleanMacError):
                make_plan(scan, [item["id"]])

    def test_duplicate_plan_retains_verified_keeper(self):
        scan = self.duplicates()
        a, b = self.item(scan, self.a), self.item(scan, self.b)
        plan = make_plan(scan, [a["id"]], keep_ids=[b["id"]])
        validate_plan(plan)
        self.assertEqual(plan["keepers"][0]["id"], b["id"])
        self.assertEqual(plan["items"][0]["keeper_id"], b["id"])
        self.assertEqual(plan["items"][0]["sha256"], plan["keepers"][0]["sha256"])

    def test_all_duplicate_copies_cannot_be_selected(self):
        scan = self.duplicates()
        selected = [self.item(scan, path)["id"] for path in (self.a, self.b)]
        with self.assertRaises(CleanMacError):
            make_plan(scan, selected)

    def test_changed_keeper_cannot_support_duplicate_removal(self):
        scan = self.duplicates()
        a, b = self.item(scan, self.a), self.item(scan, self.b)
        self.b.write_bytes(b"different keeper")
        with self.assertRaises(CleanMacError):
            make_plan(scan, [a["id"]], keep_ids=[b["id"]])

    def test_keeper_cannot_be_selected_or_unrelated(self):
        scan = self.duplicates()
        a = self.item(scan, self.a)
        with self.assertRaises(CleanMacError):
            make_plan(scan, [a["id"]], keep_ids=[a["id"]])
        ordinary = self.scan()
        with self.assertRaises(CleanMacError):
            make_plan(ordinary, [self.item(ordinary, self.a)["id"]], keep_ids=[self.item(ordinary, self.b)["id"]])

    def test_plan_digest_detects_modified_selection(self):
        scan = self.scan()
        plan = make_plan(scan, [self.item(scan, self.a)["id"]])
        plan["items"][0]["path"] = str(self.b)
        with self.assertRaises(CleanMacError):
            validate_plan(plan)

    def test_recomputed_digest_does_not_allow_unsafe_shape(self):
        scan = self.scan()
        original = make_plan(scan, [self.item(scan, self.a)["id"]])
        mutations = [
            lambda p: p.update(action="permanent-delete"),
            lambda p: p["items"][0].update(mode=stat.S_IFLNK | 0o777),
            lambda p: p["items"][0].update(ancestors=[]),
            lambda p: p["items"][0].update(root="/unrelated"),
            lambda p: p["items"][0].update(protected=True),
            lambda p: p["summary"].update(selected_count=999),
        ]
        for mutate in mutations:
            plan = copy.deepcopy(original)
            mutate(plan)
            self.resign(plan)
            with self.subTest(plan=plan), self.assertRaises(CleanMacError):
                validate_plan(plan)

    def test_partial_scan_can_plan_only_known_selected_files(self):
        scan = self.scan()
        scan["status"] = "partial"
        plan = make_plan(scan, [self.item(scan, self.a)["id"]])
        validate_plan(plan)
        scan["status"] = "unavailable"
        with self.assertRaises(CleanMacError):
            make_plan(scan, [self.item(scan, self.a)["id"]])

    def test_malformed_group_and_rule_ids_report_structured_errors(self):
        scan = self.scan()
        selected = [self.item(scan, self.a)["id"]]
        scan["duplicate_groups"] = [{"item_ids": [{"unexpected": "object"}]}]
        with self.assertRaises(CleanMacError):
            make_plan(scan, selected)
        scan.pop("duplicate_groups")
        scan["findings"] = [{"item_id": [], "rule_id": "fixture"}]
        with self.assertRaises(CleanMacError):
            make_plan(scan, selected)

    def test_same_version_changed_rule_content_requires_rescan(self):
        scan = self.scan()
        item = self.item(scan, self.a)
        finding = {"item_id": item["id"], "rule_id": "fixture-cache", "rule_version": "1", "rule_digest": "a" * 64, "eligible": True}
        scan["findings"] = [finding]
        self.classify.return_value = [{**finding, "rule_digest": "b" * 64}]
        with self.assertRaises(CleanMacError):
            make_plan(scan, [item["id"]])

    def test_rule_content_hash_is_bound_to_selected_item_and_snapshot(self):
        scan = self.scan()
        item = self.item(scan, self.a)
        fingerprint = hashlib.sha512(self.a.read_bytes()).hexdigest()
        finding = {"item_id": item["id"], "rule_id": "fixture-cache", "rule_version": "1", "rule_digest": "a" * 64, "eligible": True, "content_sha512": fingerprint}
        scan["findings"] = [finding]
        self.classify.return_value = [finding]
        plan = make_plan(scan, [item["id"]])
        self.assertEqual(plan["items"][0]["rule_content_sha512"], fingerprint)
        self.assertEqual(plan["rule_snapshots"][0]["content_sha512"], fingerprint)
        plan["items"][0]["rule_content_sha512"] = "b" * 128
        self.resign(plan)
        with self.assertRaises(CleanMacError):
            validate_plan(plan)

    def test_recomputed_digest_rejects_invalid_keeper_reference(self):
        scan = self.duplicates()
        plan = make_plan(scan, [self.item(scan, self.a)["id"]])
        plan["items"][0]["keeper_id"] = {"unexpected": "object"}
        self.resign(plan)
        with self.assertRaises(CleanMacError):
            validate_plan(plan)


if __name__ == "__main__":
    unittest.main()

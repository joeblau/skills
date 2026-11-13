"""Real cache classification/plan/executor integration on disposable files only.

The native boundary is replaced with a fixture-only move; macOS Trash is never
called. A fixture home and deterministic process snapshot isolate real npm data.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cleanmac_core import executor, rules
from cleanmac_core.common import CleanMacError
from cleanmac_core.inventory import scan_roots
from cleanmac_core.plans import make_plan
from cleanmac_core.state import StateStore


class RuleExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cleanmac-rule-execution-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "fixture-home"
        self.home.mkdir(mode=0o700)
        self.cache = self.home / ".npm/_cacache"
        self.fake_trash = self.root / "fixture-trash"
        self.fake_trash.mkdir(mode=0o700)
        self.store = StateStore(str(self.root / "state"))
        self.addCleanup(patch.stopall)
        patch.object(rules, "_home_path", return_value=self.home).start()
        patch.object(rules, "detect_active_apps", return_value=[]).start()
        patch.object(executor, "_native_executable", return_value=self.root / "never-executed-helper").start()
        self.recovery_preflight = patch.object(
            executor, "_preflight_recovery", return_value=str(self.fake_trash)
        ).start()
        self.native = patch.object(executor, "_native_trash", side_effect=self.fixture_trash).start()

    def fixture_trash(self, native, stage_path, receipt_path):
        destination = self.fake_trash / stage_path.name
        os.rename(stage_path, destination)
        return {"status": "trashed", "original_path": str(stage_path), "trash_path": str(destination)}

    def create(self, content=b"cached package bytes"):
        value = hashlib.sha512(content).hexdigest()
        self.path = self.cache / "content-v2/sha512" / value[:2] / value[2:4] / value[4:]
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(content)
        self.scan = scan_roots([str(self.cache)])
        self.scan["findings"] = rules.classify_items(self.scan["items"])
        self.item = next(item for item in self.scan["items"] if item["path"] == str(self.path))
        self.assertFalse(self.item["protected"])
        self.assertTrue(self.item["is_app_data"])
        self.assertTrue(next(f for f in self.scan["findings"] if f["item_id"] == self.item["id"])["eligible"])
        return self.path

    def plan(self):
        return make_plan(self.scan, [self.item["id"]])

    def apply(self, plan):
        return executor.apply_plan(plan, approval_digest=plan["digest"], store=self.store)

    def changed_catalog(self):
        catalog = copy.deepcopy(rules.load_rules())
        catalog["rules"][0]["effects"] += " Changed after review."
        location = self.root / "changed-catalog.json"
        location.write_text(json.dumps(catalog))
        return location

    def test_valid_rule_plan_binds_digest_and_only_moves_fixture(self):
        self.create()
        plan = self.plan()
        self.assertEqual(plan["items"][0]["matched_rule"]["rule_digest"], plan["rule_snapshots"][0]["rule_digest"])
        receipt = self.apply(plan)
        self.assertEqual(receipt["operations"][0]["status"], "trashed")
        self.recovery_preflight.assert_called_once()
        self.assertEqual(self.native.call_count, 1)
        self.assertEqual((self.fake_trash / self.path.name).read_bytes(), b"cached package bytes")

    def test_same_version_changed_rule_blocks_plan(self):
        self.create()
        with patch.object(rules, "_CATALOG", self.changed_catalog()):
            with self.assertRaises(CleanMacError) as caught:
                self.plan()
        self.assertEqual(caught.exception.code, "rule_changed")
        self.assertTrue(self.path.exists())
        self.native.assert_not_called()

    def test_same_version_changed_rule_blocks_apply_before_capture(self):
        self.create()
        plan = self.plan()
        with patch.object(rules, "_CATALOG", self.changed_catalog()):
            receipt = self.apply(plan)
        self.assertEqual(receipt["operations"][0]["error"]["code"], "rule_changed")
        self.assertTrue(self.path.exists())
        self.native.assert_not_called()

    def test_new_writer_blocks_plan_and_apply(self):
        self.create()
        plan = self.plan()
        with patch.object(rules, "detect_active_apps", return_value=["node"]):
            with self.assertRaises(CleanMacError):
                self.plan()
            receipt = self.apply(plan)
        self.assertEqual(receipt["operations"][0]["error"]["code"], "rule_changed")
        self.assertTrue(self.path.exists())
        self.native.assert_not_called()

    def test_unknown_writer_snapshot_blocks_apply(self):
        self.create()
        plan = self.plan()
        with patch.object(rules, "detect_active_apps", return_value=None):
            receipt = self.apply(plan)
        self.assertEqual(receipt["operations"][0]["error"]["code"], "rule_changed")
        self.assertTrue(self.path.exists())
        self.native.assert_not_called()

    def test_generic_app_data_cannot_bypass_by_omitting_scan_findings(self):
        path = self.home / "Library/Caches/Unknown/license.key"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"preserve")
        scan = scan_roots([str(path.parent)])
        item = next(value for value in scan["items"] if value["path"] == str(path))
        with self.assertRaises(CleanMacError):
            make_plan(scan, [item["id"]])
        self.assertEqual(path.read_bytes(), b"preserve")
        self.native.assert_not_called()

    def test_new_repository_protection_blocks_existing_cache_plan(self):
        self.create()
        plan = self.plan()
        (self.cache / ".git").mkdir()
        receipt = self.apply(plan)
        self.assertEqual(receipt["operations"][0]["status"], "skipped")
        self.assertTrue(self.path.exists())
        self.native.assert_not_called()

    def test_changed_blob_between_rule_verification_and_capture_is_not_trashed(self):
        self.create()
        plan = self.plan()
        before = self.path.stat()
        original_move = executor._rename_exclusive
        raced = False

        def race(source_fd, source_name, destination_fd, destination_name):
            nonlocal raced
            if not raced:
                raced = True
                self.path.write_bytes(b"x" * before.st_size)
                os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
            return original_move(source_fd, source_name, destination_fd, destination_name)

        with patch.object(executor, "_rename_exclusive", side_effect=race):
            receipt = self.apply(plan)
        self.native.assert_not_called()
        self.assertEqual(receipt["operations"][0]["status"], "failed")
        self.assertEqual(self.path.read_bytes(), b"x" * before.st_size)


if __name__ == "__main__":
    unittest.main()

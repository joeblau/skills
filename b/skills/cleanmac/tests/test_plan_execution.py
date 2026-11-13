"""Independent plan/executor checks using a fixture-only Trash transport.

No test invokes Foundation Trash, reads user files, or changes system settings.
The real descriptor-relative capture/restore and private state code run; only
the final native transport is replaced with an ordinary move inside a fixture.
"""

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cleanmac_core.common import CleanMacError, digest
from cleanmac_core import executor
from cleanmac_core.duplicates import find_duplicates
from cleanmac_core.inventory import scan_roots
from cleanmac_core.plans import make_plan
from cleanmac_core.state import StateStore


class PlanExecutionReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cleanmac-independent-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.scope = self.base / "scope"
        self.scope.mkdir()
        self.fake_trash = self.base / "fixture-trash"
        self.fake_trash.mkdir(mode=0o700)
        self.store = StateStore(self.base / "state")
        self.a = self.scope / "selected.txt"
        self.b = self.scope / "unselected.txt"
        self.a.write_bytes(b"selected data")
        self.b.write_bytes(b"other data")
        self.transport_count = 0
        native = patch("cleanmac_core.executor._native_executable", return_value=Path(sys.executable))
        native.start()
        self.addCleanup(native.stop)
        # This suite's transport targets only fixture-trash. Production recovery
        # preflight is exercised separately with the real native helper.
        recovery = patch("cleanmac_core.executor._preflight_recovery", return_value=str(self.fake_trash))
        recovery.start()
        self.addCleanup(recovery.stop)
        transport = patch("cleanmac_core.executor._native_trash", side_effect=self.transport)
        self.native = transport.start()
        self.addCleanup(transport.stop)

    def transport(self, native, stage_path, receipt_path):
        self.transport_count += 1
        target = self.fake_trash / f"{self.transport_count}-{stage_path.name}"
        os.rename(stage_path, target)
        result = {"status": "trashed", "original_path": str(stage_path), "trash_path": str(target)}
        Path(receipt_path).write_text(json.dumps(result))
        return result

    def item(self, scan, path):
        return next(item for item in scan["items"] if item["path"] == str(path))

    def plan(self, *, duplicate=False):
        if duplicate:
            self.b.write_bytes(self.a.read_bytes())
        scan = scan_roots([str(self.scope)])
        if duplicate:
            scan["duplicate_groups"] = find_duplicates(scan["items"])["groups"]
        plan = make_plan(scan, [self.item(scan, self.a)["id"]])
        self.store.put("scans", scan)
        self.store.put("plans", plan)
        return scan, plan

    def apply(self, plan, **kwargs):
        return executor.apply_plan(plan, approval_digest=plan["digest"], store=self.store, **kwargs)

    def test_approval_digest_must_match_and_plan_must_be_unchanged(self):
        scan, plan = self.plan()
        with self.assertRaises(CleanMacError):
            executor.apply_plan(plan, approval_digest="0" * 64, store=self.store)
        altered = copy.deepcopy(plan)
        altered["items"][0] = self.item(scan, self.b)
        with self.assertRaises(CleanMacError):
            executor.apply_plan(altered, approval_digest=plan["digest"], store=self.store)
        self.assertTrue(self.a.exists())
        self.assertTrue(self.b.exists())
        self.assertEqual(self.transport_count, 0)

    def test_only_selected_file_moves_and_reapply_is_idempotent(self):
        _, plan = self.plan()
        receipt = self.apply(plan)
        self.assertEqual(receipt["operations"][0]["status"], "trashed")
        self.assertFalse(self.a.exists())
        self.assertEqual(self.b.read_bytes(), b"other data")
        self.assertIsNone(receipt["summary"]["reclaimed_bytes"])
        self.assertEqual(receipt["summary"]["trash_retained_bytes_estimate"], plan["items"][0]["allocated_size"])
        repeated = self.apply(plan)
        self.assertEqual(receipt["id"], repeated["id"])
        self.assertEqual(self.transport_count, 1)

    def test_saved_pending_operation_cannot_replace_approved_target(self):
        scan, plan = self.plan()
        receipt = self.apply(plan, cancel=lambda: True)
        operation = receipt["operations"][0]
        other = copy.deepcopy(self.item(scan, self.b))
        operation.update(item=other, item_id=other["id"], original_path=other["path"])
        self.store.put("receipts", receipt)
        with self.assertRaises(CleanMacError):
            self.apply(plan)
        self.assertTrue(self.a.exists())
        self.assertTrue(self.b.exists())
        self.assertEqual(self.transport_count, 0)

    def test_saved_operation_list_cannot_expand_approved_scope(self):
        scan, plan = self.plan()
        receipt = self.apply(plan, cancel=lambda: True)
        other = copy.deepcopy(self.item(scan, self.b))
        receipt["operations"].append({"id": "op-extra", "status": "not_started", "item": other, "item_id": other["id"], "original_path": other["path"]})
        self.store.put("receipts", receipt)
        with self.assertRaises(CleanMacError):
            self.apply(plan)
        self.assertTrue(self.a.exists())
        self.assertTrue(self.b.exists())
        self.assertEqual(self.transport_count, 0)

    def test_restore_receipt_target_cannot_be_rewritten(self):
        _, plan = self.plan()
        receipt = self.apply(plan)
        altered = copy.deepcopy(receipt)
        destination = self.scope / "not-reviewed.txt"
        altered["operations"][0]["original_path"] = str(destination)
        altered["operations"][0]["item"]["path"] = str(destination)
        self.store.put("receipts", altered)
        with self.assertRaises(CleanMacError):
            executor.restore_receipt(altered, store=self.store)
        self.assertFalse(destination.exists())
        self.assertFalse(self.a.exists())
        self.assertTrue(Path(receipt["operations"][0]["trash_path"]).exists())

    def test_changed_duplicate_keeper_blocks_apply(self):
        _, plan = self.plan(duplicate=True)
        self.b.write_bytes(b"a changed keeper")
        receipt = self.apply(plan)
        self.assertNotEqual(receipt["operations"][0]["status"], "trashed")
        self.assertEqual(self.a.read_bytes(), b"selected data")
        self.assertEqual(self.transport_count, 0)

    def test_restore_cannot_substitute_unselected_file_as_trash_source(self):
        _, plan = self.plan()
        receipt = self.apply(plan)
        altered = copy.deepcopy(receipt)
        operation = altered["operations"][0]
        operation["trash_path"] = str(self.b)
        operation["trash_identity"] = executor._identity(self.b.stat())
        self.store.put("receipts", altered)
        try:
            result = executor.restore_receipt(altered, store=self.store)
        except CleanMacError:
            result = None
        self.assertTrue(self.b.exists(), "Recovery moved an unselected file after receipt metadata was changed")
        self.assertEqual(self.b.read_bytes(), b"other data")
        self.assertFalse(self.a.exists())
        if result:
            self.assertNotEqual(result["operations"][0]["status"], "restored")

    def test_keeper_change_during_capture_rolls_back_selected_file(self):
        _, plan = self.plan(duplicate=True)
        original = executor._rename_exclusive
        changed = False

        def racing_move(source_fd, source_name, destination_fd, destination_name):
            nonlocal changed
            original(source_fd, source_name, destination_fd, destination_name)
            if not changed:
                changed = True
                self.b.write_bytes(b"keeper changed during capture")

        with patch("cleanmac_core.executor._rename_exclusive", side_effect=racing_move):
            receipt = self.apply(plan)
        self.assertNotEqual(receipt["operations"][0]["status"], "trashed")
        self.assertEqual(self.a.read_bytes(), b"selected data")
        self.assertEqual(self.transport_count, 0)

    def test_same_size_ordinary_file_rewrite_at_capture_is_not_trashed(self):
        _, plan = self.plan()
        before = self.a.stat()
        original = executor._rename_exclusive
        changed = False

        def racing_move(source_fd, source_name, destination_fd, destination_name):
            nonlocal changed
            if not changed:
                changed = True
                self.a.write_bytes(b"x" * before.st_size)
                os.utime(self.a, ns=(before.st_atime_ns, before.st_mtime_ns))
            return original(source_fd, source_name, destination_fd, destination_name)

        with patch("cleanmac_core.executor._rename_exclusive", side_effect=racing_move):
            receipt = self.apply(plan)
        self.assertEqual(self.transport_count, 0)
        self.assertEqual(receipt["operations"][0]["status"], "failed")
        self.assertEqual(self.a.read_bytes(), b"x" * before.st_size)

    def test_interrupted_native_result_is_not_replayed(self):
        _, plan = self.plan()

        def interrupted(*args):
            self.transport(*args)
            raise KeyboardInterrupt()

        self.native.side_effect = interrupted
        receipt = self.apply(plan)
        self.assertEqual(receipt["status"], "needs-recovery")
        self.assertEqual(self.transport_count, 1)
        self.native.side_effect = self.transport
        repeated = self.apply(plan)
        self.assertEqual(repeated["status"], "needs-recovery")
        self.assertEqual(self.transport_count, 1)
        self.assertEqual(len(list(self.fake_trash.iterdir())), 1)

    def test_cancellation_before_capture_can_resume(self):
        _, plan = self.plan()
        checks = 0

        def cancel():
            nonlocal checks
            checks += 1
            return checks >= 2

        receipt = self.apply(plan, cancel=cancel)
        self.assertTrue(self.a.exists())
        self.assertEqual(self.transport_count, 0)
        repeated = self.apply(plan)
        self.assertEqual(repeated["operations"][0]["status"], "trashed")
        self.assertEqual(self.transport_count, 1)

    def test_restore_never_overwrites_new_original(self):
        _, plan = self.plan()
        receipt = self.apply(plan)
        self.a.write_bytes(b"newer original")
        restored = executor.restore_receipt(receipt, store=self.store)
        self.assertEqual(restored["operations"][0]["status"], "conflict")
        self.assertEqual(self.a.read_bytes(), b"newer original")
        self.assertEqual(Path(receipt["operations"][0]["trash_path"]).read_bytes(), b"selected data")

    def test_conflict_created_during_restore_is_not_overwritten(self):
        _, plan = self.plan()
        receipt = self.apply(plan)
        original = executor._rename_exclusive
        scope_inode = self.scope.stat().st_ino
        conflict_created = False

        def racing_move(source_fd, source_name, destination_fd, destination_name):
            nonlocal conflict_created
            if os.fstat(destination_fd).st_ino == scope_inode and not conflict_created:
                conflict_created = True
                self.a.write_bytes(b"concurrent new file")
            return original(source_fd, source_name, destination_fd, destination_name)

        with patch("cleanmac_core.executor._rename_exclusive", side_effect=racing_move):
            restored = executor.restore_receipt(receipt, store=self.store)
        self.assertEqual(restored["operations"][0]["status"], "conflict")
        self.assertEqual(self.a.read_bytes(), b"concurrent new file")
        self.assertEqual(Path(receipt["operations"][0]["trash_path"]).read_bytes(), b"selected data")

    def test_successful_restore_and_repeated_restore_preserve_data(self):
        _, plan = self.plan()
        receipt = self.apply(plan)
        restored = executor.restore_receipt(receipt, store=self.store)
        self.assertEqual(restored["operations"][0]["status"], "restored")
        self.assertEqual(self.a.read_bytes(), b"selected data")
        self.assertEqual(restored["summary"]["trash_retained_bytes_estimate"], 0)
        again = executor.restore_receipt(restored, store=self.store)
        self.assertEqual(again["operations"][0]["status"], "restored")
        self.assertEqual(self.transport_count, 1)


if __name__ == "__main__":
    unittest.main()

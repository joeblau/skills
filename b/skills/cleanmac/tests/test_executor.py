"""File mutation tests use only disposable fixtures and a fake native helper."""
import copy
import json
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
from cleanmac_core.executor import apply_plan, restore_receipt
from cleanmac_core import executor
from cleanmac_core.inventory import scan_roots
from cleanmac_core.plans import make_plan
from cleanmac_core.state import StateStore


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cleanmac-executor-")
        self.root = Path(self.temp.name).resolve()
        self.files = self.root / "files"
        self.files.mkdir()
        self.fake_trash = self.root / "fake-trash"
        self.fake_trash.mkdir(mode=0o700)
        self.store = StateStore(str(self.root / "state"))
        self.helper = self.root / "fake-native"
        self.helper.write_text("#!" + sys.executable + "\n" + '''import json,os,pathlib,sys
if sys.argv[1:] == ['info']:
 print(json.dumps({'protocol_version':1,'version':'1.0.0','capabilities':['trash','trash_preflight'],'platform':'macos','privileged':False}))
 sys.exit(0)
if sys.argv[1] == 'preflight':
 root=pathlib.Path(__file__).parent
 source=pathlib.Path(sys.argv[sys.argv.index('--path')+1])
 trash=root/'fake-trash'
 print(json.dumps({'status':'available','trash_directory':str(trash),'source_device':source.stat().st_dev,'trash_device':trash.stat().st_dev,'created_directory':False}))
 sys.exit(0)
source=pathlib.Path(sys.argv[sys.argv.index('--path')+1])
receipt=pathlib.Path(sys.argv[sys.argv.index('--receipt')+1])
root=pathlib.Path(__file__).parent
mode=(root/'helper-mode').read_text() if (root/'helper-mode').exists() else 'success'
calls=root/'calls'
calls.write_text(str(int(calls.read_text())+1) if calls.exists() else '1')
receipt.write_text(json.dumps({'status':'pending','original_path':str(source)}))
receipt.chmod(0o600)
if mode=='fail-before': sys.exit(1)
destination=root/'fake-trash'/source.name
if destination.exists(): destination=destination.with_name(source.parent.name+'-'+source.name)
os.rename(source,destination)
if mode=='ambiguous': sys.exit(1)
value={'status':'trashed','original_path':str(source),'trash_path':str(destination)}
receipt.write_text(json.dumps(value))
print(json.dumps(value))
''')
        self.helper.chmod(0o700)
        self.addCleanup(self.temp.cleanup)

    def create(self, name="document.txt", content=b"test content"):
        path = self.files / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def plan(self, *paths, duplicates=False):
        scan = scan_roots([str(self.files)])
        if duplicates:
            scan["duplicate_groups"] = find_duplicates(scan["items"])["groups"]
        selected = [item["id"] for item in scan["items"] if item["path"] in {str(p) for p in paths}]
        return make_plan(scan, selected)

    def apply(self, plan, **kwargs):
        return apply_plan(plan, approval_digest=plan["digest"], store=self.store, native_path=self.helper, **kwargs)

    def calls(self):
        path = self.root / "calls"
        return int(path.read_text()) if path.exists() else 0

    def test_exact_approval_required_before_mutation(self):
        path = self.create()
        plan = self.plan(path)
        with self.assertRaises(CleanMacError) as raised:
            apply_plan(plan, approval_digest="incorrect", store=self.store, native_path=self.helper)
        self.assertEqual(raised.exception.code, "approval_required")
        self.assertTrue(path.exists())
        self.assertEqual(self.calls(), 0)

    def test_missing_native_keeps_original(self):
        path = self.create()
        plan = self.plan(path)
        with self.assertRaises(CleanMacError) as raised:
            apply_plan(plan, approval_digest=plan["digest"], store=self.store, native_path=self.root / "missing")
        self.assertEqual(raised.exception.code, "native_helper_unavailable")
        self.assertEqual(path.read_bytes(), b"test content")

    def test_incompatible_native_is_rejected_before_capture(self):
        path = self.create()
        plan = self.plan(path)
        self.helper.write_text("#!" + sys.executable + "\nprint('{\"protocol_version\":99}')\n")
        with self.assertRaises(CleanMacError) as raised:
            self.apply(plan)
        self.assertEqual(raised.exception.code, "native_helper_unavailable")
        self.assertTrue(path.exists())
        self.assertEqual(self.store.list("receipts"), [])

    def test_denied_trash_preflight_never_moves_selected_files(self):
        first = self.create("first.txt")
        second = self.create("second.txt")
        plan = self.plan(first, second)
        original = executor._open_directory

        def deny_trash(path, *args, **kwargs):
            if str(path) == str(self.fake_trash):
                raise PermissionError(1, "Operation not permitted")
            return original(path, *args, **kwargs)

        with patch.object(executor, "_open_directory", side_effect=deny_trash), self.assertRaises(CleanMacError) as raised:
            self.apply(plan)
        self.assertEqual(raised.exception.code, "trash_access_required")
        self.assertTrue(raised.exception.details["permission_needed"])
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(self.calls(), 0)
        self.assertEqual(list(self.fake_trash.iterdir()), [])

    def test_permission_revoked_after_native_move_records_observed_effect(self):
        path = self.create()
        plan = self.plan(path)
        with patch.object(executor, "_check_trash_result", side_effect=PermissionError(1, "Operation not permitted")):
            result = self.apply(plan)
        self.assertEqual(result["status"], "needs-recovery")
        self.assertEqual(result["operations"][0]["status"], "verify-denied")
        self.assertEqual(result["summary"]["trashed_count"], 1)
        self.assertEqual(result["summary"]["verified_trashed_count"], 0)
        self.assertEqual(result["summary"]["unverified_move_count"], 1)
        self.assertEqual(Path(result["operations"][0]["unverified_trash_path"]).read_bytes(), b"test content")
        self.apply(plan)
        self.assertEqual(self.calls(), 1)

    def test_changed_pending_receipt_cannot_expand_approved_selection(self):
        selected = self.create("selected.txt")
        other = self.create("other.txt", b"must stay")
        plan = self.plan(selected)
        result = self.apply(plan, cancel=lambda: True)
        scan = scan_roots([str(self.files)])
        substituted = next(x for x in scan["items"] if x["path"] == str(other))
        changed = copy.deepcopy(result)
        changed["operations"][0].update(item=substituted, item_id=substituted["id"], original_path=str(other))
        self.store.put("receipts", changed)
        with self.assertRaises(CleanMacError):
            self.apply(plan)
        self.assertEqual(other.read_bytes(), b"must stay")
        self.assertTrue(selected.exists())
        self.assertEqual(self.calls(), 0)

    def test_changed_trash_handle_cannot_restore_unselected_inode(self):
        selected = self.create("selected.txt")
        other = self.create("other.txt", b"must stay")
        result = self.apply(self.plan(selected))
        changed = copy.deepcopy(result)
        changed["operations"][0]["trash_path"] = str(other)
        changed["operations"][0]["trash_identity"] = executor._identity(other.stat())
        self.store.put("receipts", changed)
        with self.assertRaises(CleanMacError):
            restore_receipt(result, store=self.store)
        self.assertEqual(other.read_bytes(), b"must stay")
        self.assertFalse(selected.exists())

    def test_trash_and_restore_preserve_exact_content(self):
        path = self.create("odd\n'$(name).txt")
        plan = self.plan(path)
        result = self.apply(plan)
        self.assertEqual(result["status"], "complete")
        self.assertFalse(path.exists())
        operation = result["operations"][0]
        self.assertEqual(operation["status"], "trashed")
        self.assertEqual(Path(operation["trash_path"]).read_bytes(), b"test content")
        self.assertIsNone(result["summary"]["reclaimed_bytes"])
        self.assertGreaterEqual(result["summary"]["trash_retained_bytes_estimate"], 0)
        restored = restore_receipt(result, store=self.store)
        self.assertEqual(restored["status"], "complete")
        self.assertEqual(restored["operations"][0]["status"], "restored")
        self.assertEqual(path.read_bytes(), b"test content")
        self.assertEqual(self.calls(), 1)

    def test_reapply_does_not_repeat_native_operation(self):
        path = self.create()
        plan = self.plan(path)
        result = self.apply(plan)
        repeated = self.apply(plan)
        self.assertEqual(result, repeated)
        self.assertEqual(self.calls(), 1)

    def test_changed_file_is_skipped(self):
        path = self.create()
        plan = self.plan(path)
        path.write_bytes(b"new user content")
        result = self.apply(plan)
        self.assertEqual(result["operations"][0]["status"], "skipped")
        self.assertEqual(path.read_bytes(), b"new user content")
        self.assertEqual(self.calls(), 0)

    def test_symlink_substituted_for_parent_is_skipped(self):
        path = self.create("nested/document.txt")
        plan = self.plan(path)
        moved = self.files / "moved"
        path.parent.rename(moved)
        path.parent.symlink_to(moved, target_is_directory=True)
        result = self.apply(plan)
        self.assertEqual(result["operations"][0]["status"], "skipped")
        self.assertEqual((moved / path.name).read_bytes(), b"test content")
        self.assertEqual(self.calls(), 0)

    def test_duplicate_keeper_changed_before_apply(self):
        first = self.create("one.txt", b"same")
        second = self.create("two.txt", b"same")
        plan = self.plan(second, duplicates=True)
        first.write_bytes(b"keeper changed")
        result = self.apply(plan)
        self.assertEqual(result["operations"][0]["status"], "skipped")
        self.assertEqual(second.read_bytes(), b"same")
        self.assertEqual(self.calls(), 0)

    def test_raced_capture_is_rolled_back_without_trashing(self):
        path = self.create()
        plan = self.plan(path)
        real_move = executor._rename_exclusive
        changed = False

        def replace_at_capture(source_fd, source_name, destination_fd, destination_name):
            nonlocal changed
            if not changed:
                changed = True
                path.write_bytes(b"raced new data")
            return real_move(source_fd, source_name, destination_fd, destination_name)

        with patch.object(executor, "_rename_exclusive", side_effect=replace_at_capture):
            result = self.apply(plan)
        self.assertEqual(result["operations"][0]["status"], "failed")
        self.assertEqual(result["operations"][0]["rollback"], "returned_to_original_path")
        self.assertEqual(path.read_bytes(), b"raced new data")
        self.assertEqual(self.calls(), 0)

    def test_native_failure_before_move_rolls_back(self):
        path = self.create()
        plan = self.plan(path)
        (self.root / "helper-mode").write_text("fail-before")
        result = self.apply(plan)
        self.assertEqual(result["operations"][0]["status"], "failed")
        self.assertEqual(path.read_bytes(), b"test content")
        self.apply(plan)
        self.assertEqual(self.calls(), 1)

    def test_native_permission_diagnostic_is_preserved(self):
        path = self.create()
        plan = self.plan(path)

        def deny(native, stage_path, receipt_path):
            raise CleanMacError("Native permission was denied.", code="permission_denied", details={"native_error": {"code": "permission_denied", "posix_errno": 1}})

        with patch.object(executor, "_native_trash", side_effect=deny):
            result = self.apply(plan)
        operation = result["operations"][0]
        self.assertEqual(operation["error"]["code"], "permission_denied")
        self.assertEqual(operation["error"]["details"]["native_error"]["posix_errno"], 1)
        self.assertEqual(path.read_bytes(), b"test content")

    def test_failed_rollback_preserves_quarantine_and_blocks_replay(self):
        path = self.create()
        plan = self.plan(path)

        def conflict(native, stage_path, receipt_path):
            path.write_bytes(b"new original")
            raise CleanMacError("Fake helper failed.", code="native_outcome_unknown")

        with patch.object(executor, "_native_trash", side_effect=conflict):
            result = self.apply(plan)
        operation = result["operations"][0]
        self.assertEqual(operation["status"], "ambiguous")
        self.assertEqual(Path(operation["recovery_path"]).read_bytes(), b"test content")
        self.assertEqual(path.read_bytes(), b"new original")
        repeated = self.apply(plan)
        self.assertEqual(repeated["status"], "needs-recovery")
        self.assertEqual(path.read_bytes(), b"new original")

    def test_ambiguous_native_result_never_replays(self):
        path = self.create()
        plan = self.plan(path)
        (self.root / "helper-mode").write_text("ambiguous")
        result = self.apply(plan)
        self.assertEqual(result["status"], "needs-recovery")
        self.assertEqual(result["operations"][0]["status"], "ambiguous")
        self.assertFalse(path.exists())
        self.assertEqual((self.fake_trash / path.name).read_bytes(), b"test content")
        self.apply(plan)
        self.assertEqual(self.calls(), 1)

    def test_cancel_before_capture_leaves_unstarted_for_exact_resume(self):
        path = self.create()
        plan = self.plan(path)
        result = self.apply(plan, cancel=lambda: True)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["operations"][0]["status"], "not_started")
        self.assertTrue(path.exists())
        resumed = self.apply(plan)
        self.assertEqual(resumed["operations"][0]["status"], "trashed")
        self.assertEqual(self.calls(), 1)

    def test_restore_never_overwrites_recreated_original(self):
        path = self.create()
        result = self.apply(self.plan(path))
        path.write_bytes(b"new original")
        restored = restore_receipt(result, store=self.store)
        self.assertEqual(restored["operations"][0]["status"], "conflict")
        self.assertEqual(path.read_bytes(), b"new original")
        self.assertEqual(Path(result["operations"][0]["trash_path"]).read_bytes(), b"test content")

    def test_restore_rejects_changed_trash_item(self):
        path = self.create()
        result = self.apply(self.plan(path))
        trash = Path(result["operations"][0]["trash_path"])
        trash.write_bytes(b"different data in trash")
        restored = restore_receipt(result, store=self.store)
        self.assertEqual(restored["operations"][0]["status"], "conflict")
        self.assertFalse(path.exists())
        self.assertEqual(trash.read_bytes(), b"different data in trash")

    def test_rule_reclassification_blocks_apply(self):
        path = self.create()
        plan = self.plan(path)
        finding = {"item_id": plan["items"][0]["id"], "eligible": False, "rule_id": "protected", "rule_version": "1"}
        with patch("cleanmac_core.rules.classify_items", return_value=[finding]):
            result = self.apply(plan)
        self.assertEqual(result["operations"][0]["error"]["code"], "rule_changed")
        self.assertTrue(path.exists())
        self.assertEqual(self.calls(), 0)

    def test_restore_without_saved_receipt_rejected(self):
        path = self.create()
        result = self.apply(self.plan(path))
        other = StateStore(str(self.root / "other-state"))
        with self.assertRaises(CleanMacError):
            restore_receipt(result, store=other)
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()

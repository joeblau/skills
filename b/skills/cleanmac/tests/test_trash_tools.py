"""Trash erasure behavior in private disposable simulated home/volume fixtures."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cleanmac_core import trash_tools
from cleanmac_core.common import CleanMacError
from cleanmac_core.state import StateStore


class TrashToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir(mode=0o700)
        self.trash = self.home / ".Trash"
        self.trash.mkdir(mode=0o700)
        self.file = self.trash / "selected.txt"
        self.file.write_bytes(b"chosen disposable fixture")
        self.store = StateStore(self.base / "state")
        self.options = {"roots": [str(self.trash)], "configuration": {}, "selected": [], "record_id": None, "approve": None}
        self.volume = {"uuid": "fixture-volume", "device_identifier": "disk-fixture", "mount_point": str(self.base)}
        self.patches = [mock.patch.object(trash_tools, "_home", return_value=str(self.home)),
                        mock.patch.object(trash_tools, "_volume", return_value=self.volume),
                        mock.patch.object(trash_tools.platform, "system", return_value="Darwin")]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def inspect(self, **kwargs):
        return trash_tools.dispatch("trash", "inspect", self.options, store=self.store, **kwargs)

    def plan(self, path=None):
        scan = self.inspect()
        selected = next(x for x in scan["items"] if x["path"] == str(path or self.file))
        return trash_tools.dispatch("trash", "plan", dict(self.options, record_id=scan["id"], selected=[selected["id"]]), store=self.store)

    def apply(self, plan, **kwargs):
        return trash_tools.dispatch("trash", "apply", dict(self.options, record_id=plan["id"], approve=plan["digest"]), store=self.store, **kwargs)

    def assert_code(self, code, fn, *args, **kwargs):
        with self.assertRaises(CleanMacError) as result:
            fn(*args, **kwargs)
        self.assertEqual(code, result.exception.code)

    def test_read_only_inventory_and_irreversible_summary(self):
        before = self.file.read_bytes()
        report = self.inspect()
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["summary"]["eligible_files"], 1)
        self.assertEqual(self.file.read_bytes(), before)
        plan = self.plan()
        self.assertTrue(plan["summary"]["irreversible"])
        self.assertEqual(plan["summary"]["recovery"], "unavailable")
        self.assertEqual(plan["summary"]["path"], str(self.file))

    def test_exact_erase_new_arrival_survives_and_replay_is_inert(self):
        plan = self.plan()
        fresh = self.trash / "new.txt"
        fresh.write_bytes(b"new arrival")
        receipt = self.apply(plan)
        self.assertEqual(receipt["status"], "completed")
        self.assertFalse(self.file.exists())
        self.assertEqual(fresh.read_bytes(), b"new arrival")
        self.assertEqual(receipt["result"]["recovery"], "unavailable")
        self.file.write_bytes(b"new at old name")
        replay = self.apply(plan)
        self.assertEqual(replay["id"], receipt["id"])
        self.assertEqual(self.file.read_bytes(), b"new at old name")
        self.assertFalse(list(self.trash.glob(".cleanmac-erase-*")))

    def test_generic_files_and_other_accounts_are_rejected(self):
        for root in (str(self.home), str(self.base / "someone" / ".Trash"), "/Volumes/Example/.Trashes/999999"):
            with self.subTest(root=root):
                self.assert_code("invalid_trash_scope", trash_tools.dispatch, "trash", "inspect", dict(self.options, roots=[root]), store=self.store)

    def test_missing_root_is_unscanned_not_empty(self):
        self.file.unlink()
        self.trash.rmdir()
        report = self.inspect()
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["coverage"][0]["status"], "unscanned")

    def test_denied_root_is_permission_needed(self):
        with mock.patch.object(trash_tools, "_root", side_effect=PermissionError(errno.EPERM, "denied")):
            report = self.inspect()
        self.assertEqual(report["status"], "permission-needed")
        self.assertEqual(report["errors"][0]["coverage"], "unscanned")
        self.assertEqual(report["errors"][0]["suggested_settings_pane"], "full-disk-access")

    def test_selected_content_change_blocks_before_capture(self):
        plan = self.plan()
        self.file.write_bytes(b"different")
        self.assert_code("trash_file_changed", self.apply, plan)
        self.assertEqual(self.file.read_bytes(), b"different")
        self.assertFalse(list(self.trash.glob(".cleanmac-erase-*")))

    def test_same_size_content_change_is_rejected(self):
        plan = self.plan()
        old = self.file.stat()
        self.file.write_bytes(b"X" * old.st_size)
        os.utime(self.file, ns=(old.st_atime_ns, old.st_mtime_ns))
        with self.assertRaises(CleanMacError):
            self.apply(plan)
        self.assertTrue(self.file.exists())

    def test_replaced_volume_identity_blocks(self):
        plan = self.plan()
        with mock.patch.object(trash_tools, "_volume", return_value=dict(self.volume, uuid="new-volume")):
            self.assert_code("trash_volume_changed", self.apply, plan)
        self.assertTrue(self.file.exists())

    def test_replaced_trash_ancestor_blocks(self):
        plan = self.plan()
        displaced = self.home / "PreviousTrash"
        self.trash.rename(displaced)
        self.trash.mkdir(mode=0o700)
        self.file.write_bytes(b"replacement")
        self.assert_code("trash_ancestor_changed", self.apply, plan)
        self.assertEqual(self.file.read_bytes(), b"replacement")
        self.assertTrue((displaced / self.file.name).exists())

    def test_symlink_hardlink_package_and_recovery_stage_protected(self):
        outside = self.home / "outside"
        outside.write_bytes(b"must survive")
        link = self.trash / "link"
        link.symlink_to(outside)
        hard = self.trash / "hard"
        os.link(outside, hard)
        package = self.trash / "App.app"
        package.mkdir()
        (package / "inside").write_bytes(b"package")
        stage = self.trash / ".cleanmac-erase-existing"
        stage.mkdir()
        (stage / "captured").write_bytes(b"recovery")
        report = self.inspect()
        by_name = {Path(v["path"]).name: v for v in report["items"]}
        self.assertFalse(by_name["link"]["eligible"])
        self.assertFalse(by_name["hard"]["eligible"])
        self.assertFalse(by_name["App.app"]["eligible"])
        self.assertFalse(by_name[stage.name]["eligible"])
        self.assertNotIn("inside", by_name)
        self.assertNotIn("captured", by_name)
        self.assertEqual(outside.read_bytes(), b"must survive")

    def test_folder_plan_is_rejected(self):
        folder = self.trash / "folder"
        folder.mkdir()
        self.assert_code("ineligible_trash_item", self.plan, folder)

    def test_nested_regular_file_erased_without_removing_folder(self):
        folder = self.trash / "folder"
        folder.mkdir()
        nested = folder / "nested"
        nested.write_bytes(b"nested")
        result = self.apply(self.plan(nested))
        self.assertEqual(result["status"], "completed")
        self.assertTrue(folder.is_dir())
        self.assertFalse(nested.exists())
        self.assertTrue(self.file.exists())

    def test_multiple_selection_and_blanket_approval_rejected(self):
        report = self.inspect()
        selected = report["items"][0]["id"]
        self.assert_code("invalid_selection", trash_tools.dispatch, "trash", "plan", dict(self.options, record_id=report["id"], selected=[selected, selected]), store=self.store)
        plan = self.plan()
        self.assert_code("approval_required", trash_tools.dispatch, "trash", "apply", dict(self.options, record_id=plan["id"], approve="yes"), store=self.store)
        self.assertTrue(self.file.exists())

    def test_cancel_before_execution_never_moves(self):
        plan = self.plan()
        receipt = self.apply(plan, cancel=lambda: True)
        self.assertEqual(receipt["status"], "not_started")
        self.assertTrue(self.file.exists())
        self.assertFalse(list(self.trash.glob(".cleanmac-erase-*")))

    def test_swap_just_before_capture_restores_replacement_no_erase(self):
        plan = self.plan()
        original = trash_tools._rename_exclusive
        displaced = self.trash / "original-moved"
        changed = False
        def rename(source_fd, source, dest_fd, dest):
            nonlocal changed
            if not changed and source == self.file.name:
                changed = True
                self.file.rename(displaced)
                self.file.write_bytes(b"replacement that must survive")
            return original(source_fd, source, dest_fd, dest)
        with mock.patch.object(trash_tools, "_rename_exclusive", side_effect=rename):
            receipt = self.apply(plan)
        self.assertEqual(receipt["status"], "recovery_needed")
        self.assertEqual(receipt["checkpoint"]["phase"], "restored-after-stop")
        self.assertEqual(self.file.read_bytes(), b"replacement that must survive")
        self.assertTrue(displaced.exists())

    def test_post_capture_content_change_and_conflict_is_retained(self):
        plan = self.plan()
        original = trash_tools._rename_exclusive
        def rename(source_fd, source, dest_fd, dest):
            result = original(source_fd, source, dest_fd, dest)
            if source == self.file.name:
                with os.fdopen(os.open(dest, os.O_WRONLY | os.O_TRUNC, dir_fd=dest_fd), "wb") as handle:
                    handle.write(b"changed captured file")
                self.file.write_bytes(b"new arrival at source")
            return result
        with mock.patch.object(trash_tools, "_rename_exclusive", side_effect=rename):
            receipt = self.apply(plan)
        self.assertEqual(receipt["status"], "recovery_needed")
        self.assertEqual(receipt["checkpoint"]["phase"], "retained-for-manual-recovery")
        self.assertEqual(self.file.read_bytes(), b"new arrival at source")
        self.assertEqual(Path(receipt["checkpoint"]["captured_path"]).read_bytes(), b"changed captured file")
        replay = self.apply(plan)
        self.assertEqual(replay["id"], receipt["id"])
        self.assertEqual(self.file.read_bytes(), b"new arrival at source")

    def test_failure_after_unlink_is_not_retried(self):
        plan = self.plan()
        original = trash_tools.checkpoint_action
        def checkpoint(value):
            if value["phase"] == "erased":
                raise OSError("fixture receipt storage failure")
            return original(value)
        with mock.patch.object(trash_tools, "checkpoint_action", side_effect=checkpoint):
            receipt = self.apply(plan)
        self.assertEqual(receipt["status"], "recovery_needed")
        self.assertFalse(self.file.exists())
        self.file.write_bytes(b"new after outcome unknown")
        replay = self.apply(plan)
        self.assertEqual(replay["id"], receipt["id"])
        self.assertEqual(self.file.read_bytes(), b"new after outcome unknown")

    def test_detached_volume_after_capture_restores_or_retains_not_erased(self):
        plan = self.plan()
        count = 0
        def volume(path):
            nonlocal count
            count += 1
            return self.volume if count < 3 else dict(self.volume, uuid="substituted")
        with mock.patch.object(trash_tools, "_volume", side_effect=volume):
            receipt = self.apply(plan)
        self.assertEqual(receipt["status"], "recovery_needed")
        self.assertTrue(self.file.exists())

    def test_scan_limit_and_cancellation_preserve_coverage(self):
        (self.trash / "second").write_bytes(b"another")
        options = dict(self.options, configuration={"max_items": 1})
        value = trash_tools.dispatch("trash", "inspect", options, store=self.store)
        self.assertEqual(value["status"], "partial")
        self.assertEqual(len(value["items"]), 1)
        cancelled = self.inspect(cancel=lambda: True)
        self.assertEqual(cancelled["status"], "cancelled")

    def test_external_current_user_path_requires_mounted_volume(self):
        target = f"/Volumes/DisposableFixture/.Trashes/{os.getuid()}"
        with mock.patch.object(trash_tools.os.path, "ismount", return_value=False):
            self.assert_code("volume_unavailable", trash_tools._scope, target)
        with mock.patch.object(trash_tools.os.path, "ismount", return_value=True):
            self.assertEqual(trash_tools._scope(target), (target, "external", "/Volumes/DisposableFixture"))

    def test_owner_only_trash_gate(self):
        self.trash.chmod(0o755)
        value = self.inspect()
        self.assertEqual(value["status"], "unavailable")
        self.assertEqual(value["errors"][0]["code"], "unsafe_trash_permissions")

    def test_nonmacos_does_not_query_volume(self):
        with mock.patch.object(trash_tools.platform, "system", return_value="Linux"), mock.patch.object(trash_tools, "_volume") as volume:
            result = self.inspect()
        self.assertEqual(result["status"], "unavailable")
        volume.assert_not_called()


if __name__ == "__main__":
    unittest.main()

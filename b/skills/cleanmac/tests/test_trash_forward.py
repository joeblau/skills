"""Independent erasure race tests; every file belongs to disposable fixtures."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cleanmac_core import trash_tools as trash
from cleanmac_core.common import CleanMacError
from cleanmac_core.state import StateStore


class TrashForwardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.home = self.base / "fixture-home"
        self.home.mkdir(mode=0o700)
        self.root = self.home / ".Trash"
        self.root.mkdir(mode=0o700)
        self.selected = self.root / "chosen.txt"
        self.selected.write_bytes(b"approved disposable content")
        self.store = StateStore(self.base / "state")
        self.options = {"roots": [str(self.root)], "selected": [], "configuration": {}}
        self.volume = {"uuid": "forward-fixture", "device_identifier": "fixture-disk", "mount_point": str(self.base)}
        patch.object(trash, "_home", return_value=str(self.home)).start()
        patch.object(trash, "_volume", return_value=self.volume).start()
        patch.object(trash.platform, "system", return_value="Darwin").start()
        self.addCleanup(patch.stopall)

    def plan(self):
        scan = trash.dispatch("trash", "inspect", self.options, store=self.store)
        selected = next(item for item in scan["items"] if item["path"] == str(self.selected))
        return trash.dispatch("trash", "plan", dict(self.options, record_id=scan["id"], selected=[selected["id"]]), store=self.store)

    def apply(self, plan):
        return trash.dispatch("trash", "apply", dict(self.options, record_id=plan["id"], approve=plan["digest"]), store=self.store)

    def inject_after_last_hash(self, mutation):
        real_checkpoint, real_hash = trash.checkpoint_action, trash._hash_fd
        state = {"checkpoint": None, "injected": False}
        def checkpoint(value):
            result = real_checkpoint(value)
            state["checkpoint"] = dict(value)
            return result
        def hashing(fd, cancel=None):
            result = real_hash(fd, cancel)
            value = state["checkpoint"]
            if value and value["phase"] == "before-unlink" and not state["injected"]:
                state["injected"] = True
                mutation(value)
            return result
        return patch.object(trash, "checkpoint_action", side_effect=checkpoint), patch.object(trash, "_hash_fd", side_effect=hashing)

    def test_captured_entry_replacement_during_final_hash_is_not_erased(self):
        plan = self.plan()
        original_saved = self.home / "original-preserved.txt"
        injected = {}
        def swap(value):
            captured = Path(value["captured_path"])
            captured.rename(original_saved)
            captured.write_bytes(b"new replacement must remain")
            injected["path"] = captured
        checkpoint, hashing = self.inject_after_last_hash(swap)
        with checkpoint, hashing:
            receipt = self.apply(plan)
        self.assertEqual(receipt["status"], "recovery_needed")
        self.assertEqual(original_saved.read_bytes(), b"approved disposable content")
        survivor = self.selected if self.selected.exists() else injected["path"]
        self.assertEqual(survivor.read_bytes(), b"new replacement must remain")

    def test_capture_directory_replacement_during_final_hash_prevents_unlink(self):
        plan = self.plan()
        displaced = self.home / "displaced-private-stage"
        injected = {}
        def swap(value):
            stage = Path(value["stage_path"])
            stage.rename(displaced)
            stage.mkdir(mode=0o700)
            fresh = stage / "new-arrival.txt"
            fresh.write_bytes(b"new stage entry survives")
            injected["fresh"] = fresh
        checkpoint, hashing = self.inject_after_last_hash(swap)
        with checkpoint, hashing:
            receipt = self.apply(plan)
        self.assertEqual(receipt["status"], "recovery_needed")
        survivor = self.selected if self.selected.exists() else displaced / "selected-file"
        self.assertEqual(survivor.read_bytes(), b"approved disposable content")
        self.assertEqual(injected["fresh"].read_bytes(), b"new stage entry survives")

    def test_generic_directory_cannot_become_trash_via_forged_scan(self):
        scan = trash.dispatch("trash", "inspect", self.options, store=self.store)
        outside = self.home / "ordinary"
        outside.mkdir()
        private = outside / "private.txt"
        private.write_bytes(b"not trash")
        scan["roots"][0]["path"] = str(outside)
        scan["items"][0]["root"] = str(outside)
        scan["items"][0]["path"] = str(private)
        self.store.put("scans", scan)
        with self.assertRaises(CleanMacError):
            trash.dispatch("trash", "plan", dict(self.options, record_id=scan["id"], selected=[scan["items"][0]["id"]]), store=self.store)
        self.assertEqual(private.read_bytes(), b"not trash")

    def test_source_parent_symlink_after_plan_preserves_outside_file(self):
        nested = self.root / "nested"
        nested.mkdir()
        self.selected = nested / "chosen.txt"
        self.selected.write_bytes(b"approved disposable content")
        plan = self.plan()
        original_parent = self.root / "saved-parent"
        nested.rename(original_parent)
        outside = self.home / "outside"
        outside.mkdir()
        target = outside / "chosen.txt"
        target.write_bytes(b"outside must survive")
        nested.symlink_to(outside, target_is_directory=True)
        with self.assertRaises((CleanMacError, OSError)):
            self.apply(plan)
        self.assertEqual(target.read_bytes(), b"outside must survive")
        self.assertEqual((original_parent / "chosen.txt").read_bytes(), b"approved disposable content")

    def test_failed_capture_journal_prevents_file_move(self):
        plan = self.plan()
        def fail(value):
            raise OSError("fixture journal failure")
        with patch.object(trash, "checkpoint_action", side_effect=fail):
            receipt = self.apply(plan)
        self.assertEqual(receipt["status"], "recovery_needed")
        self.assertEqual(self.selected.read_bytes(), b"approved disposable content")
        self.assertFalse(list(self.root.glob(".cleanmac-erase-*")))


if __name__ == "__main__":
    unittest.main()

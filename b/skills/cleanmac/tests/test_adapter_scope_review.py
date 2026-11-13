"""Independent regression checks spanning adapters, inventory, and CLI totals."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cleanmac_core import adapters, cli, rules
from cleanmac_core.state import StateStore


class AdapterScopeReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cleanmac-scope-review-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_xcode_checks_ancestry_before_category_metadata(self):
        selected = self.root / "selected"
        outside = self.root / "outside"
        selected.mkdir()
        (outside / "DerivedData").mkdir(parents=True)
        (outside / "DerivedData" / "private").write_bytes(b"out of scope")
        (selected / "Xcode").symlink_to(outside, target_is_directory=True)
        unsafe_path = str(selected / "Xcode" / "DerivedData")
        original_lstat = os.lstat
        unsafe_metadata_reads = []

        def tracked_lstat(path, *args, **kwargs):
            if os.fspath(path) == unsafe_path:
                unsafe_metadata_reads.append(path)
            return original_lstat(path, *args, **kwargs)

        with mock.patch.object(adapters.platform, "system", return_value="Darwin"), mock.patch.object(adapters.os, "lstat", side_effect=tracked_lstat):
            result = adapters.scan_xcode([str(selected)])
        self.assertEqual(result["items"], [])
        self.assertEqual(unsafe_metadata_reads, [], "Intermediate symlinks must be rejected before category metadata is accessed")

    def test_xcode_root_replacement_cannot_change_the_measured_scope(self):
        selected = self.root / "selected"
        replacement = self.root / "replacement"
        for root, data in ((selected, b"old"), (replacement, b"wrong-new-data")):
            (root / "Xcode" / "DerivedData").mkdir(parents=True)
            (root / "Xcode" / "DerivedData" / "file").write_bytes(data)
        category = str(selected / "Xcode" / "DerivedData")
        original_open_dir = adapters._open_dir
        replaced = False

        def racing_open_dir(path, *args, **kwargs):
            nonlocal replaced
            if path == category and not replaced:
                selected.rename(self.root / "old-selected")
                replacement.rename(selected)
                replaced = True
            return original_open_dir(path, *args, **kwargs)

        with mock.patch.object(adapters.platform, "system", return_value="Darwin"), mock.patch.object(adapters, "_open_dir", side_effect=racing_open_dir):
            result = adapters.scan_xcode([str(selected)])
        found = [item for item in result["items"] if item["category"] == "derived_data"]
        if found:
            self.assertEqual(found[0]["logical_size"], 3, "A replacement root must not be silently measured under the original selected identity")

    def test_cli_counts_hardlinked_files_once_and_excludes_state(self):
        selected = self.root / "selected"
        selected.mkdir()
        first = selected / "one"
        first.write_bytes(b"1234567")
        os.link(first, selected / "alias")
        second = selected / "two"
        second.write_bytes(b"abc")
        store = StateStore(selected / "private-state")
        store.put("scans", {"id": "scan-prior", "schema_version": 1, "status": "complete"})
        with mock.patch.object(cli.sys, "platform", "darwin"), mock.patch.object(rules, "detect_active_apps", return_value=[]):
            result = cli.dispatch(["scan", "--root", str(selected), "--areas", "files,storage"], store=store)
        self.assertEqual(result["summary"]["file_count"], 3)
        self.assertEqual(result["summary"]["unique_file_count"], 2)
        self.assertEqual(result["summary"]["logical_bytes"], 10)
        self.assertEqual(result["summary"]["allocated_bytes_estimate"], (first.stat().st_blocks + second.stat().st_blocks) * 512)
        self.assertIsNone(result["summary"]["reclaimable_bytes"])
        self.assertFalse(any(item["path"].startswith(str(store.root) + os.sep) for item in result["items"]))

    def test_cli_honors_cancel_before_read_only_adapter_work(self):
        with mock.patch.object(cli.sys, "platform", "darwin"), mock.patch.object(adapters.platform, "system", return_value="Darwin"), mock.patch.object(adapters, "_run_probe", side_effect=AssertionError("Cancelled overview must not invoke probes")):
            for command in ("apps", "xcode", "overview"):
                with self.subTest(command=command):
                    arguments = [command] if command == "overview" else [command, "--root", str(self.root)]
                    result = cli.dispatch(arguments, cancel=lambda: True)
                    self.assertEqual(result["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()

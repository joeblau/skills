"""Read-only adapter behavior using disposable bundles/developer directories."""

import errno
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cleanmac_core import adapters


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cleanmac-adapter-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.darwin = patch.object(adapters.platform, "system", return_value="Darwin")
        self.darwin.start()
        self.addCleanup(self.darwin.stop)

    def make_app(self, relative="Example.app", **values):
        path = self.root / relative
        (path / "Contents").mkdir(parents=True)
        data = {
            "CFBundleName": "Example", "CFBundleIdentifier": "test.example",
            "CFBundleShortVersionString": "2.3", "CFBundleVersion": "45",
            "LSMinimumSystemVersion": "13.0", "CFBundleExecutable": "Example",
            **values,
        }
        (path / "Contents" / "Info.plist").write_bytes(plistlib.dumps(data))
        return path

    def test_application_metadata_without_code_traversal_or_launch(self):
        app = self.make_app()
        self.make_app("Example.app/Contents/Nested.app", CFBundleIdentifier="test.nested")
        with patch.object(adapters.subprocess, "run", side_effect=AssertionError("No app execution")):
            report = adapters.scan_applications([str(self.root)])
        self.assertEqual(report["status"], "complete")
        self.assertEqual(len(report["items"]), 1)
        item = report["items"][0]
        self.assertEqual(item["path"], str(app))
        self.assertEqual(item["bundle_id"], "test.example")
        self.assertEqual(item["version"], "2.3")
        self.assertFalse(item["eligible"])
        self.assertTrue(item["report_only"])
        self.assertEqual(item["signature_status"], "not_checked")
        self.assertIsNone(item["allocated_size"])

    def test_nested_app_and_overlapping_roots_do_not_duplicate(self):
        app = self.make_app("Utilities/Example.app")
        report = adapters.scan_applications([str(self.root), str(self.root), str(app)])
        self.assertEqual(len(report["items"]), 1)
        self.assertEqual(len(report["roots"]), 2)

    def test_symlink_root_is_unavailable(self):
        self.make_app()
        link = self.root / "alias"
        link.symlink_to(self.root, target_is_directory=True)
        report = adapters.scan_applications([str(link)])
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["items"], [])

    def test_symlinked_metadata_is_not_read(self):
        app = self.make_app()
        metadata = app / "Contents" / "Info.plist"
        moved = self.root / "outside.plist"
        metadata.rename(moved)
        metadata.symlink_to(moved)
        report = adapters.scan_applications([str(app)])
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["items"][0]["metadata_status"], "unavailable")
        self.assertIsNone(report["items"][0]["bundle_id"])

    def test_invalid_bundle_metadata_is_partial_not_invented(self):
        app = self.make_app()
        (app / "Contents" / "Info.plist").write_bytes(b"not a plist")
        report = adapters.scan_applications([str(app)])
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["items"][0]["metadata_status"], "invalid")
        self.assertEqual(report["errors"][0]["code"], "invalid_plist")

    def test_missing_application_root_is_unscanned(self):
        report = adapters.scan_applications([str(self.root / "missing")])
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["errors"][0]["code"], "not_found")

    def test_malformed_xml_metadata_is_reported(self):
        app = self.make_app()
        (app / "Contents" / "Info.plist").write_bytes(b'<?xml version="1.0"?><plist><dict><key>bad</dict></plist>')
        report = adapters.scan_applications([str(app)])
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["items"][0]["metadata_status"], "invalid")

    def test_denied_root_is_not_an_empty_inventory(self):
        with patch.object(adapters, "_open_dir", side_effect=PermissionError(errno.EACCES, "denied")):
            report = adapters.scan_applications([str(self.root)])
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["coverage"][0]["status"], "unavailable")
        self.assertEqual(report["errors"][0]["code"], "permission_denied")

    def test_application_depth_and_entry_limits_are_partial(self):
        self.make_app("a/b/c/d/e/Deep.app")
        report = adapters.scan_applications([str(self.root)])
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["items"], [])
        self.assertTrue(any(e["code"] == "depth_limit" for e in report["errors"]))
        with patch.object(adapters, "_MAX_APPLICATION_ENTRIES", 0):
            limited = adapters.scan_applications([str(self.root)])
        self.assertEqual(limited["status"], "partial")
        self.assertTrue(any(e["code"] == "scan_limit" for e in limited["errors"]))

    def test_xcode_categories_are_metadata_only_and_preserve_managed_resources(self):
        derived = self.root / "Xcode" / "DerivedData"
        module = derived / "ModuleCache.noindex"
        module.mkdir(parents=True)
        (derived / "build.dat").write_bytes(b"12345")
        (module / "module.pcm").write_bytes(b"123")
        os.link(derived / "build.dat", derived / "build-alias.dat")
        outside = self.root / "private-file"
        outside.write_bytes(b"do not count")
        (derived / "link").symlink_to(outside)
        archives = self.root / "Xcode" / "Archives"
        archives.mkdir()
        (archives / "important.dSYM").write_bytes(b"preserve")
        before = outside.read_bytes()
        with patch.object(adapters.subprocess, "run", side_effect=AssertionError("No simctl or xcrun")):
            report = adapters.scan_xcode([str(self.root)])
        self.assertEqual(report["status"], "partial")
        items = {x["category"]: x for x in report["items"]}
        self.assertEqual(items["derived_data"]["logical_size"], 8)
        self.assertEqual(items["derived_data"]["files"], 2)
        self.assertEqual(items["module_cache"]["logical_size"], 3)
        self.assertEqual(items["module_cache"]["overlaps"], ["derived_data"])
        self.assertIsNone(report["summary"]["reclaimable_bytes"])
        self.assertTrue(all(x["report_only"] and x["protected"] and not x["eligible"] for x in report["items"]))
        self.assertEqual(outside.read_bytes(), before)
        self.assertEqual((archives / "important.dSYM").read_bytes(), b"preserve")

    def test_xcode_absent_category_does_not_imply_global_absence(self):
        report = adapters.scan_xcode([str(self.root)])
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["items"], [])
        self.assertTrue(all(c["status"] == "absent" for c in report["coverage"]))
        self.assertTrue(report["limitations"])

    def test_xcode_rejects_symlink_ancestor_and_keeps_denial_visible(self):
        outside = self.root / "outside"
        (outside / "DerivedData").mkdir(parents=True)
        (outside / "DerivedData" / "private").write_bytes(b"private")
        (self.root / "Xcode").symlink_to(outside, target_is_directory=True)
        report = adapters.scan_xcode([str(self.root)])
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["items"], [])
        self.assertTrue(report["errors"])

    def test_xcode_budget_does_not_report_unexamined_categories_complete(self):
        derived = self.root / "Xcode" / "DerivedData"
        derived.mkdir(parents=True)
        (derived / "a").write_bytes(b"a")
        with patch.object(adapters, "_MAX_ENTRIES", 0):
            report = adapters.scan_xcode([str(self.root)])
        self.assertEqual(report["status"], "partial")
        self.assertTrue(any(c["status"] == "partial" for c in report["coverage"]))

    def test_xcode_tree_rejects_an_unselected_volume(self):
        expected_device = self.root.stat().st_dev + 1
        with self.assertRaises(OSError) as error:
            adapters._tree_size(str(self.root), {"entries": 0, "started": adapters.time.monotonic()}, expected_device=expected_device)
        self.assertEqual(error.exception.errno, errno.EXDEV)

    def test_xcode_pins_root_before_opening_relative_categories(self):
        selected = self.root / "selected"
        replacement = self.root / "replacement"
        for path, data in ((selected, b"old"), (replacement, b"replacement bytes")):
            (path / "Xcode" / "DerivedData").mkdir(parents=True)
            (path / "Xcode" / "DerivedData" / "data").write_bytes(data)
        original = adapters._open_relative_dir
        replaced = False

        def racing_open(root_fd, relative, **kwargs):
            nonlocal replaced
            if not replaced:
                selected.rename(self.root / "original")
                replacement.rename(selected)
                replaced = True
            return original(root_fd, relative, **kwargs)

        with patch.object(adapters, "_open_relative_dir", side_effect=racing_open):
            report = adapters.scan_xcode([str(selected)])
        derived = next(item for item in report["items"] if item["category"] == "derived_data")
        self.assertEqual(derived["logical_size"], 3)
        self.assertTrue(derived["path_changed"])
        self.assertEqual(report["status"], "partial")
        self.assertTrue(any(error["code"] == "scope_changed" for error in report["errors"]))

    def test_cancel_before_adapters_performs_no_inspection(self):
        with patch.object(adapters, "_open_dir", side_effect=AssertionError("No path access after cancel")), patch.object(adapters, "_run_probe", side_effect=AssertionError("No commands after cancel")), patch.object(adapters.os, "statvfs", side_effect=AssertionError("No storage probe after cancel")):
            for adapter in (adapters.scan_applications, adapters.scan_xcode, adapters.system_overview):
                self.assertEqual(adapter(cancel=lambda: True)["status"], "cancelled")

    def test_application_cancel_retains_only_examined_bundles(self):
        self.make_app("First.app")
        self.make_app("Second.app")
        original = adapters._app_item
        examined = 0

        def inspect(*args, **kwargs):
            nonlocal examined
            result = original(*args, **kwargs)
            examined += 1
            return result

        with patch.object(adapters, "_app_item", side_effect=inspect):
            report = adapters.scan_applications([str(self.root)], cancel=lambda: examined >= 1)
        self.assertEqual(report["status"], "cancelled")
        self.assertEqual(len(report["items"]), 1)
        self.assertEqual(report["coverage"][0]["status"], "cancelled")

    def test_xcode_cancel_stops_between_categories(self):
        for relative in ("Xcode/DerivedData", "Xcode/Archives"):
            (self.root / relative).mkdir(parents=True)
            (self.root / relative / "file").write_bytes(b"data")
        original = adapters._tree_size
        measured = 0

        def measure(*args, **kwargs):
            nonlocal measured
            result = original(*args, **kwargs)
            measured += 1
            return result

        with patch.object(adapters, "_tree_size", side_effect=measure):
            report = adapters.scan_xcode([str(self.root)], cancel=lambda: measured >= 1)
        self.assertEqual(report["status"], "cancelled")
        self.assertEqual(measured, 1)
        self.assertEqual(len(report["items"]), 1)

    def test_overview_cancel_stops_between_probes(self):
        calls = 0

        def probe(argv):
            nonlocal calls
            calls += 1
            return {"status": "available", "text": "1024"}

        with patch.object(adapters, "_run_probe", side_effect=probe):
            report = adapters.system_overview(cancel=lambda: calls >= 1)
        self.assertEqual(report["status"], "cancelled")
        self.assertEqual(calls, 1)
        self.assertNotIn("vm_statistics", report["measurements"])

    def test_overview_measurements_do_not_claim_reclaimability_or_pressure(self):
        fs = SimpleNamespace(f_blocks=100, f_frsize=4096, f_bfree=40, f_bavail=30)
        responses = [
            {"status": "available", "text": "17179869184"},
            {"status": "available", "text": "Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: 100.\nPages active: 200.\nPages wired down: 300.\n"},
        ]
        with patch.object(adapters.os, "statvfs", return_value=fs), patch.object(adapters, "_run_probe", side_effect=responses) as probe:
            report = adapters.system_overview()
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["measurements"]["physical_memory"]["bytes"], 17179869184)
        self.assertEqual(report["measurements"]["vm_statistics"]["page_size"], 16384)
        self.assertEqual(report["measurements"]["vm_statistics"]["pressure"], "not_measured")
        self.assertEqual(report["measurements"]["storage"]["available_bytes"], 30 * 4096)
        self.assertIsNone(report["measurements"]["storage"]["reclaimable_bytes"])
        self.assertFalse(report["summary"]["cleanup_performed"])
        self.assertEqual(probe.call_args_list[0].args[0], ["/usr/sbin/sysctl", "-n", "hw.memsize"])

    def test_overview_probe_failure_is_partial(self):
        with patch.object(adapters, "_run_probe", return_value={"status": "unavailable", "reason": "timeout"}), patch.object(adapters.os, "statvfs", side_effect=OSError(errno.EACCES, "denied")):
            report = adapters.system_overview()
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["measurements"]["storage"]["status"], "unavailable")

    def test_probe_timeout_and_output_limit(self):
        with patch.object(adapters.subprocess, "run", side_effect=subprocess.TimeoutExpired("sysctl", 3)):
            self.assertEqual(adapters._run_probe(["/usr/sbin/sysctl"])["status"], "unavailable")
        result = subprocess.CompletedProcess(["fixed"], 0, stdout=b"x" * 1025, stderr=b"")
        with patch.object(adapters.subprocess, "run", return_value=result), patch.object(adapters, "_MAX_PROBE_OUTPUT", 1024):
            self.assertEqual(adapters._run_probe(["/usr/sbin/sysctl"])["status"], "unavailable")

    def test_non_macos_does_not_probe(self):
        with patch.object(adapters.platform, "system", return_value="Linux"), patch.object(adapters.subprocess, "run", side_effect=AssertionError("macOS command on Linux")):
            for adapter in [adapters.scan_applications, adapters.scan_xcode, adapters.system_overview]:
                report = adapter()
                self.assertEqual(report["status"], "unavailable")
                self.assertEqual(report["errors"][0]["code"], "unsupported_platform")


if __name__ == "__main__":
    unittest.main()

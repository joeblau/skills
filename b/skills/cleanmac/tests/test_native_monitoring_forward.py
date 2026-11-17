"""Independent user journeys over disposable files and read-only native APIs.

Never requests Photos/Accessibility permissions, invokes screen overlays, reads
personal directories, or sends real model/account requests.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import plistlib
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cleanmac_core import native_tools as native, monitoring_tools as monitor
from cleanmac_core import executor
from cleanmac_core.common import CleanMacError
from cleanmac_core.state import StateStore


HELPER = Path(__file__).resolve().parents[1] / "native/bin/CleanMac Features.app/Contents/MacOS/cleanmac-features"


class _ForwardFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cleanmac-forward-native-monitor-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.data = self.root / "chosen-files"
        self.data.mkdir()
        self.store = StateStore(self.root / "private-state")

    def file(self, name, data=b"fixture", age=0):
        path = self.data / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        stamp = time.time() - age * 86400
        os.utime(path, (stamp, stamp))
        return path

    def inspect(self, name, **options):
        with patch.object(monitor.platform, "system", return_value="Darwin"):
            return monitor.dispatch(name, "inspect", options, store=self.store)


class ForwardFixtures(_ForwardFixture):
    def test_custom_downloads_age_type_and_origin_do_not_select_files(self):
        old = self.file("archive.zip", b"archive content", age=90)
        self.file("recent.zip", b"new", age=2)
        self.file("notes.txt", b"old text", age=90)
        origins = plistlib.dumps(["https://alice:secret@example.test/private-document?token=secret#part", "https://example.test/other"])
        with patch.object(os, "getxattr", return_value=origins, create=True):
            report = self.inspect("downloads", roots=[str(self.data)], configuration={"older_than_days": 30, "file_type": "archive"})
        self.assertEqual([i["path"] for i in report["items"]], [str(old)])
        row = report["items"][0]
        self.assertFalse(row["selected"])
        self.assertIsNone(row["last_used_at"])
        self.assertEqual(row["source_origins"], ["https://example.test"])
        encoded = json.dumps(report)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("private-document", encoded)
        self.assertTrue(old.exists())

    def test_incomplete_download_is_reviewable_but_rejected_by_file_plan(self):
        partial = self.file("large.zip.crdownload", b"unfinished", age=90)
        report = self.inspect("downloads", roots=[str(self.data)], configuration={"include_origin": False})
        row = next(i for i in report["items"] if i["path"] == str(partial))
        self.assertTrue(row["protected"])
        self.assertEqual(row["protection_reason"], "incomplete-download")
        from cleanmac_core.plans import make_plan
        with self.assertRaises(CleanMacError):
            make_plan(self.store.get("scans", report["scan_id"]), [row["id"]])

    def test_malformed_download_origin_is_unknown_not_a_crash(self):
        self.file("download.zip")
        with patch.object(os, "getxattr", return_value=b"<?xml version='1.0'?><plist><array>", create=True):
            report = self.inspect("downloads", roots=[str(self.data)])
        self.assertEqual(len(report["items"]), 1)
        self.assertEqual(report["items"][0]["source_origins"], [])
        self.assertEqual(report["items"][0]["source_status"], "unknown")

    @unittest.skipUnless(sys.platform == "darwin", "Darwin descriptor xattr fixture")
    def test_real_darwin_download_origin_is_available_without_os_getxattr(self):
        file = self.file("actual-origin.zip")
        raw = plistlib.dumps(["https://example.test/private?credential=do-not-disclose"])
        libc = ctypes.CDLL(None, use_errno=True)
        function = libc.fsetxattr
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
        function.restype = ctypes.c_int
        descriptor = os.open(file, os.O_RDWR | os.O_NOFOLLOW)
        try:
            buffer = ctypes.create_string_buffer(raw)
            outcome = function(descriptor, b"com.apple.metadata:kMDItemWhereFroms", buffer, len(raw), 0, 0)
            self.assertEqual(outcome, 0, os.strerror(ctypes.get_errno()))
        finally:
            os.close(descriptor)
        report = self.inspect("downloads", roots=[str(self.data)])
        self.assertEqual(report["items"][0]["source_origins"], ["https://example.test"])
        self.assertEqual(report["items"][0]["source_status"], "origin-metadata")
        self.assertNotIn("do-not-disclose", json.dumps(report))

    def test_memory_uses_runtime_page_size_and_keeps_unknown_pressure(self):
        values = {
            ("/usr/sbin/sysctl", "-n", "hw.memsize"): b"17179869184\n",
            ("/usr/bin/vm_stat",): b"Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: 3.\nPages occupied by compressor: 7.\n",
            ("/usr/sbin/sysctl", "-n", "vm.swapusage"): b"total = 1024.00M used = 512.00M free = 512.00M (encrypted)\n",
            ("/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"): b"3\n",
            tuple(monitor._PS): b"",
        }
        with patch.object(monitor, "_probe", side_effect=lambda argv, **kwargs: {"status": "available", "data": values[tuple(argv)]}):
            report = self.inspect("memory")
        self.assertEqual(report["measurements"]["vm"]["bytes"]["free"], 49152)
        self.assertEqual(report["measurements"]["vm"]["bytes"]["compressed_storage"], 114688)
        self.assertEqual(report["measurements"]["swap"]["used_bytes"], 536870912)
        self.assertEqual(report["measurements"]["pressure"]["level"], "unknown")

    def test_network_overlapping_interfaces_are_not_added_into_speed(self):
        header = "Name Mtu Network Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll\n"
        before = (header + "en0 1500 <Link#4> aa:bb 1 0 1000 1 0 2000 0\nutun0 1500 <Link#5> 1 0 1000 1 0 2000 0\n").encode()
        after = (header + "en0 1500 <Link#4> aa:bb 2 0 1400 2 0 2800 0\nutun0 1500 <Link#5> 2 0 1400 2 0 2800 0\n").encode()
        with patch.object(monitor, "_probe", side_effect=[{"status": "available", "data": before}, {"status": "available", "data": after}]), patch.object(monitor, "_pause", return_value=True), patch.object(monitor, "_clock", side_effect=[10, 12]):
            report = self.inspect("network")
        self.assertEqual(len(report["items"]), 2)
        self.assertTrue(all(i["received_bytes_per_second"] == 200 for i in report["items"]))
        self.assertIsNone(report["measurements"]["total_bytes_per_second"])
        self.assertEqual(report["measurements"]["network_requests"], 0)

    def test_foreign_saved_scan_is_rejected_as_inventory(self):
        self.store.put("scans", {"id": "scan-device-backup", "roots": [str(self.data)], "items": [{"id": "backup-one", "path": str(self.data), "protected": True}], "tool": "device-backups", "status": "complete"})
        for name in ("downloads", "large-old-files", "storage-map"):
            with self.subTest(name=name), self.assertRaises(CleanMacError) as context:
                self.inspect(name, record_id="scan-device-backup")
            self.assertEqual(context.exception.code, "invalid_scan")

    def test_storage_drilldown_keeps_hidden_totals_and_escapes_html(self):
        self.file("visible.txt", b"12345")
        self.file(".hidden", b"1234567")
        self.file("nested/<script>alert(1)</script>.txt", b"123")
        top = self.inspect("storage-map", roots=[str(self.data)])
        self.assertEqual(top["items"][0]["logical_bytes"], 15)
        self.assertIsNone(top["items"][0]["reclaimable_bytes"])
        drilled = self.inspect("storage-map", record_id=top["scan_id"], configuration={"path": str(self.data), "include_hidden": False})
        self.assertNotIn(str(self.data / ".hidden"), [i["path"] for i in drilled["items"]])
        self.assertTrue(drilled["measurements"]["totals_include_hidden"])
        nested = self.inspect("storage-map", record_id=top["scan_id"], configuration={"path": str(self.data / "nested"), "format": "html"})
        html = next(value for key, value in nested.items() if "html" in key and isinstance(value, str))
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;", html)
        with self.assertRaises(CleanMacError):
            self.inspect("storage-map", record_id=top["scan_id"], configuration={"path": str(self.root)})

    def test_document_missing_setup_is_explicitly_unscanned(self):
        document = self.file("document.txt")
        with patch.dict(os.environ, {"CLEANMAC_FEATURES_PATH": str(self.root / "missing-helper")}):
            result = native.dispatch("document-versions", "inspect", {"roots": [str(document)]}, store=self.store)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["coverage"], "unscanned")
        self.assertFalse(self.store.list("scans"))
        self.assertTrue(document.exists())

    def test_document_denial_has_no_mutation_or_permission_escalation(self):
        document = self.file("document.txt")
        info = {"protocol_version": 1, "version": "1.0.0", "privileged": False, "capabilities": ["document-versions"]}
        def call(command, payload, **kwargs):
            if command == "info":
                return info
            self.assertEqual(command, "versions-inspect")
            raise CleanMacError("The selected file cannot be opened.", "permission_denied")
        with patch.object(native, "_call", side_effect=call), self.assertRaises(CleanMacError) as context:
            native.dispatch("document-versions", "inspect", {"roots": [str(document)]}, store=self.store)
        self.assertEqual(context.exception.code, "permission_denied")
        self.assertEqual(document.read_bytes(), b"fixture")
        self.assertFalse(self.store.list("plans"))


@unittest.skipUnless(sys.platform == "darwin" and HELPER.is_file(), "Requires the locally built optional native features helper")
class NativeImageForwardTests(_ForwardFixture):
    def bmp(self, name, width):
        stride = (width * 3 + 3) & ~3
        pixels = (b"\x00\x00\xff" * width + b"\0" * (stride - width * 3)) * width
        header = b"BM" + struct.pack("<IHHI", 54 + len(pixels), 0, 0, 54)
        header += struct.pack("<IiiHHIIiiII", 40, width, width, 1, 24, 0, len(pixels), 2835, 2835, 0, 0)
        return self.file(name, header + pixels)

    def test_native_similar_review_requires_keeper_and_binds_exact_files(self):
        first, keeper = self.bmp("red-small.bmp", 20), self.bmp("red-large.bmp", 40)
        original = {p: p.read_bytes() for p in (first, keeper)}
        scan = native.dispatch("similar-images", "inspect", {"roots": [str(self.data)]}, store=self.store)
        self.assertEqual(len(scan["pairs"]), 1)
        self.assertTrue(scan["pairs"][0]["requires_visual_review"])
        rows = {i["path"]: i for i in scan["items"]}
        with self.assertRaises(CleanMacError):
            native.dispatch("similar-images", "plan", {"record_id": scan["id"], "selected": [rows[str(first)]["id"]]}, store=self.store)
        plan = native.dispatch("similar-images", "plan", {"record_id": scan["id"], "selected": [rows[str(first)]["id"]], "configuration": {"confirm_visual_review": True, "keep": [rows[str(keeper)]["id"]]}}, store=self.store)
        self.assertEqual(plan["payload"]["file_plan"]["items"][0]["path"], str(first))
        self.assertEqual(plan["payload"]["file_plan"]["items"][0]["content_sha256"], hashlib.sha256(original[first]).hexdigest())
        self.assertEqual(plan["payload"]["keepers"][0]["content_sha256"], hashlib.sha256(original[keeper]).hexdigest())
        self.assertEqual(original, {p: p.read_bytes() for p in original})
        keeper.write_bytes(b"changed after visual review")
        with patch.object(executor, "apply_plan") as apply, self.assertRaises(CleanMacError):
            native.dispatch("similar-images", "apply", {"record_id": plan["id"], "approve": plan["digest"]}, store=self.store)
        apply.assert_not_called()
        self.assertTrue(first.exists())


if __name__ == "__main__":
    unittest.main()

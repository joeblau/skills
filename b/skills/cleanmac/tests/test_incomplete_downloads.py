from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cleanmac_core.common import CleanMacError
from cleanmac_core.inventory import scan_roots
from cleanmac_core.plans import make_plan


class IncompleteDownloadTests(unittest.TestCase):
    def test_generic_file_scan_and_plan_preserve_in_progress_downloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for suffix in (".crdownload", ".part", ".partial", ".download"):
                (root / ("file" + suffix)).write_bytes(b"partial bytes")
            (root / "complete.txt").write_bytes(b"completed")
            scan = scan_roots([str(root)])
            for item in scan["items"]:
                if item["kind"] != "file":
                    continue
                if item["path"].endswith("complete.txt"):
                    self.assertFalse(item["protected"])
                else:
                    self.assertEqual(item["protection_reason"], "incomplete-download")
                    with self.assertRaises(CleanMacError):
                        make_plan(scan, [item["id"]])

    def test_explicit_download_package_child_cannot_bypass_protection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "payload.download" / "contents"
            root.mkdir(parents=True)
            (root / "payload").write_bytes(b"in progress")
            scan = scan_roots([str(root)])
            self.assertTrue(all(item["protected"] for item in scan["items"]))
            self.assertTrue(all(item["protection_reason"] == "incomplete-download" for item in scan["items"]))


if __name__ == "__main__":
    unittest.main()

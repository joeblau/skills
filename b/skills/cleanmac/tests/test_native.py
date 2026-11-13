"""Native helper protocol tests; rejected operations never enter real Trash."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


NATIVE = Path(__file__).resolve().parents[1] / "native" / "bin" / "cleanmac-native"


@unittest.skipUnless(sys.platform == "darwin" and NATIVE.is_file(), "Build native helper on macOS first")
class NativeTests(unittest.TestCase):
    def invoke(self, *args):
        return subprocess.run([str(NATIVE), *map(str, args)], capture_output=True, text=True, timeout=15)

    def test_info_protocol(self):
        result = self.invoke("info")
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.assertEqual(info["protocol_version"], 1)
        self.assertEqual(info["capabilities"], ["trash", "trash_preflight"])
        self.assertFalse(info["privileged"])

    def test_bad_arguments_are_json(self):
        result = self.invoke("erase", "/")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stderr)["error"]["code"], "invalid_arguments")

    def test_preflight_keeps_source_and_creates_no_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, parent = self.staging(tmp)
            source = parent / "captured.txt"
            source.write_text("keep")
            before = source.stat()
            result = self.invoke("preflight", "--path", source)
            if result.returncode == 0:
                info = json.loads(result.stdout)
                self.assertEqual(info["status"], "available")
                self.assertEqual(info["source_device"], info["trash_device"])
                self.assertFalse(info["created_directory"])
                self.assertTrue(info["trash_directory"].startswith("/"))
            else:
                self.assertIn(json.loads(result.stderr)["error"]["code"],
                              {"permission_denied", "trash_unavailable", "cross_device_unavailable"})
            self.assertEqual(source.read_text(), "keep")
            self.assertEqual(source.stat().st_ino, before.st_ino)
            self.assertEqual(sorted(path.name for path in parent.iterdir()), [source.name])

    def staging(self, base):
        root = Path(base).resolve() / "state"
        parent = root / "quarantine" / "operation-123"
        parent.mkdir(parents=True, mode=0o700)
        for directory in (root, root / "quarantine", parent):
            directory.chmod(0o700)
        return root, parent

    def assert_rejected(self, source, receipt, expected=None):
        result = self.invoke("trash", "--path", source, "--receipt", receipt)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        error = json.loads(result.stderr)["error"]
        if expected:
            self.assertEqual(error["code"], expected, error)
        self.assertTrue(source.exists() or source.is_symlink())
        return error

    def test_arbitrary_file_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp).resolve() / "ordinary.txt"
            source.write_text("keep")
            self.assert_rejected(source, source.parent / "receipt.json", "unsafe_staging")
            self.assertEqual(source.read_text(), "keep")

    def test_unsafe_private_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, parent = self.staging(tmp)
            source = parent / "captured.txt"
            source.write_text("keep")
            parent.chmod(0o755)
            self.assert_rejected(source, parent / "receipt.json", "unsafe_staging")
            self.assertFalse((parent / "receipt.json").exists())

    def test_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, parent = self.staging(tmp)
            target = root / "keep.txt"
            target.write_text("keep")
            source = parent / "captured.txt"
            source.symlink_to(target)
            self.assert_rejected(source, parent / "receipt.json", "unsupported_item")
            self.assertEqual(target.read_text(), "keep")

    def test_directory_and_hardlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, parent = self.staging(tmp)
            directory = parent / "folder"
            directory.mkdir()
            self.assert_rejected(directory, parent / "receipt.json", "unsupported_item")
            original = root / "keep.txt"
            original.write_text("keep")
            linked = parent / "linked.txt"
            os.link(original, linked)
            self.assert_rejected(linked, parent / "receipt.json", "unsupported_item")

    def test_existing_receipt_prevents_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, parent = self.staging(tmp)
            source = parent / "captured.txt"
            source.write_text("keep")
            receipt = parent / "receipt.json"
            receipt.write_text('{"status":"pending"}')
            self.assert_rejected(source, receipt, "receipt_exists")
            self.assertEqual(receipt.read_text(), '{"status":"pending"}')

    def test_receipt_cannot_be_source_or_outside_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, parent = self.staging(tmp)
            source = parent / "captured.txt"
            source.write_text("keep")
            self.assert_rejected(source, source, "unsafe_staging")
            self.assert_rejected(source, root / "receipt.json", "unsafe_staging")

    def test_symlink_ancestor_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, parent = self.staging(tmp)
            source = parent / "captured.txt"
            source.write_text("keep")
            alias = root.parent / "alias"
            alias.symlink_to(root, target_is_directory=True)
            aliased = alias / "quarantine" / parent.name / source.name
            self.assert_rejected(aliased, aliased.parent / "receipt.json", "unsafe_staging")
            self.assertEqual(source.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()

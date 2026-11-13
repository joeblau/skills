"""Opt-in real Foundation Trash test; touches only newly generated fixture files."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("CLEANMAC_NATIVE_INTEGRATION") == "1",
                     "Set CLEANMAC_NATIVE_INTEGRATION=1 on macOS for disposable native roundtrip")
class NativeRoundtripTests(unittest.TestCase):
    def test_cli_scan_plan_native_trash_restore_and_no_replay(self):
        skill = Path(__file__).resolve().parents[1]
        helper = Path(os.environ.get("CLEANMAC_NATIVE_PATH", skill / "native/bin/cleanmac-native"))
        self.assertTrue(helper.is_file(), "Build native/build.sh before this explicit integration test")
        fixture = Path(tempfile.mkdtemp(prefix="cleanmac-native-roundtrip-")).resolve()
        successful = False
        try:
            downloads = fixture / "Downloads"
            downloads.mkdir()
            state = fixture / "state"
            unique = uuid.uuid4().hex
            keeper = downloads / f"cleanmac-{unique}-keeper.txt"
            target = downloads / f"cleanmac-{unique}-copy.txt"
            content = b"CleanMac disposable integration fixture\n" * 16
            keeper.write_bytes(content)
            target.write_bytes(content)
            original_inode = target.stat().st_ino

            def call(*args, expected=0):
                result = subprocess.run(
                    [sys.executable, str(skill / "scripts/cleanmac.py"), "--json", "--data-dir", str(state), *args],
                    text=True, capture_output=True, timeout=30,
                )
                if expected is not None:
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                return json.loads(result.stdout)

            scan = call("scan", "--root", str(downloads), "--areas", "files,duplicates,storage")
            self.assertEqual(scan["status"], "complete")
            self.assertEqual(len(scan["duplicate_groups"]), 1)
            items = {entry["path"]: entry for entry in scan["items"]}
            plan = call("plan", scan["id"], "--select", items[str(target)]["id"], "--keep", items[str(keeper)]["id"])
            denied = call("apply", plan["id"], "--approve", "0" * 64, expected=2)
            self.assertEqual(denied["error"]["code"], "approval_required")
            self.assertEqual(target.read_bytes(), content)
            receipt = call("apply", plan["id"], "--approve", plan["digest"], expected=None)
            if receipt.get("error", {}).get("code") == "trash_access_required":
                self.assertEqual(target.read_bytes(), content)
                self.assertEqual(target.stat().st_ino, original_inode)
                self.assertEqual(keeper.read_bytes(), content)
                for recorded in (state / "receipts").glob("*.json"):
                    pending = json.loads(recorded.read_text())
                    self.assertTrue(all(item["status"] == "not_started" for item in pending["operations"]))
                    self.assertTrue(all(not item.get("stage_path") and not item.get("trash_path") for item in pending["operations"]))
                self.assertFalse((state / "quarantine").exists())
                successful = True
                self.skipTest("Host denies Trash verification; preflight correctly prevented all file moves")
            self.assertNotEqual(receipt.get("status"), "error", receipt.get("error"))
            self.assertFalse(target.exists(), receipt.get("summary"))
            self.assertEqual(keeper.read_bytes(), content)
            operation = receipt["operations"][0]
            self.assertEqual(operation["status"], "trashed", operation.get("error"))
            trash_path = Path(operation["trash_path"])
            self.assertEqual(trash_path.read_bytes(), content)
            self.assertEqual(trash_path.stat().st_ino, original_inode)
            again = call("apply", plan["id"], "--approve", plan["digest"])
            self.assertEqual(again["id"], receipt["id"])
            self.assertEqual(again["operations"], receipt["operations"])
            restored = call("restore", receipt["id"], "--approve", receipt["id"])
            self.assertEqual(restored["operations"][0]["status"], "restored", restored.get("summary"))
            self.assertEqual(target.read_bytes(), content)
            self.assertEqual(target.stat().st_ino, original_inode)
            self.assertFalse(trash_path.exists())
            self.assertEqual(keeper.read_bytes(), content)
            successful = True
        finally:
            if successful:
                shutil.rmtree(fixture)
            else:
                print(f"Native integration fixture and recovery state preserved: {fixture}", file=sys.stderr)


if __name__ == "__main__":
    unittest.main()

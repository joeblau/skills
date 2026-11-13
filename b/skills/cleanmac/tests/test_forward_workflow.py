"""Exercise documented commands as a user, without executing cleanup.

Scenario: find duplicate Downloads, keep a chosen copy, ignore a folder for
future scans, then resume the saved review in a fresh CLI process.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == "darwin", "The published runtime targets macOS")
class ForwardWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cleanmac-forward-test-")
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "Downloads"
        self.root.mkdir()
        self.state = self.base / "state"
        self.runner = Path(__file__).resolve().parents[1] / "scripts" / "cleanmac"
        self.keeper = self.root / "Trip notes.txt"
        self.other = self.root / "Trip notes copy\n[review].txt"
        self.dash = self.root / "--not-a-command.txt"
        for path in [self.keeper, self.other, self.dash]:
            path.write_bytes(b"exact duplicated payload\n" * 8)
        (self.root / "独自.txt").write_bytes(b"a unique payload")
        (self.root / "\x1b[31mnot a terminal command.txt").write_bytes(b"control filename fixture")
        self.ignored = self.root / "keep later"
        self.ignored.mkdir()
        for name in ["download A.txt", "download B.txt"]:
            (self.ignored / name).write_bytes(b"another shared payload\n" * 6)
        self.before = self.snapshot()

    def tearDown(self):
        self.temporary.cleanup()

    def snapshot(self):
        result = {}
        for path in sorted(self.root.rglob("*")):
            info = path.lstat()
            result[str(path.relative_to(self.root))] = {
                "inode": info.st_ino,
                "mode": info.st_mode,
                "mtime_ns": info.st_mtime_ns,
                "ctime_ns": info.st_ctime_ns,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
            }
        return result

    def command(self, *args, machine=True):
        command = [str(self.runner), "--data-dir", str(self.state)]
        if machine:
            command.append("--json")
        completed = subprocess.run(command + list(args), capture_output=True, text=True, timeout=20)
        self.assertEqual(completed.returncode, 0,
                         f"Command {args!r} failed: {completed.stdout}\n{completed.stderr}")
        self.assertEqual(self.snapshot(), self.before, f"Command {args!r} changed scan targets")
        return json.loads(completed.stdout) if machine else completed.stdout

    def scan(self):
        return self.command("scan", "--root", str(self.root), "--areas", "duplicates",
                            "--duplicate-min-size", "1")

    @staticmethod
    def item_id(scan, path):
        info = path.stat()
        return next(item["id"] for item in scan["items"]
                    if item["device"] == info.st_dev and item["inode"] == info.st_ino)

    def test_duplicate_keep_ignore_and_resume_preserve_targets(self):
        scan = self.scan()
        self.assertEqual(scan["status"], "complete")
        self.assertEqual(len(scan["duplicate_groups"]), 2)
        self.assertEqual(scan["summary"]["selected_count"], 0)
        keeper_id = self.item_id(scan, self.keeper)
        selected_ids = [self.item_id(scan, self.other), self.item_id(scan, self.dash)]

        review = self.command("review", scan["id"], "--category", "duplicates", "--limit", "20")
        self.assertEqual(review["pagination"]["total"], 5)
        self.assertEqual(len(review["duplicate_groups"]), 2)

        # Choosing what to retain and preparing a preview does not approve it.
        plan = self.command("plan", scan["id"], "--select", selected_ids[0],
                            "--select", selected_ids[1], "--keep", keeper_id)
        self.assertEqual({item["id"] for item in plan["items"]}, set(selected_ids))
        self.assertEqual([item["id"] for item in plan["keepers"]], [keeper_id])
        self.assertEqual(plan["status"], "ready")
        self.assertIsNone(plan["summary"]["reclaimable_bytes"])

        self.command("ignore", "add", "--path", str(self.ignored))
        ignores = self.command("ignore", "list")
        self.assertIn(str(self.ignored), ignores["ignore_paths"])
        later = self.scan()
        self.assertEqual(len(later["duplicate_groups"]), 1)
        ignored_child_ids = {self.item_id(scan, self.ignored / name)
                             for name in ["download A.txt", "download B.txt"]}
        self.assertFalse(ignored_child_ids & {item["id"] for item in later["items"]})

        # Each command is a new process: history is the documented resume path.
        history = self.command("history")
        self.assertEqual(history["receipts"], [])
        self.assertIn(plan["id"], {record["id"] for record in history["plans"]})
        resumed = self.command("history", "--id", plan["id"])
        self.assertEqual(resumed["id"], plan["id"])
        self.assertEqual(resumed["digest"], plan["digest"])
        self.assertEqual({item["id"] for item in resumed["items"]}, set(selected_ids))
        self.assertEqual([item["id"] for item in resumed["keepers"]], [keeper_id])
        original_review = self.command("review", scan["id"], "--category", "duplicates", "--limit", "20")
        self.assertEqual(len(original_review["duplicate_groups"]), 2,
                         "Future ignore preferences should not rewrite a saved scan")

    def test_review_displays_control_filenames_without_terminal_sequences(self):
        scan = self.scan()
        text = self.command("review", scan["id"], "--limit", "30", machine=False)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("Trip notes copy\n[review].txt", text)
        self.assertIn("Trip notes", text)
        self.assertIn("not a terminal command.txt", text)
        self.assertEqual(self.command("history")["receipts"], [])


if __name__ == "__main__":
    unittest.main()

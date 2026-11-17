"""Forward evaluation through the real CLI in a disposable home directory.

No personal folders, provider accounts, native Trash, or schedule installation
are used. These exercise records across fresh command processes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


RUNNER = Path(__file__).resolve().parents[1] / "scripts" / "cleanmac"


@unittest.skipUnless(sys.platform == "darwin", "The product CLI gates operational commands to macOS.")
class CrossToolWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cleanmac-forward-")
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / "home"
        self.downloads = self.home / "Downloads"
        self.downloads.mkdir(parents=True)
        (self.home / "Library").mkdir()
        self.state = self.base / "state"
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.home),
                    "LANG": "en_US.UTF-8", "PYTHONIOENCODING": "utf-8"}
        self.keep = self.downloads / "keep.txt"
        self.remove = self.downloads / "copy.txt"
        self.keep.write_bytes(b"disposable duplicate fixture\n")
        self.remove.write_bytes(self.keep.read_bytes())
        self.unique = self.downloads / "unique.txt"
        self.unique.write_bytes(b"different fixture\n")
        self.before = {path.name: path.read_bytes() for path in self.downloads.iterdir()}

    def tearDown(self):
        self.assertEqual(self.before, {path.name: path.read_bytes() for path in self.downloads.iterdir()})
        self.assertFalse((self.home / "Library" / "LaunchAgents").exists())
        self.temp.cleanup()

    def command(self, *args, expected=0):
        result = subprocess.run([str(RUNNER), "--json", "--data-dir", str(self.state), *args],
                                capture_output=True, text=True, env=self.env, timeout=30, check=False)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def config(self, **values):
        return json.dumps(values)

    def save_scope(self):
        return self.command("tool", "saved-scopes", "--action", "save", "--root", str(self.downloads),
                            "--config", self.config(name="DownloadsOnly", areas=["duplicates", "storage"]))

    def test_saved_downloads_duplicate_favorite_and_keeper_resume(self):
        saved = self.save_scope()
        self.assertFalse(saved["saved_approval"])
        favorite = self.command("tool", "favorites", "--action", "add", "--config", self.config(name="duplicates"))
        self.assertIn("duplicates", favorite["favorites"])
        spec = self.command("tools", "duplicates")["tools"][0]
        self.assertIn("plan", spec["actions"])
        scan = self.command("tool", "saved-scopes", "--action", "run", "--config", self.config(name="DownloadsOnly"))
        self.assertEqual(scan["roots"], [str(self.downloads)])
        by_name = {Path(item["path"]).name: item["id"] for item in scan["items"]}
        suggested = scan["duplicate_groups"][0]["keeper_id"]
        chosen_keeper = next(identifier for identifier in scan["duplicate_groups"][0]["item_ids"] if identifier != suggested)
        duplicate_review = self.command("tool", "duplicates", "--record", scan["id"])
        self.assertEqual({item["id"] for item in duplicate_review["items"]}, {by_name["copy.txt"], by_name["keep.txt"]})
        plan = self.command("tool", "duplicates", "--action", "plan", "--record", scan["id"],
                            "--select", suggested, "--config", self.config(keep=[chosen_keeper]))
        resumed = self.command("history", "--id", plan["id"])
        self.assertEqual([item["id"] for item in resumed["keepers"]], [chosen_keeper])
        self.assertEqual([item["id"] for item in resumed["items"]], [suggested])
        self.assertEqual(resumed["digest"], plan["digest"])
        self.assertEqual(self.command("history")["receipts"], [])
        refused = self.command("tool", "duplicates", "--action", "plan", "--record", scan["id"],
                               "--select", by_name["unique.txt"], expected=2)
        self.assertEqual(refused["error"]["code"], "invalid_selection")

    def test_schedule_plan_is_reviewable_and_cancel_leaves_no_installation(self):
        self.save_scope()
        stored = self.command("tool", "saved-scopes")["presets"]["DownloadsOnly"]
        roots = [entry["path"] for entry in stored["scan"]["roots"]]
        self.assertEqual(roots, [str(self.downloads)])
        plan = self.command("tool", "schedules", "--action", "plan",
                            "--config", self.config(name="DownloadsWeekly", operation="create", preset="DownloadsOnly", interval_seconds=604800, notify=False))
        agent = plan["payload"]["launchagent"]
        self.assertEqual(agent["StartInterval"], 604800)
        self.assertFalse(agent["RunAtLoad"])
        self.assertIn("-I", agent["ProgramArguments"])
        self.assertEqual(plan["summary"]["roots"], roots)
        self.assertTrue(plan["summary"]["read_only"])
        self.assertFalse(plan["summary"]["notifications"])
        self.assertEqual(plan["payload"]["program"], agent["ProgramArguments"])
        # Cancel means do not call apply. Removing this unexecuted plan also
        # proves it does not carry a receipt or an installed LaunchAgent.
        self.command("forget", plan["id"])
        schedules = self.command("tool", "schedules")
        self.assertEqual(schedules["schedules"], [])
        self.assertEqual(self.command("history")["receipts"], [])

    def test_missing_accounts_remain_unscanned_with_setup_handoffs(self):
        # The environment is deliberately minimal: no host credential env vars
        # can leak into the test or cause a real account lookup.
        for name in ("google-drive", "onedrive", "dropbox"):
            report = self.command("tool", name, "--root", "root", "--config", self.config(token_env="CLEANMAC_FORWARD_MISSING_TOKEN"))
            self.assertEqual(report["coverage"], "unscanned")
            self.assertIn(report["status"], {"dependency-needed", "unavailable", "permission-needed"})
            self.assertFalse(report.get("items"))
            spec = self.command("tools", name)["tools"][0]
            self.assertIn("oauth_connection", spec["configuration"])
        report = self.command("tool", "gmail")
        self.assertEqual(report["coverage"], "unscanned")
        self.assertEqual(report["status"], "dependency-needed")
        self.assertFalse(report["items"])
        spec = self.command("tools", "gmail")["tools"][0]
        self.assertIn("oauth_connection", spec["configuration"])
        setup = self.command("tool", "accounts")
        self.assertEqual(setup["connections"], [])
        self.assertEqual({provider["provider"] for provider in setup["providers"]}, {"gmail", "google-drive", "onedrive", "dropbox"})
        self.assertTrue(all(provider["registration_url"].startswith("https://") for provider in setup["providers"]))
        actions = self.command("tools", "accounts")["tools"][0]
        self.assertIn("connect", actions["actions"])
        self.assertIn("client_id", actions["configuration"])
        history = self.command("history")
        self.assertEqual(history["scans"], [])
        self.assertEqual(history["receipts"], [])


if __name__ == "__main__":
    unittest.main()

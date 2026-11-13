"""Independent permission conversations using disposable roots and denied fakes."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from cleanmac_core import cli, permissions, review
from cleanmac_core.state import StateStore


class PermissionConversationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cleanmac-permission-flow-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.downloads = self.home / "Downloads"
        self.downloads.mkdir()
        self.mail = self.home / "Library" / "Mail"
        self.mail.mkdir(parents=True)
        (self.downloads / "retained.txt").write_text("ordinary file")
        (self.mail / "never-read.txt").write_text("fixture content")

    def denied(self, number=errno.EPERM):
        with patch.dict(os.environ, {"TERM_PROGRAM": "Apple_Terminal"}):
            denied = permissions.describe_access_error(str(self.mail), PermissionError(number, "fixture denial"))
        return {
            "status": "available", "access": [denied],
            "permission_guidance": {"host": denied["host"]},
        }

    def wizard(self, answers, cap, *, succeed_after=999, scan_status="complete"):
        calls, output = [], []
        answers = iter(answers)
        def dispatch(argv):
            calls.append(list(argv))
            if argv[0] == "capabilities":
                if sum(call[0] == "capabilities" for call in calls) > succeed_after:
                    return {"status": "available", "access": [{"path": str(self.mail), "status": "available"}]}
                return cap
            if argv[0] == "permissions":
                return {"status": "opened", "permission_changed": False}
            if argv[0] == "scan":
                return {"id": "scan-fixture", "status": scan_status, "roots": [str(self.mail)], "items": [], "findings": [], "errors": []}
            raise AssertionError("Unexpected action: " + repr(argv))
        def ask(prompt):
            output.append(prompt)
            value = next(answers)
            if isinstance(value, BaseException):
                raise value
            return value
        result = review.interactive_wizard(dispatch, input_fn=ask, output=output.append, is_tty=True, home=self.home)
        return result, calls, "\n".join(output)

    def test_skip_mail_and_continue_only_selected_downloads_using_dispatcher(self):
        """Agent-driven multi-root workflow never silently retries denied Mail."""
        store = StateStore(self.home / "private-state")
        observed_probes = []
        def probe(path):
            observed_probes.append(path)
            if path == str(self.mail):
                return self.denied()["access"][0]
            self.assertEqual(path, str(self.downloads))
            return {"path": path, "status": "available", "coverage": "root-only"}
        with patch.object(permissions, "_probe_root", side_effect=probe), patch.object(permissions, "_helper_status", return_value={"status": "missing"}), patch.object(permissions.platform, "system", return_value="Darwin"):
            cap = cli.dispatch(["capabilities", "--root", str(self.mail), "--root", str(self.downloads)])
        approved_roots = [entry["path"] for entry in cap["access"] if entry["status"] == "available"]
        self.assertEqual(approved_roots, [str(self.downloads)])
        with patch.object(sys, "platform", "darwin"), patch.object(permissions, "open_permission_settings", side_effect=AssertionError("implicit Settings action")):
            scan = cli.dispatch(["scan", "--root", approved_roots[0], "--areas", "files,storage"], store=store)
        self.assertEqual(observed_probes, [str(self.mail), str(self.downloads)])
        self.assertEqual(scan["roots"], [str(self.downloads)])
        self.assertEqual(scan["summary"]["selected_count"], 0)
        self.assertTrue(all(str(item["path"]).startswith(str(self.downloads)) for item in scan["items"]))
        self.assertEqual(store.list("plans"), [])
        self.assertEqual(store.list("receipts"), [])
        self.assertEqual((self.mail / "never-read.txt").read_text(), "fixture content")

    def test_mail_settings_uses_suggested_pane_without_reprobing_before_retry(self):
        result, calls, output = self.wizard(["3", str(self.mail), "scan", "settings", "continue", "5"], self.denied())
        self.assertEqual([call[0] for call in calls], ["capabilities", "permissions"])
        self.assertEqual(calls[1], ["permissions", "--open-settings", "--pane", "full-disk-access"])
        self.assertIn("Terminal", output)
        self.assertIn("exact app", output)
        self.assertIn("No scan ran", output)
        self.assertEqual(result["status"], "cancelled")

    def test_ordinary_permission_denial_can_skip_without_settings_or_escalation(self):
        result, calls, output = self.wizard(["3", str(self.mail), "scan", "skip", "5"], self.denied(errno.EACCES))
        self.assertEqual([call[0] for call in calls], ["capabilities"])
        self.assertIn("Filesystem permissions or an ACL denied access", output)
        self.assertIn("No scan ran", output)
        self.assertEqual(result["status"], "cancelled")

    def test_retry_rechecks_original_root_and_leaves_selection_empty(self):
        result, calls, output = self.wizard(["3", str(self.mail), "scan", "retry", "plan", "done"], self.denied(), succeed_after=1)
        self.assertEqual([call[0] for call in calls], ["capabilities", "capabilities", "scan"])
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(calls[2], ["scan", "--root", str(self.mail), "--areas", "files,storage"])
        self.assertEqual(result["selected_ids"], [])
        self.assertIn("Nothing is selected", output)

    def test_back_from_permission_handoff_runs_no_scan(self):
        result, calls, _ = self.wizard(["3", str(self.mail), "scan", "back", "5"], self.denied())
        self.assertEqual([call[0] for call in calls], ["capabilities"])
        self.assertEqual(result["status"], "cancelled")

    def test_keyboard_interrupt_during_permission_choice_stops_without_mutation(self):
        result, calls, _ = self.wizard(["3", str(self.mail), "scan", KeyboardInterrupt()], self.denied())
        self.assertEqual([call[0] for call in calls], ["capabilities"])
        self.assertEqual(result["status"], "cancelled")

    def test_popup_explanation_precedes_any_selected_root_probe(self):
        events = []
        answers = iter(["3", str(self.mail), "scan", "skip", "5"])
        def dispatch(argv):
            events.append("PROBE " + argv[0])
            return self.denied()
        review.interactive_wizard(dispatch, input_fn=lambda _: next(answers), output=events.append, is_tty=True, home=self.home)
        popup = next(index for index, value in enumerate(events) if "macOS may show" in value)
        probe = events.index("PROBE capabilities")
        self.assertLess(popup, probe)

    @unittest.skipUnless(os.name == "posix", "PTY and SIGINT need POSIX")
    def test_sigint_at_real_terminal_prompt_exits_without_state_or_scan(self):
        master, slave = pty.openpty()
        state_path = self.home / "unused-state"
        process = subprocess.Popen(
            [sys.executable, str(SCRIPTS / "cleanmac.py"), "--data-dir", str(state_path)],
            stdin=slave, stdout=slave, stderr=slave, close_fds=True, start_new_session=True,
        )
        os.close(slave)
        try:
            received = b""
            deadline = time.monotonic() + 5
            while b"Choose> " not in received and time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    received += os.read(master, 65536)
            self.assertIn(b"Choose> ", received)
            process.send_signal(signal.SIGINT)
            process.wait(timeout=2)
            self.assertFalse(state_path.exists())
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=2)
            os.close(master)


if __name__ == "__main__":
    unittest.main()

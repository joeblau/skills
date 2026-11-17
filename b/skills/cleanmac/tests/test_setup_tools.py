from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cleanmac_core import setup_tools
from cleanmac_core.common import CleanMacError


class SetupTests(unittest.TestCase):
    def test_smoke_uses_only_disposable_files_and_never_executes_cleanup(self):
        with patch("cleanmac_core.executor.apply_plan", side_effect=AssertionError("cleanup called")), patch("subprocess.run", side_effect=AssertionError("native process called")):
            report = setup_tools.dispatch("setup", "smoke", {}, store=None)
        self.assertEqual(report["status"], "complete")
        self.assertIn("private-state-roundtrip", report["checks"])
        self.assertFalse(report["user_files_changed"])
        self.assertFalse(report["native_trash_tested"])
        self.assertFalse(report["provider_accounts_tested"])

    def test_smoke_rejects_user_roots_or_credential_configuration(self):
        for options in ({"roots": ["/Users/example/Downloads"]}, {"configuration": {"token": "private"}}, {"approve": "digest"}):
            with self.subTest(options=options), self.assertRaises(CleanMacError):
                setup_tools.dispatch("setup", "smoke", options, store=None)

    def test_cancel_before_start_creates_no_fixtures(self):
        with patch.object(setup_tools.tempfile, "TemporaryDirectory", side_effect=AssertionError("created fixture")):
            self.assertEqual(setup_tools._smoke(cancel=lambda: True)["status"], "cancelled")

    def test_inspection_has_explicit_build_commands_no_auto_install(self):
        supplied = {"helper": {"status": "missing"}, "status": "available"}
        with patch("cleanmac_core.permissions.capabilities", return_value=supplied) as capabilities, patch("subprocess.run", side_effect=AssertionError("process called")):
            report = setup_tools.dispatch("setup", "inspect", {"roots": ["/chosen"]}, store=None)
        self.assertEqual(capabilities.call_args.args[0], ["/chosen"])
        self.assertFalse(report["setup"]["native_trash"]["built"])
        self.assertEqual(report["setup"]["native_trash"]["build_argv"][0], "/bin/sh")
        self.assertNotIn("access_token", json.dumps(report))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cleanmac_core.common import CleanMacError
from cleanmac_core import permissions


class PermissionExplanationTests(unittest.TestCase):
    def test_eacces_is_not_misrepresented_as_full_disk_access(self):
        result = permissions.describe_access_error("/selected", PermissionError(errno.EACCES, "denied"))
        self.assertEqual(result["reason"], "filesystem-permission-denied")
        self.assertIsNone(result["system_settings_url"])
        self.assertEqual(result["coverage"], "unscanned")
        self.assertTrue(result["can_continue_with_accessible_items"])

    def test_eperm_preserves_uncertain_policy_cause_and_choices(self):
        result = permissions.describe_access_error("/Users/example/Downloads", PermissionError(errno.EPERM, "denied"))
        self.assertEqual(result["reason"], "privacy-or-system-policy-denied")
        self.assertEqual(result["suggested_settings_pane"], "files-and-folders")
        self.assertEqual([action["id"] for action in result["actions"]], ["skip", "retry", "open-settings"])
        self.assertTrue(result["actions"][-1]["requires_explicit_request"])

    def test_app_data_guidance_does_not_claim_granted_fda(self):
        result = permissions.describe_access_error("/Users/example/Library/Mail", OSError(errno.EPERM, "denied"))
        self.assertEqual(result["suggested_settings_pane"], "full-disk-access")
        self.assertEqual(result["status"], "permission-needed")
        self.assertNotIn("granted", result)

    def test_missing_volume_is_not_permission_request(self):
        result = permissions.describe_access_error("/Volumes/offline", FileNotFoundError(errno.ENOENT, "gone"))
        self.assertEqual(result["reason"], "location-missing")
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["system_settings_url"])

    def test_host_hint_is_not_authoritative_and_unknown_environment_is_not_echoed(self):
        with patch.dict(os.environ, {"TERM_PROGRAM": "Apple_Terminal"}, clear=True):
            host = permissions._host_context()
            self.assertEqual(host["host_hint"], "Terminal")
            self.assertEqual(host["attribution"], "unverified")
        with patch.dict(os.environ, {"TERM_PROGRAM": "\x1b[31muntrusted", "SECRET_TOKEN": "not-for-output"}, clear=True):
            encoded = json.dumps(permissions._host_context())
            self.assertNotIn("untrusted", encoded)
            self.assertNotIn("not-for-output", encoded)


class RootAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_directory_probe_does_not_open_file_contents(self):
        payload = self.root / "private.txt"
        payload.write_text("leave contents untouched")
        with patch("builtins.open", side_effect=AssertionError("content open")):
            result = permissions._probe_root(self.root)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["coverage"], "root-only")
        self.assertEqual(result["full_disk_access"], "unknown")
        self.assertEqual(payload.read_text(), "leave contents untouched")

    def test_file_root_is_not_content_opened(self):
        payload = self.root / "file"
        payload.write_text("data")
        result = permissions._probe_root(payload)
        self.assertEqual(result["reason"], "directory-required")

    def test_root_symlink_is_not_followed(self):
        target = self.root / "actual"
        target.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(target, target_is_directory=True)
        result = permissions._probe_root(alias)
        self.assertEqual(result["reason"], "symlink-not-followed")

    def test_parent_symlink_is_not_followed(self):
        target = self.root / "actual"
        target.mkdir()
        (target / "child").mkdir()
        alias = self.root / "alias"
        alias.symlink_to(target, target_is_directory=True)
        result = permissions._probe_root(alias / "child")
        self.assertEqual(result["status"], "unavailable")

    def test_denied_listing_reports_unscanned(self):
        with patch.object(permissions.os, "scandir", side_effect=PermissionError(errno.EPERM, "denied")):
            result = permissions._probe_root(self.root)
        self.assertEqual(result["status"], "permission-needed")
        self.assertEqual(result["coverage"], "unscanned")

    def test_invalid_roots_are_structured_errors(self):
        for root in ("", "a\x00b", b"bytes", 1):
            with self.subTest(root=root), self.assertRaises(CleanMacError):
                permissions._probe_root(root)


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.missing_helper = self.root / "missing-native"

    def test_no_implicit_protected_roots_or_settings_open(self):
        with patch.object(permissions.platform, "system", return_value="Darwin"), patch.object(permissions, "_probe_root", side_effect=AssertionError("implicit root probe")), patch.object(permissions.subprocess, "run", side_effect=AssertionError("unexpected process")):
            result = permissions.capabilities(native_path=self.missing_helper)
        self.assertEqual(result["access"], [])
        self.assertEqual(result["helper"]["status"], "missing")
        self.assertEqual(result["permission_state"]["full_disk_access"], "unknown")
        self.assertEqual(result["permission_state"]["app_sandbox"], "unknown")

    def test_missing_helper_disables_apply_but_not_inventory(self):
        with patch.object(permissions.platform, "system", return_value="Darwin"), patch.object(permissions, "_module_available", return_value=True):
            result = permissions.capabilities(native_path=self.missing_helper)
        features = {item["id"]: item for item in result["capabilities"]}
        self.assertEqual(len(features), 76)
        self.assertEqual(features["CM-003"]["status"], "available")
        self.assertEqual(features["CM-005"]["status"], "unavailable")
        self.assertEqual(features["CM-027"]["permitted_actions"], [])
        self.assertEqual(features["CM-027"]["status"], "unavailable")
        self.assertEqual(features["CM-006"]["command"], "review")
        self.assertIsNone(features["CM-009"]["command"])
        self.assertEqual(features["CM-009"]["invocation"], [])
        self.assertEqual(features["CM-010"]["command"], "ignore")

    def test_unimportable_adapter_is_not_advertised(self):
        with patch.object(permissions.Path, "is_file", return_value=True), patch.object(permissions.importlib, "import_module", side_effect=ImportError("broken dependency")):
            self.assertFalse(permissions._module_available("inventory"))

    def test_unsupported_platform_does_not_probe_or_run_macos_tools(self):
        with patch.object(permissions.platform, "system", return_value="Linux"), patch.object(permissions, "_probe_root", side_effect=AssertionError("probe on unsupported OS")), patch.object(permissions.subprocess, "run", side_effect=AssertionError("macOS tool called")):
            result = permissions.capabilities([str(self.root)])
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(all(item["status"] == "unavailable" for item in result["capabilities"]))

    def test_mixed_access_keeps_denied_scope_visible(self):
        with patch.object(permissions.platform, "system", return_value="Darwin"):
            result = permissions.capabilities([str(self.root), str(self.root / "missing")], native_path=self.missing_helper)
        self.assertEqual([item["status"] for item in result["access"]], ["available", "unavailable"])
        self.assertEqual(result["access"][1]["coverage"], "unscanned")

    def test_rejects_string_root_argument_and_excessive_roots(self):
        for roots in ("/tmp", ["/tmp"] * 129):
            with self.subTest(roots=type(roots)), self.assertRaises(CleanMacError):
                permissions.capabilities(roots)

    def test_helper_read_only_handshake_does_not_claim_privileged_authorization(self):
        helper = self.root / "native"
        helper.write_text("fixture")
        helper.chmod(0o700)
        info = {"protocol_version": 1, "version": "1.0.0", "capabilities": ["trash"], "privileged": False}
        process = subprocess.CompletedProcess([], 0, json.dumps(info), "")
        with patch.object(permissions.subprocess, "run", return_value=process) as run:
            result = permissions._helper_status(helper, True)
        self.assertEqual(run.call_args.args[0], [str(helper), "info"])
        self.assertEqual(result["status"], "available")
        self.assertFalse(result["privileged"])
        self.assertEqual(result["authorization"], "not-applicable")
        self.assertEqual(result["full_disk_access"], "unknown")

    def test_incompatible_or_timed_out_helper_is_unavailable(self):
        helper = self.root / "native"
        helper.write_text("fixture")
        helper.chmod(0o700)
        info = {"protocol_version": 2, "version": "2", "capabilities": ["trash"], "privileged": False}
        with patch.object(permissions.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(info), "")):
            self.assertEqual(permissions._helper_status(helper, True)["status"], "incompatible")
        with patch.object(permissions.subprocess, "run", side_effect=subprocess.TimeoutExpired("info", 3)):
            self.assertEqual(permissions._helper_status(helper, True)["status"], "unresponsive")

    def test_writable_or_symlink_helper_is_never_executed(self):
        helper = self.root / "native"
        helper.write_text("fixture")
        helper.chmod(0o777)
        link = self.root / "link"
        link.symlink_to(helper)
        with patch.object(permissions.subprocess, "run", side_effect=AssertionError("unsafe helper executed")):
            self.assertEqual(permissions._helper_status(helper, True)["status"], "unsafe-path")
            self.assertEqual(permissions._helper_status(link, True)["status"], "unsafe-path")


class SettingsTests(unittest.TestCase):
    def test_only_explicit_fixed_settings_urls_are_opened(self):
        with patch.object(permissions.platform, "system", return_value="Darwin"), patch.object(permissions.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            result = permissions.open_permission_settings()
        self.assertEqual(run.call_args.args[0], ["/usr/bin/open", permissions.SETTINGS_URLS["files-and-folders"]])
        self.assertFalse(result["permission_changed"])
        self.assertEqual(result["status"], "opened")

    def test_arbitrary_url_and_nonmacos_are_rejected(self):
        with patch.object(permissions.subprocess, "run", side_effect=AssertionError("unexpected process")):
            with self.assertRaises(CleanMacError):
                permissions.open_permission_settings("https://example.com")
            with patch.object(permissions.platform, "system", return_value="Linux"), self.assertRaises(CleanMacError):
                permissions.open_permission_settings()


if __name__ == "__main__":
    unittest.main()

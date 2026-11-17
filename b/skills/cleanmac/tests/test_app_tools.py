"""Disposable app/provider fixtures; never query installed apps or mutate Homebrew."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import plistlib
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cleanmac_core import app_tools
from cleanmac_core.common import CleanMacError
from cleanmac_core.state import StateStore


class AppToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.store = StateStore(self.root / "state")
        self.apps = self.root / "Applications"
        self.apps.mkdir()
        self.bundle = self.make_app("Fixture", "org.example.fixture")
        self.provider = self.root / "provider" / "brew"
        self.provider.parent.mkdir()
        self.provider.write_text("#!/bin/sh\nexit 0\n")
        self.provider.chmod(0o700)
        self.caskroom = self.root / "Caskroom"
        self.meta = self.caskroom / "fixture" / ".metadata"
        self.definition = self.meta / "1.0" / "20260101000000.000" / "Casks" / "fixture.json"
        self.definition.parent.mkdir(parents=True)
        self.definition.write_text("{}")
        self.receipt = {"source": {"version": "1.0", "tap": "homebrew/cask"},
                        "uninstall_flight_blocks": False, "uninstall_artifacts": [{"app": ["Fixture.app"]}],
                        "runtime_dependencies": {}}
        self.receipt_path = self.meta / "INSTALL_RECEIPT.json"
        self.receipt_path.write_text(json.dumps(self.receipt))
        self.cask = {"token": "fixture", "name": ["Fixture"], "tap": "homebrew/cask", "old_tokens": [],
                     "installed": "1.0", "version": "2.0", "pinned": False, "disabled": False,
                     "artifacts": [{"app": ["Fixture.app"], "target": str(self.bundle)}],
                     "sha256": "a" * 64, "url": "https://example.org/app.dmg", "auto_updates": False,
                     "depends_on": {"macos": {">=": ["12"]}}}
        self.options = {"roots": [str(self.apps)], "configuration": {"brew_path": str(self.provider)},
                        "selected": [], "record_id": None, "approve": None}
        self.platform = mock.patch.object(app_tools.platform, "system", return_value="Darwin")
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.addCleanup(self.tmp.cleanup)

    def make_app(self, name, identifier):
        bundle = self.apps / (name + ".app")
        (bundle / "Contents" / "MacOS").mkdir(parents=True)
        (bundle / "Contents" / "Resources" / "en.lproj").mkdir(parents=True)
        (bundle / "Contents" / "Resources" / "fr.lproj").mkdir()
        (bundle / "Contents" / "Resources" / "en.lproj" / "Localizable.strings").write_bytes(b"english")
        (bundle / "Contents" / "Resources" / "fr.lproj" / "Localizable.strings").write_bytes(b"francais")
        with (bundle / "Contents" / "Info.plist").open("wb") as f:
            plistlib.dump({"CFBundleIdentifier": identifier, "CFBundleExecutable": name,
                          "CFBundleName": name, "CFBundleVersion": "1"}, f)
        (bundle / "Contents" / "MacOS" / name).write_bytes(b"\xcf\xfa\xed\xfe" + struct.pack("<I", 0x0100000C) + b"\0" * 32)
        return bundle

    def fake_run(self, argv, **kwargs):
        if argv[0] == "/usr/sbin/sysctl":
            return {"returncode": 0, "stdout": "1\n", "stderr": ""}
        if argv[0] == "/usr/bin/mdls":
            return {"returncode": 0, "stdout": "(null)\n", "stderr": ""}
        if argv[0] == "/bin/ps":
            return {"returncode": 0, "stdout": "/usr/bin/python3\n", "stderr": ""}
        if argv[0] == "/usr/bin/codesign":
            ident = plistlib.loads((Path(argv[-1]) / "Contents" / "Info.plist").read_bytes())["CFBundleIdentifier"]
            return {"returncode": 0, "stdout": "", "stderr": f"Identifier={ident}\nTeamIdentifier=TEAM123\n"}
        raise AssertionError(f"Unmocked command: {argv}")

    def fake_brew(self, provider, args, **kwargs):
        if args == ["--caskroom"]:
            return str(self.caskroom) + "\n"
        if args[0] == "info":
            return json.dumps({"casks": [self.cask]})
        if args[0] == "outdated":
            return json.dumps({"casks": [{"name": "fixture", "installed_versions": ["1.0"], "current_version": "2.0"}]})
        raise AssertionError(f"Unexpected mutation: {args}")

    def assert_code(self, code, fn, *args, **kwargs):
        with self.assertRaises(CleanMacError) as error:
            fn(*args, **kwargs)
        self.assertEqual(code, error.exception.code)

    def plan(self, name="app-uninstall"):
        with mock.patch.object(app_tools, "_run", side_effect=self.fake_run), mock.patch.object(app_tools, "_brew", side_effect=self.fake_brew):
            report = app_tools.dispatch(name, "inspect", self.options, store=self.store)
            options = dict(self.options, record_id=report["id"], selected=[report["items"][0]["id"]])
            return app_tools.dispatch(name, "plan", options, store=self.store)

    def test_application_reports_preserve_bundles_and_distinct_ids(self):
        second = self.make_app("Other", "org.other.fixture")
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.apps.rglob("*") if p.is_file()}
        with mock.patch.object(app_tools, "_run", side_effect=self.fake_run):
            report = app_tools.dispatch("applications", "inspect", self.options, store=self.store)
        self.assertEqual(report["status"], "complete", report)
        self.assertEqual(len({x["id"] for x in report["items"]}), 2)
        self.assertEqual({x["bundle_id"] for x in report["items"]}, {"org.example.fixture", "org.other.fixture"})
        self.assertTrue(all(x["unused_status"] == "unknown" for x in report["items"]))
        self.assertTrue(all(x["compatibility_status"] == "native-main-executable" for x in report["items"]))
        self.assertTrue(all(x["eligible"] is False for x in report["items"]))
        self.assertEqual(before, {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.apps.rglob("*") if p.is_file()})

    def test_application_scan_tolerates_unrelated_ancestor_entry_change(self):
        # A sibling file changes the ancestor's directory timestamps, not its
        # inode or the selected app root. This models concurrent temp fixtures
        # without relying on thread scheduling or touching a shared host folder.
        original_open = app_tools.adapters.os.open
        changed = False

        def opening(path, flags, *args, **kwargs):
            nonlocal changed
            if path == self.root.name and not changed:
                changed = True
                sibling = self.root / "unrelated-new-entry"
                sibling.write_bytes(b"unrelated")
                sibling.unlink()
            return original_open(path, flags, *args, **kwargs)

        with mock.patch.object(app_tools.adapters.os, "open", side_effect=opening), mock.patch.object(app_tools, "_run", side_effect=self.fake_run):
            report = app_tools.dispatch("applications", "inspect", self.options, store=self.store)
        self.assertTrue(changed)
        self.assertEqual(report["status"], "complete", report)
        self.assertEqual([v["bundle_id"] for v in report["items"]], ["org.example.fixture"])

    def test_localizations_are_preferences_not_removal_rules(self):
        self.options["configuration"]["preferred_languages"] = ["en"]
        with mock.patch.object(app_tools, "_run", side_effect=self.fake_run):
            report = app_tools.dispatch("localizations", "inspect", self.options, store=self.store)
        locales = {x["language"]: x for x in report["items"][0]["localizations"]}
        self.assertEqual(locales["en"]["logical_bytes"], 7)
        self.assertEqual(locales["fr"]["preference"], "not-selected")
        self.assertFalse(locales["fr"]["eligible"])

    def test_nested_binary_and_32_bit_main(self):
        main = self.bundle / "Contents" / "MacOS" / "Fixture"
        main.write_bytes(b"\xce\xfa\xed\xfe" + struct.pack("<I", 7) + b"\0" * 20)
        helper = self.bundle / "Contents" / "Frameworks" / "Helper"
        helper.parent.mkdir()
        helper.write_bytes(b"\xcf\xfa\xed\xfe" + struct.pack("<I", 0x01000007) + b"\0" * 20)
        with mock.patch.object(app_tools, "_run", side_effect=self.fake_run):
            report = app_tools.dispatch("architectures", "inspect", self.options, store=self.store)
        self.assertEqual(report["items"][0]["architectures"], ["i386", "x86_64"])
        self.assertEqual(report["items"][0]["compatibility_status"], "unsupported-32-bit")

    def test_universal_header_and_overlapping_slices(self):
        path = self.root / "universal"
        header = b"\xca\xfe\xba\xbe" + struct.pack(">I", 2)
        path.write_bytes(header + struct.pack(">IIIII", 0x01000007, 0, 64, 8, 0) + struct.pack(">IIIII", 0x0100000C, 0, 72, 8, 0) + b"\0" * 32)
        with path.open("rb") as source:
            values = app_tools._macho(source.fileno(), 80)
        self.assertEqual([v["architecture"] for v in values], ["x86_64", "arm64"])
        path.write_bytes(header + struct.pack(">IIIII", 0x01000007, 0, 64, 16, 0) + struct.pack(">IIIII", 0x0100000C, 0, 72, 8, 0) + b"\0" * 32)
        with path.open("rb") as source:
            self.assert_code("invalid_macho", app_tools._macho, source.fileno(), 80)

    def test_bundle_symlink_is_not_read(self):
        secret = self.root / "secret"
        secret.write_bytes(b"not an app resource")
        (self.bundle / "Contents" / "Resources" / "outside").symlink_to(secret)
        value = app_tools._bundle_contents(str(self.bundle))
        self.assertEqual(value["status"], "partial")
        self.assertEqual(value["excluded"], 1)
        self.assertNotIn("outside", [b["relative_path"] for b in value["binaries"]])

    def test_scans_require_explicit_scope(self):
        self.assert_code("scope_required", app_tools.dispatch, "applications", "inspect", {}, store=self.store)

    def test_unsupported_platform_never_runs_commands(self):
        with mock.patch.object(app_tools.platform, "system", return_value="Linux"), mock.patch.object(app_tools, "_run") as run:
            value = app_tools.dispatch("applications", "inspect", self.options, store=self.store)
        self.assertEqual(value["status"], "unavailable")
        run.assert_not_called()

    def test_cancel_bundle_inspection(self):
        result = app_tools._bundle_contents(str(self.bundle), cancel=lambda: True)
        self.assertEqual(result["status"], "cancelled")

    def test_app_leftovers_never_infer_orphanhood(self):
        component = self.root / "components"
        component.mkdir()
        (component / "org.example.fixture.plist").write_text("untrusted")
        (component / "org.example.fixtureSibling.plist").write_text("untrusted")
        (component / "old.dmg").write_bytes(b"installer")
        options = dict(self.options, roots=[str(component)], configuration={"application_roots": [str(self.apps)]})
        value = app_tools.dispatch("app-leftovers", "inspect", options, store=self.store)
        self.assertEqual(len(value["items"]), 3)
        self.assertTrue(all(not x["eligible"] for x in value["items"]))
        by_name = {Path(x["path"]).name: x for x in value["items"]}
        self.assertEqual(by_name["org.example.fixture.plist"]["app_presence"], "observed")
        self.assertEqual(by_name["org.example.fixtureSibling.plist"]["app_presence"], "unknown")

    def test_package_report_never_uses_lexical_version_order(self):
        self.cask["installed"] = "9.0"
        self.cask["version"] = "10.0"
        with mock.patch.object(app_tools, "_brew", side_effect=self.fake_brew):
            value = app_tools.dispatch("app-updates", "inspect", self.options, store=self.store)
        self.assertTrue(value["items"][0]["update_available"])

    def test_hooks_packages_and_shared_artifacts_are_rejected(self):
        for artifact in ({"uninstall": [{"script": "bad"}]}, {"pkg": ["bad.pkg"]}, {"preflight": None}, {"binary": ["bin/fixture"]}):
            with self.subTest(artifact=artifact):
                value = copy.deepcopy(self.cask)
                value["artifacts"].append(artifact)
                self.assert_code("unsupported_cask", app_tools._cask_guard, value, "uninstall")

    def test_protected_agent_cask_cannot_be_changed(self):
        self.cask["token"] = "codex"
        self.assert_code("protected_application", app_tools._cask_guard, self.cask, "uninstall")

    def test_third_party_tap_and_checksumless_update_rejected(self):
        self.cask["tap"] = "someone/tools"
        self.assert_code("unsupported_cask", app_tools._cask_guard, self.cask, "uninstall")
        self.cask["tap"] = "homebrew/cask"
        self.cask["sha256"] = "no_check"
        self.assert_code("unsupported_update", app_tools._cask_guard, self.cask, "upgrade")

    def test_dependency_update_does_not_widen_scope(self):
        self.cask["depends_on"] = {"formula": ["runtime"]}
        self.assert_code("unsupported_update", app_tools._cask_guard, self.cask, "upgrade")

    def test_plan_has_exact_app_and_nonrecoverability(self):
        value = self.plan()
        self.assertEqual(value["payload"]["token"], "fixture")
        self.assertEqual(value["payload"]["operation"], "uninstall")
        self.assertEqual(value["payload"]["bundles"][0]["path"], str(self.bundle))
        self.assertIn("no guaranteed rollback", value["summary"]["recovery"])

    def test_running_app_blocks_plan(self):
        original = self.fake_run
        def run(argv, **kwargs):
            if argv[0] == "/bin/ps":
                return {"returncode": 0, "stdout": str(self.bundle / "Contents" / "MacOS" / "Fixture"), "stderr": ""}
            return original(argv, **kwargs)
        with mock.patch.object(app_tools, "_run", side_effect=run), mock.patch.object(app_tools, "_brew", side_effect=self.fake_brew):
            report = app_tools.dispatch("app-uninstall", "inspect", self.options, store=self.store)
            options = dict(self.options, record_id=report["id"], selected=[report["items"][0]["id"]])
            self.assert_code("application_running", app_tools.dispatch, "app-uninstall", "plan", options, store=self.store)

    def test_installed_receipt_override_blocks_plan(self):
        self.definition.write_text(json.dumps({"artifacts": [{"uninstall": [{"delete": "shared"}]}]}))
        with self.assertRaises(CleanMacError):
            self.plan()

    def test_old_ruby_definition_is_not_executed(self):
        self.definition.unlink()
        self.definition.with_suffix(".rb").write_text("raise 'must not execute'")
        with self.assertRaises((OSError, CleanMacError)):
            self.plan()

    def test_approval_mismatch_never_reaches_provider(self):
        plan = self.plan()
        options = dict(self.options, record_id=plan["id"], approve="wrong")
        with mock.patch.object(app_tools, "_brew") as brew:
            with self.assertRaises(CleanMacError):
                app_tools.dispatch("app-uninstall", "apply", options, store=self.store)
        brew.assert_not_called()

    def test_changed_ownership_rejects_before_mutation(self):
        plan = self.plan()
        self.receipt["uninstall_flight_blocks"] = True
        self.receipt_path.write_text(json.dumps(self.receipt))
        options = dict(self.options, record_id=plan["id"], approve=plan["digest"])
        with mock.patch.object(app_tools, "_run", side_effect=self.fake_run), mock.patch.object(app_tools, "_brew", side_effect=self.fake_brew) as brew:
            with self.assertRaises(CleanMacError):
                app_tools.dispatch("app-uninstall", "apply", options, store=self.store)
        self.assertFalse(any(call.args[1][0] == "uninstall" for call in brew.call_args_list))

    def test_failed_provider_receipt_freezes_retry(self):
        plan = self.plan()
        options = dict(self.options, record_id=plan["id"], approve=plan["digest"])
        calls = []
        def brew(provider, args, **kwargs):
            if args[0] == "uninstall":
                calls.append(args)
                raise CleanMacError("Interrupted", "provider_timeout")
            return self.fake_brew(provider, args, **kwargs)
        with mock.patch.object(app_tools, "_run", side_effect=self.fake_run), mock.patch.object(app_tools, "_brew", side_effect=brew):
            first = app_tools.dispatch("app-uninstall", "apply", options, store=self.store)
            second = app_tools.dispatch("app-uninstall", "apply", options, store=self.store)
        self.assertEqual(first["status"], "recovery_needed")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.list("receipts")[0]["status"], "recovery_needed")

    def test_verified_uninstall_and_replay_execute_once(self):
        plan = self.plan()
        options = dict(self.options, record_id=plan["id"], approve=plan["digest"])
        calls = []
        removed = False
        def brew(provider, args, **kwargs):
            nonlocal removed
            if args[0] == "uninstall":
                calls.append(args)
                self.bundle.rename(self.root / "Fixture.uninstalled")
                removed = True
                return ""
            if removed and args[:2] == ["info", "--json=v2"]:
                return json.dumps({"casks": []})
            return self.fake_brew(provider, args, **kwargs)
        with mock.patch.object(app_tools, "_run", side_effect=self.fake_run), mock.patch.object(app_tools, "_brew", side_effect=brew):
            receipt = app_tools.dispatch("app-uninstall", "apply", options, store=self.store)
            replay = app_tools.dispatch("app-uninstall", "apply", options, store=self.store)
        self.assertEqual(receipt["status"], "completed")
        self.assertTrue(receipt["result"]["verified"])
        self.assertEqual(receipt["id"], replay["id"])
        self.assertEqual(calls, [["uninstall", "--cask", "fixture"]])

    def test_update_checks_signing_identity_after_install(self):
        plan = self.plan("app-updates")
        options = dict(self.options, record_id=plan["id"], approve=plan["digest"])
        upgraded = False
        def brew(provider, args, **kwargs):
            nonlocal upgraded
            if args[0] == "upgrade":
                upgraded = True
                return ""
            if upgraded:
                current = dict(self.cask, installed="2.0")
                return json.dumps({"casks": [current]})
            return self.fake_brew(provider, args, **kwargs)
        def run(argv, **kwargs):
            result = self.fake_run(argv, **kwargs)
            if upgraded and argv[0] == "/usr/bin/codesign":
                result["stderr"] = result["stderr"].replace("TEAM123", "EVIL123")
            return result
        with mock.patch.object(app_tools, "_run", side_effect=run), mock.patch.object(app_tools, "_brew", side_effect=brew):
            receipt = app_tools.dispatch("app-updates", "apply", options, store=self.store)
        self.assertEqual(receipt["error"]["code"], "verification_failed")
        self.assertEqual(self.store.list("receipts")[0]["status"], "recovery_needed")

    def test_verified_update_keeps_vendor_identity(self):
        plan = self.plan("app-updates")
        options = dict(self.options, record_id=plan["id"], approve=plan["digest"])
        upgraded = False
        def brew(provider, args, **kwargs):
            nonlocal upgraded
            if args[0] == "upgrade":
                upgraded = True
                self.assertEqual(args, ["upgrade", "--cask", "fixture"])
                return ""
            if upgraded:
                return json.dumps({"casks": [dict(self.cask, installed="2.0")]})
            return self.fake_brew(provider, args, **kwargs)
        with mock.patch.object(app_tools, "_run", side_effect=self.fake_run), mock.patch.object(app_tools, "_brew", side_effect=brew):
            receipt = app_tools.dispatch("app-updates", "apply", options, store=self.store)
        self.assertEqual(receipt["status"], "completed")
        self.assertTrue(receipt["result"]["verified"])

    def test_bundle_and_metadata_symlink_substitution_blocks_plan(self):
        with mock.patch.object(app_tools, "_run", side_effect=self.fake_run), mock.patch.object(app_tools, "_brew", side_effect=self.fake_brew):
            report = app_tools.dispatch("app-uninstall", "inspect", self.options, store=self.store)
            options = dict(self.options, record_id=report["id"], selected=[report["items"][0]["id"]])
            moved = self.root / "Original.app"
            self.bundle.rename(moved)
            self.bundle.symlink_to(moved)
            with self.assertRaises((OSError, CleanMacError)):
                app_tools.dispatch("app-uninstall", "plan", options, store=self.store)
        self.assertTrue(moved.is_dir())
        self.assertEqual(self.store.list("plans"), [])

    def test_changed_provider_blocks_approved_plan(self):
        plan = self.plan()
        self.provider.write_text("#!/bin/sh\nexit 1\n")
        options = dict(self.options, record_id=plan["id"], approve=plan["digest"])
        with mock.patch.object(app_tools, "_brew") as brew:
            self.assert_code("changed_provider", app_tools.dispatch, "app-uninstall", "apply", options, store=self.store)
        brew.assert_not_called()

    def test_changed_bundle_signature_blocks_plan(self):
        def run(argv, **kwargs):
            result = self.fake_run(argv, **kwargs)
            if argv[0] == "/usr/bin/codesign" and "--verify" in argv:
                result["returncode"] = 1
            return result
        with mock.patch.object(app_tools, "_run", side_effect=run), mock.patch.object(app_tools, "_brew", side_effect=self.fake_brew):
            report = app_tools.dispatch("app-uninstall", "inspect", self.options, store=self.store)
            options = dict(self.options, record_id=report["id"], selected=[report["items"][0]["id"]])
            self.assert_code("signature_unverified", app_tools.dispatch, "app-uninstall", "plan", options, store=self.store)

    def test_provider_environment_cannot_override_cask_scope(self):
        with mock.patch.dict(os.environ, {"HOMEBREW_CASK_OPTS": "--zap", "HOMEBREW_API_DOMAIN": "https://malicious.invalid", "RUBYOPT": "-rbad"}), mock.patch.object(app_tools, "_run", return_value={"returncode": 0, "stdout": "{}", "stderr": ""}) as run:
            app_tools._brew({"path": str(self.provider)}, ["info", "--json=v2"])
        env = run.call_args.kwargs["env"]
        self.assertNotIn("HOMEBREW_CASK_OPTS", env)
        self.assertNotIn("HOMEBREW_API_DOMAIN", env)
        self.assertNotIn("RUBYOPT", env)
        self.assertEqual(env["HOMEBREW_NO_INSTALL_CLEANUP"], "1")

    def test_provider_mode_change_is_rejected(self):
        self.provider.chmod(0o777)
        self.assert_code("unsafe_provider", app_tools._provider, self.options["configuration"])

    def test_provider_output_is_bounded_and_noninteractive(self):
        result = app_tools._run([sys.executable, "-c", "import sys; print(sys.stdin.read()); print('ok')"], timeout=2)
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["stdout"], "\nok\n")
        self.assert_code("provider_timeout", app_tools._run, [sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.02)


if __name__ == "__main__":
    unittest.main()

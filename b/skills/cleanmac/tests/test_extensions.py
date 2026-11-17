"""Registry, CLI and terminal behavior using isolated tool fixtures only."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cleanmac_core import cli, extensions, review
from cleanmac_core.common import CleanMacError
from cleanmac_core.state import StateStore


SPEC = {"name": "fixture", "title": "Fixture tool", "feature_ids": ["CM-054"],
        "actions": ["inspect", "plan", "apply"], "configuration": {"account_id": "Explicit account ID"}}


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cleanmac-extensions-")
        self.base = Path(self.temp.name).resolve()
        self.store = StateStore(self.base / "state")
        self.callback = mock.Mock(return_value={"status": "complete", "items": []})
        self.module = SimpleNamespace(TOOL_SPECS=[copy.deepcopy(SPEC)], dispatch=self.callback)

    def tearDown(self):
        self.temp.cleanup()

    def registry(self):
        return mock.patch.object(extensions, "_registry", return_value=({"fixture": (dict(SPEC), self.module)}, []))

    def test_catalog_only_imports_bundled_metadata_without_invoking_tool(self):
        with mock.patch.object(extensions, "MODULES", ("fixture_tools",)), mock.patch.object(extensions.importlib, "import_module", return_value=self.module) as imported:
            result = extensions.catalog()
        self.assertEqual(result["tools"][0]["dependency_status"], "not-checked")
        self.assertEqual(result["tools"][0]["implementation"], "installed")
        self.callback.assert_not_called()
        imported.assert_called_once_with(".fixture_tools", package="cleanmac_core")

    def test_broken_module_is_reported_without_leaking_exception_text(self):
        with mock.patch.object(extensions, "MODULES", ("fixture_tools",)), mock.patch.object(extensions.importlib, "import_module", side_effect=RuntimeError("bearer-secret")):
            result = extensions.catalog()
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["tools"])
        self.assertNotIn("bearer-secret", json.dumps(result))

    def test_duplicate_name_is_not_arbitrarily_dispatched(self):
        with mock.patch.object(extensions, "MODULES", ("first", "second")), mock.patch.object(extensions.importlib, "import_module", return_value=self.module):
            result = extensions.catalog()
            with self.assertRaises(CleanMacError):
                extensions.dispatch_tool("fixture", "inspect", {}, store=self.store)
        self.assertEqual(result["tools"], [])
        self.callback.assert_not_called()

    def test_invalid_config_rejects_before_dispatch_without_raw_values(self):
        raws = ['{"access_token":"private-secret"}', '{"nested":{"password":"private-secret"}}',
                '{"x":NaN}', '{"x":1,"x":2}', '[1,2]', '"private-secret"', '{bad private-secret}']
        for raw in raws:
            with self.subTest(raw=raw):
                with self.assertRaises(CleanMacError) as raised:
                    extensions.parse_configuration(raw)
                self.assertNotIn("private-secret", str(raised.exception))

    def test_config_accepts_credential_references_and_preserves_opaque_ids(self):
        config = extensions.parse_configuration('{"token_env":"EXAMPLE_TOKEN","keychain_service":"cleanmac-example","account_id":"account-one"}')
        options = extensions.normalize_options(roots=[str(self.base), str(self.base)], selected=["object,a b", "object,a b"], configuration=config)
        self.assertEqual(options["selected"], ["object,a b"])
        self.assertEqual(options["roots"], [str(self.base)])
        self.assertEqual(options["configuration"], config)

    def test_remote_root_ids_are_never_rewritten_as_local_paths(self):
        options = extensions.normalize_options(roots=["root", "remote:folder", "root"])
        self.assertEqual(options["roots"], ["root", "remote:folder"])

    def test_gmail_credential_objects_allow_only_explicit_references(self):
        for reference in ({"source": "environment", "name": "CLEANMAC_GMAIL_TOKEN"},
                          {"source": "keychain", "service": "cleanmac.gmail", "account": "fixture"}):
            self.assertEqual(extensions.validate_configuration({"credential": reference})["credential"], reference)
        for reference in ("private-secret", {"source": "environment", "name": "CLEANMAC_TOKEN", "value": "private-secret"},
                          {"source": "keychain", "service": "cleanmac.gmail", "token": "private-secret"}):
            with self.assertRaises(CleanMacError):
                extensions.validate_configuration({"credential": reference})

    def test_config_rejects_depth_size_and_nonfinite_values(self):
        deep = {}
        for _ in range(15):
            deep = {"child": deep}
        for value in (deep, {"large": "x" * 65536}, {"n": float("inf")}, {"n": 2 ** 100}, []):
            with self.assertRaises(CleanMacError):
                extensions.validate_configuration(value)

    def test_config_file_requires_owner_only_regular_file_and_no_symlinks(self):
        path = self.base / "config.json"
        path.write_text('{"account_id":"fixture"}')
        path.chmod(0o600)
        self.assertEqual(extensions.parse_configuration(path=str(path)), {"account_id": "fixture"})
        link = self.base / "linked.json"
        link.symlink_to(path)
        with self.assertRaises(CleanMacError):
            extensions.parse_configuration(path=str(link))
        path.chmod(0o644)
        with self.assertRaises(CleanMacError):
            extensions.parse_configuration(path=str(path))

    def test_unknown_tool_action_and_unapproved_apply_never_dispatch(self):
        with self.registry():
            for name, action, options in [("missing", "inspect", {}), ("fixture", "erase", {}), ("fixture", "apply", {})]:
                with self.assertRaises(CleanMacError):
                    extensions.dispatch_tool(name, action, options, store=self.store)
        self.callback.assert_not_called()

    def test_cancellation_prevents_tool_dispatch(self):
        with self.registry():
            result = extensions.dispatch_tool("fixture", "inspect", {}, store=self.store, cancel=lambda: True)
        self.assertEqual(result["status"], "cancelled")
        self.callback.assert_not_called()

    def test_other_declared_approval_actions_require_plan_and_digest(self):
        spec = dict(SPEC, actions=["inspect", "restore"], approval_actions=["restore"])
        with mock.patch.object(extensions, "_registry", return_value=({"fixture": (spec, self.module)}, [])), self.assertRaises(CleanMacError) as raised:
            extensions.dispatch_tool("fixture", "restore", {}, store=self.store)
        self.assertEqual(raised.exception.code, "approval_required")
        self.callback.assert_not_called()

    def test_unexpected_tool_exception_is_sanitized(self):
        self.callback.side_effect = RuntimeError("private-secret")
        with self.registry(), self.assertRaises(CleanMacError) as raised:
            extensions.dispatch_tool("fixture", "inspect", {}, store=self.store)
        self.assertEqual(raised.exception.code, "tool_failed")
        self.assertNotIn("private-secret", str(raised.exception))

    def test_cli_dispatches_exact_values_and_global_options(self):
        with self.registry(), mock.patch.object(sys, "platform", "darwin"):
            result = cli.dispatch(["--json", "tool", "fixture", "--action", "plan", "--root", str(self.base),
                                   "--select", "id,with,commas", "--record", "scan-fixture", "--config", '{"account_id":"fixture"}'], store=self.store)
        args, kwargs = self.callback.call_args
        self.assertEqual(args[:2], ("fixture", "plan"))
        self.assertEqual(args[2]["selected"], ["id,with,commas"])
        self.assertEqual(args[2]["record_id"], "scan-fixture")
        self.assertEqual(args[2]["configuration"], {"account_id": "fixture"})
        self.assertIs(kwargs["store"], self.store)
        self.assertEqual(result["tool"], "fixture")

    def test_tools_discovery_never_creates_state_even_on_linux(self):
        unused = self.base / "unused"
        output = io.StringIO()
        with self.registry(), mock.patch.object(sys, "platform", "linux"), contextlib.redirect_stdout(output):
            code = cli.main(["tools", "fixture", "--json", "--data-dir", str(unused)])
        self.assertEqual(code, 0)
        self.assertFalse(unused.exists())
        self.callback.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["tools"][0]["name"], "fixture")

    def test_nonmac_tool_rejects_without_callback(self):
        with self.registry(), mock.patch.object(sys, "platform", "linux"), self.assertRaises(CleanMacError) as raised:
            cli.dispatch(["tool", "fixture"], store=self.store)
        self.assertEqual(raised.exception.code, "unsupported_platform")
        self.callback.assert_not_called()

    def test_parser_errors_never_echo_accidental_secret_arguments(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(["tool", "fixture", "--json", "--access-token", "private-secret"])
        self.assertEqual(code, 2)
        self.assertNotIn("private-secret", output.getvalue())


class ToolWizardTests(unittest.TestCase):
    def run_wizard(self, answers, inspection=None):
        answers = iter(answers)
        calls, output = [], []

        def invoke(argv):
            calls.append(argv)
            if argv[0] == "tools":
                return {"tools": [SPEC]}
            if argv[0] == "tool":
                action = argv[argv.index("--action") + 1]
                if action == "inspect":
                    if inspection is not None:
                        return inspection
                    return {"id": "scan-fixture", "tool": "fixture", "status": "complete", "items": [{"id": "object,a", "title": "Example"}]}
                if action == "plan":
                    return {"id": "plan-action-fixture", "record_type": "action_plan", "tool": "fixture", "action": "cloud-trash", "digest": "exact-digest", "summary": "Trash exact remote object", "payload": {"selected": ["object,a"]}}
                if action == "apply":
                    return {"id": "receipt-action-fixture", "record_type": "action_receipt", "status": "completed", "tool": "fixture", "action": "cloud-trash"}
            raise AssertionError(argv)

        def ask(_):
            try:
                return next(answers)
            except StopIteration:
                raise EOFError

        result = review.interactive_wizard(invoke, input_fn=ask, output=output.append, is_tty=True)
        return result, calls, output

    def test_inspect_and_cancel_plan_never_apply(self):
        _, calls, _ = self.run_wizard(["tools", "fixture", "inspect", "select 1", "plan", "apply", "back", "done"])
        self.assertFalse(any("apply" in c for c in calls))

    def test_apply_uses_displayed_plan_digest_and_exact_remote_id(self):
        _, calls, output = self.run_wizard(["tools", "fixture", "inspect", "select 1", "plan", "apply", "apply", "done"])
        plan = next(c for c in calls if "plan" in c)
        self.assertEqual(plan[plan.index("--select") + 1], "object,a")
        applied = next(c for c in calls if "apply" in c)
        self.assertEqual(applied[applied.index("--record") + 1], "plan-action-fixture")
        self.assertEqual(applied[applied.index("--approve") + 1], "exact-digest")
        self.assertTrue(any('"cloud-trash"' in line and "exact-digest" in line for line in output))

    def test_action_receipt_does_not_claim_local_trash_recovery(self):
        rendered = review.format_result({"record_type": "action_receipt", "id": "receipt-action-fixture", "tool": "fixture", "action": "dns-reset"})
        self.assertIn("dns-reset", rendered)
        self.assertNotIn("Trash-retained bytes", rendered)

    def test_changing_selection_after_plan_keeps_original_inspection_record(self):
        _, calls, _ = self.run_wizard(["tools", "fixture", "inspect", "select 1", "plan", "select 1", "plan", "done"])
        plans = [call for call in calls if "plan" in call]
        self.assertEqual(len(plans), 2)
        self.assertTrue(all(call[call.index("--record") + 1] == "scan-fixture" for call in plans))

    def test_missing_dependency_result_preserves_unscanned_coverage_and_guidance(self):
        missing = {"tool": "fixture", "status": "dependency-needed", "coverage": "unscanned",
                   "error": {"code": "auth_required", "message": "Use the accounts tool."}, "items": []}
        _, calls, output = self.run_wizard(["tools", "fixture", "inspect", "done"], inspection=missing)
        self.assertTrue(any('"unscanned"' in line and "Use the accounts tool" in line for line in output))
        self.assertFalse(any("apply" in call for call in calls))


if __name__ == "__main__":
    unittest.main()

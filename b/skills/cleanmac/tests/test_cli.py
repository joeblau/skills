"""Behavioral CLI tests over disposable files; no native Trash operations."""
from __future__ import annotations

import contextlib
import io
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from cleanmac_core.cli import _TerminalProgress, dispatch, main
from cleanmac_core.common import CleanMacError
from cleanmac_core.state import StateStore


class CLIWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cleanmac-cli-")
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "downloads"
        self.root.mkdir()
        self.store = StateStore(self.base / "state")
        self.platform = mock.patch.object(sys, "platform", "darwin")
        self.platform.start()

    def tearDown(self):
        self.platform.stop()
        self.temp.cleanup()

    def scan(self, *extra):
        return dispatch(["scan", "--root", str(self.root), "--areas", "files,duplicates,large,storage", "--min-size", "1", *extra], store=self.store)

    def test_noninteractive_menu_never_scans_or_creates_state(self):
        unused = self.base / "not-created"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--json", "--data-dir", str(unused)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "menu")
        self.assertFalse(unused.exists())

    def test_scan_is_read_only_and_explicit_selection_builds_plan(self):
        first = self.root / "a.txt"
        second = self.root / "b.txt"
        first.write_bytes(b"same content")
        second.write_bytes(b"same content")
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        scan = self.scan()
        self.assertEqual(scan["status"], "complete")
        self.assertEqual(scan["summary"]["selected_count"], 0)
        self.assertEqual(len(scan["duplicate_groups"]), 1)
        by_name = {Path(item["path"]).name: item["id"] for item in scan["items"]}
        plan = dispatch(["plan", scan["id"], "--select", by_name["b.txt"], "--keep", by_name["a.txt"]], store=self.store)
        self.assertEqual([item["id"] for item in plan["items"]], [by_name["b.txt"]])
        self.assertEqual([item["id"] for item in plan["keepers"]], [by_name["a.txt"]])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        self.assertEqual(self.store.get("plans", plan["id"]), plan)

    def test_review_pagination_and_resume_preserve_exact_ids(self):
        for n in range(5):
            (self.root / f"{n}.txt").write_text(f"content {n}")
        scan = self.scan()
        page = dispatch(["review", scan["id"], "--offset", "1", "--limit", "2", "--search", ".txt"], store=self.store)
        self.assertEqual(len(page["items"]), 2)
        self.assertEqual(page["pagination"]["total"], 5)
        self.assertEqual(page["pagination"]["next_offset"], 3)
        resumed = dispatch(["history", "--id", scan["id"]], store=self.store)
        self.assertEqual(resumed["items"], scan["items"])
        self.assertEqual(resumed["summary"]["selected_count"], 0)

    def test_ignore_applies_to_future_scan_and_not_prefix_neighbor(self):
        excluded = self.root / "keep"
        neighbor = self.root / "keeper"
        excluded.mkdir()
        neighbor.mkdir()
        (excluded / "private.txt").write_text("retain")
        (neighbor / "visible.txt").write_text("inspect")
        initial = self.scan()
        result = dispatch(["ignore", "add", "--path", str(excluded)], store=self.store)
        self.assertIn(str(excluded), result["ignore_paths"])
        later = self.scan()
        paths = {item["path"] for item in later["items"]}
        self.assertNotIn(str(excluded / "private.txt"), paths)
        self.assertIn(str(neighbor / "visible.txt"), paths)
        self.assertEqual(self.store.get("scans", initial["id"]), initial)
        dispatch(["ignore", "remove", "--path", str(excluded)], store=self.store)
        self.assertEqual(dispatch(["ignore", "list"], store=self.store)["ignore_paths"], [])

    def test_invalid_scope_and_limits_never_run_inventory(self):
        for args in (["scan", "--root", str(self.root), "--areas", "malware"],
                     ["scan", "--root", str(self.root), "--max-items", "0"],
                     ["scan", "--root", str(self.root), "--duplicate-min-size", "0"]):
            with mock.patch("cleanmac_core.inventory.scan_roots") as scanner:
                with self.assertRaises(CleanMacError):
                    dispatch(args, store=self.store)
                scanner.assert_not_called()

    def test_malformed_cli_returns_structured_error_without_traceback(self):
        output = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(["scan", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "invalid_arguments")
        self.assertEqual(errors.getvalue(), "")

    def test_global_options_work_before_and_after_subcommand(self):
        (self.root / "x.txt").write_text("x")
        for argv in (["--json", "--data-dir", str(self.store.root), "scan", "--root", str(self.root)],
                     ["scan", "--root", str(self.root), "--json", "--data-dir", str(self.store.root)]):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(argv), 0)
            self.assertEqual(json.loads(output.getvalue())["summary"]["file_count"], 1)

    def test_forget_removes_only_saved_record(self):
        target = self.root / "retained.txt"
        target.write_text("preserve")
        scan = self.scan()
        result = dispatch(["forget", scan["id"]], store=self.store)
        self.assertEqual(result["status"], "forgotten")
        self.assertEqual(target.read_text(), "preserve")
        self.assertEqual(self.store.list("scans"), [])

    def test_restore_requires_exact_receipt_approval_before_executor(self):
        with self.assertRaises(CleanMacError) as raised:
            dispatch(["restore", "receipt-example", "--approve", "other"], store=self.store)
        self.assertEqual(raised.exception.code, "approval_mismatch")

    def test_platform_rejection_never_invokes_inventory(self):
        with mock.patch.object(sys, "platform", "linux"), mock.patch("cleanmac_core.inventory.scan_roots") as scanner:
            with self.assertRaises(CleanMacError) as raised:
                self.scan()
        self.assertEqual(raised.exception.code, "unsupported_platform")
        scanner.assert_not_called()

    def test_cancelled_scan_is_saved_for_review(self):
        (self.root / "item.txt").write_text("untouched")
        result = dispatch(["scan", "--root", str(self.root)], store=self.store, cancel=lambda: True)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.store.get("scans", result["id"])["status"], "cancelled")
        self.assertTrue((self.root / "item.txt").exists())

    def test_launcher_non_tty_returns_menu(self):
        result = subprocess.run([str(SCRIPTS / "cleanmac"), "--json"], stdin=subprocess.DEVNULL,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "menu")

    def test_external_native_build_path_reaches_capability_probe(self):
        helper = self.base / "private-helper"
        with mock.patch.dict(os.environ, {"CLEANMAC_NATIVE_PATH": str(helper)}), mock.patch("cleanmac_core.permissions.capabilities", return_value={"status": "complete"}) as capabilities:
            dispatch(["capabilities"])
        capabilities.assert_called_once_with([], native_path=str(helper))

    def test_hardlink_storage_totals_count_one_physical_identity(self):
        original = self.root / "original.txt"
        original.write_bytes(b"same allocation")
        os.link(original, self.root / "alias.txt")
        result = self.scan()
        self.assertEqual(result["summary"]["file_count"], 2)
        self.assertEqual(result["summary"]["unique_file_count"], 1)
        self.assertEqual(result["summary"]["logical_bytes"], len(b"same allocation"))

    def test_progress_is_throttled_uses_real_counts_and_omits_paths(self):
        output = io.StringIO()
        with mock.patch("time.monotonic", side_effect=[0, 0, 0.2, 1.2, 1.3]):
            reporter = _TerminalProgress(output)
            reporter({"phase": "inventory", "counts": {"files": 2, "entries_seen": 4}, "path": "/private/secret"})
            reporter({"phase": "inventory", "counts": {"files": 3, "entries_seen": 5}})
            reporter({"phase": "inventory", "counts": {"files": 9, "entries_seen": 12}})
            reporter({"phase": "duplicates_hashing", "hashed": 4, "path": "secret\x1b[31m"})
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("2 files", lines[0])
        self.assertIn("9 files", lines[1])
        self.assertIn("4 hashed", lines[2])
        self.assertNotIn("secret", output.getvalue())
        self.assertNotIn("%", output.getvalue())

    def test_machine_json_has_no_progress_even_with_terminal_stderr(self):
        class TTYBuffer(io.StringIO):
            def isatty(self):
                return True
        output, errors = io.StringIO(), TTYBuffer()
        observed = []
        def fake_dispatch(argv, **kwargs):
            observed.append(kwargs.get("progress"))
            return {"status": "complete", "actual_count": 7}
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), mock.patch("cleanmac_core.cli.dispatch", side_effect=fake_dispatch):
            self.assertEqual(main(["capabilities", "--json"]), 0)
        self.assertEqual(observed, [None])
        self.assertEqual(json.loads(output.getvalue())["actual_count"], 7)
        self.assertEqual(errors.getvalue(), "")

    def test_human_terminal_progress_is_on_stderr_only(self):
        class TTYBuffer(io.StringIO):
            def isatty(self):
                return True
        output, errors = io.StringIO(), TTYBuffer()
        def fake_dispatch(argv, **kwargs):
            kwargs["progress"]({"phase": "inventory", "counts": {"files": 3}})
            return {"status": "complete"}
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), mock.patch("cleanmac_core.cli.dispatch", side_effect=fake_dispatch):
            self.assertEqual(main(["capabilities"]), 0)
        self.assertIn("3 files", errors.getvalue())
        self.assertNotIn("3 files", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["status"], "complete")

    def test_cancelled_inventory_skips_rule_hashing(self):
        with mock.patch("cleanmac_core.rules.classify_items") as classifier:
            result = dispatch(["scan", "--root", str(self.root)], store=self.store, cancel=lambda: True)
        self.assertEqual(result["status"], "cancelled")
        classifier.assert_not_called()

    def test_interrupt_during_scan_reaches_cooperative_cancel(self):
        output = io.StringIO()
        def fake_dispatch(argv, **kwargs):
            signal.raise_signal(signal.SIGINT)
            self.assertTrue(kwargs["cancel"]())
            return {"status": "cancelled", "id": "scan-stopped"}
        with contextlib.redirect_stdout(output), mock.patch("cleanmac_core.cli.dispatch", side_effect=fake_dispatch):
            self.assertEqual(main(["--json", "scan", "--root", str(self.root)]), 130)
        self.assertEqual(json.loads(output.getvalue())["status"], "cancelled")

    def test_interrupt_at_wizard_prompt_exits_instead_of_waiting_for_input(self):
        errors = io.StringIO()
        def fake_wizard(*args, **kwargs):
            signal.raise_signal(signal.SIGINT)
            self.fail("An input interrupt should stop the wizard")
        with contextlib.redirect_stderr(errors), mock.patch("sys.stdin.isatty", return_value=True), mock.patch("cleanmac_core.review.interactive_wizard", side_effect=fake_wizard):
            self.assertEqual(main([]), 130)
        self.assertEqual(json.loads(errors.getvalue())["status"], "cancelled")

    def test_read_only_adapters_receive_same_cancel_callback(self):
        callback = lambda: False
        for command, function in (("apps", "scan_applications"), ("xcode", "scan_xcode"), ("overview", "system_overview")):
            with self.subTest(command=command), mock.patch("cleanmac_core.adapters." + function, return_value={"status": "complete"}) as adapter:
                dispatch([command], cancel=callback)
                self.assertIs(adapter.call_args.kwargs["cancel"], callback)


if __name__ == "__main__":
    unittest.main()

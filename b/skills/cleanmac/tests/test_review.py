from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cleanmac_core.common import CleanMacError
from cleanmac_core.review import interactive_wizard, render_plan, render_review


def item(number, path=None, **kwargs):
    return dict(id=f"item-{number}", path=path or f"/fixture/Downloads/file-{number}",
                kind="file", logical_size=100, allocated_size=4096,
                protected=False, is_placeholder=False, nlink=1, **kwargs)


class FakeCommands:
    """Pure command boundary fake: no file reads, permission changes, or Trash."""

    def __init__(self, items=None):
        self.scan = {"id": "scan-test", "roots": ["/fixture/Downloads"], "status": "complete",
                     "items": items or [item(1), item(2)], "findings": [], "duplicate_groups": []}
        self.calls = []
        self.capability_results = [{"status": "available", "access": []}]
        self.plan_error = None
        self.receipt = {"id": "receipt-test", "status": "complete", "operations": [
            {"status": "trashed", "original_path": "/fixture/Downloads/file-2"}]}

    def __call__(self, argv):
        self.calls.append(list(argv))
        command = argv[0]
        if command == "capabilities":
            result = self.capability_results[0]
            if len(self.capability_results) > 1:
                self.capability_results.pop(0)
            return copy.deepcopy(result)
        if command in ("scan", "review"):
            return copy.deepcopy(self.scan)
        if command == "plan":
            if self.plan_error:
                raise self.plan_error
            selected = set(argv[argv.index("--select") + 1].split(","))
            kept = set(argv[argv.index("--keep") + 1].split(",")) if "--keep" in argv else set()
            return {"id": "plan-test", "digest": "exact-approved-digest", "action": "trash",
                    "items": [copy.deepcopy(x) for x in self.scan["items"] if x["id"] in selected],
                    "keepers": [copy.deepcopy(x) for x in self.scan["items"] if x["id"] in kept]}
        if command == "apply":
            return copy.deepcopy(self.receipt)
        if command == "history":
            if "--id" in argv:
                return copy.deepcopy(self.receipt)
            return {"scans": [{"id": "scan-test", "status": "complete"}], "plans": [],
                    "receipts": [{"id": "receipt-test", "status": "complete"}]}
        if command == "restore":
            return {"id": "receipt-restored", "status": "restored", "operations": self.receipt["operations"]}
        if command == "ignore":
            return {"status": "complete", "ignore_paths": [argv[-1]]}
        if command == "permissions":
            return {"status": "available", "settings_action": {"status": "opened"}}
        raise AssertionError(argv)


class ReviewTests(unittest.TestCase):
    def run_wizard(self, answers, commands=None):
        commands = commands or FakeCommands()
        answers = iter(answers)
        output = []
        prompts = []

        def ask(prompt):
            prompts.append(prompt)
            try:
                return next(answers)
            except StopIteration:
                raise EOFError

        result = interactive_wizard(commands, input_fn=ask, output=output.append,
                                    is_tty=True, home="/fixture")
        return result, commands, output, prompts

    def test_non_tty_never_prompts_or_dispatches(self):
        def fail(*args):
            self.fail("non-TTY menu performed an action")
        result = interactive_wizard(fail, input_fn=fail, output=fail, is_tty=False)
        self.assertEqual(result["status"], "menu")
        self.assertTrue(result["requires_input"])

    def test_control_and_bidi_filenames_are_escaped(self):
        scan = FakeCommands([item(1, "/fixture/evil\n\x1b[2J\u202erun`command`")]).scan
        rendered = render_review(scan)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\u202e", rendered)
        self.assertIn("\\u001b", rendered)
        self.assertIn("\\u000a", rendered)
        self.assertIn("item-1", rendered)

    def test_zero_default_selection_cannot_create_plan(self):
        result, commands, output, _ = self.run_wizard(["quick", "scan", "plan", "done"])
        self.assertEqual(result["selected_ids"], [])
        self.assertFalse(any(c[0] in ("plan", "apply") for c in commands.calls))
        self.assertTrue(any("Nothing is selected" in s for s in output))

    def test_cancel_preview_does_not_apply(self):
        _, commands, output, _ = self.run_wizard(["quick", "scan", "select 2", "plan", "cancel", "done"])
        self.assertFalse(any(c[0] == "apply" for c in commands.calls))
        self.assertTrue(any("/fixture/Downloads/file-2" in s and "Approval digest" in s for s in output))

    def test_single_approval_binds_exact_plan_and_digest(self):
        result, commands, output, prompts = self.run_wizard(["quick", "scan", "select 2", "plan", "trash", "done"])
        self.assertEqual(result["id"], "receipt-test")
        self.assertEqual([c for c in commands.calls if c[0] == "apply"],
                         [["apply", "plan-test", "--approve", "exact-approved-digest"]])
        self.assertEqual(sum("Move exactly" in p for p in prompts), 1)
        self.assertTrue(any("Trash retains" in line for line in output))

    def test_keep_changes_selected_plan(self):
        commands = FakeCommands()
        commands.scan["duplicate_groups"] = [{"id": "dup-1", "item_ids": ["item-1", "item-2"], "keeper_id": "item-1"}]
        _, commands, _, _ = self.run_wizard(["quick", "scan", "select 1,2", "keep 1", "plan", "cancel", "done"], commands)
        plan = next(c for c in commands.calls if c[0] == "plan")
        self.assertEqual(plan[plan.index("--select") + 1], "item-2")
        self.assertEqual(plan[plan.index("--keep") + 1], "item-1")

    def test_duplicate_suggestion_does_not_select_or_apply(self):
        commands = FakeCommands()
        commands.scan["duplicate_groups"] = [{"id": "dup-1", "item_ids": ["item-1", "item-2"], "keeper_id": "item-1"}]
        result, commands, output, _ = self.run_wizard(["quick", "scan", "done"], commands)
        self.assertEqual(result["selected_ids"], [])
        self.assertTrue(any("suggested keeper" in line for line in output))

    def test_pagination_requires_visible_row(self):
        commands = FakeCommands([item(n) for n in range(1, 26)])
        result, _, output, _ = self.run_wizard(["quick", "scan", "select 25", "next", "select 25", "done"], commands)
        self.assertEqual(result["selected_ids"], ["item-25"])
        self.assertTrue(any("not on the displayed page" in line for line in output))

    def test_existing_review_page_keeps_absolute_row_numbers(self):
        scan = FakeCommands([item(21)]).scan
        scan["pagination"] = {"offset": 20, "total": 50}
        self.assertIn("Items 21–21 of 50", render_review(scan))

    def test_ignore_is_explicit_and_removes_selection(self):
        result, commands, _, _ = self.run_wizard(["quick", "scan", "select 1", "ignore 1", "ignore", "done"])
        self.assertEqual(result["selected_ids"], [])
        self.assertEqual([c for c in commands.calls if c[0] == "ignore"],
                         [["ignore", "add", "--path", "/fixture/Downloads/file-1"]])

    def test_permission_denial_skips_without_scanning_or_opening_settings(self):
        commands = FakeCommands()
        commands.capability_results = [{"status": "available", "access": [
            {"path": "/fixture/Downloads", "status": "permission-needed", "message": "Denied by privacy policy."}]}]
        _, commands, output, _ = self.run_wizard(["quick", "scan", "continue", "done"], commands)
        self.assertFalse(any(c[0] in ("scan", "permissions", "apply") for c in commands.calls))
        self.assertTrue(any("No scan ran" in line for line in output))

    def test_settings_requires_choice_and_retry_uses_same_scope(self):
        commands = FakeCommands()
        denied = {"status": "available", "access": [{"path": "/fixture/Downloads", "status": "permission-needed",
                  "message": "Permission required", "suggested_settings_pane": "full-disk-access"}]}
        commands.capability_results = [denied, {"status": "available", "access": []}]
        _, commands, _, _ = self.run_wizard(["quick", "scan", "settings", "retry", "done"], commands)
        self.assertEqual([c for c in commands.calls if c[0] == "permissions"],
                         [["permissions", "--open-settings", "--pane", "full-disk-access"]])
        scans = [c for c in commands.calls if c[0] == "scan"]
        self.assertEqual(len(scans), 1)
        self.assertEqual(scans[0][scans[0].index("--root") + 1], "/fixture/Downloads")

    def test_opening_settings_does_not_reprobe_without_retry(self):
        commands = FakeCommands()
        commands.capability_results = [{"status": "available", "access": [
            {"path": "/fixture/Downloads", "status": "permission-needed", "message": "Denied."}]}]
        _, commands, _, _ = self.run_wizard(["quick", "scan", "settings", "continue", "done"], commands)
        self.assertEqual(sum(c[0] == "capabilities" for c in commands.calls), 1)
        self.assertFalse(any(c[0] == "scan" for c in commands.calls))

    def test_ordinary_keep_is_not_sent_as_duplicate_keeper(self):
        _, commands, _, _ = self.run_wizard(["quick", "scan", "keep 1", "select 2", "plan", "cancel", "done"])
        plan = next(c for c in commands.calls if c[0] == "plan")
        self.assertNotIn("--keep", plan)

    def test_keeper_from_unselected_duplicate_group_is_not_sent(self):
        commands = FakeCommands([item(n) for n in range(1, 5)])
        commands.scan["duplicate_groups"] = [
            {"id": "dup-1", "item_ids": ["item-1", "item-2"], "keeper_id": "item-1"},
            {"id": "dup-2", "item_ids": ["item-3", "item-4"], "keeper_id": "item-3"},
        ]
        _, commands, _, _ = self.run_wizard(["quick", "scan", "keep 1", "select 4", "plan", "cancel", "done"], commands)
        self.assertNotIn("--keep", next(c for c in commands.calls if c[0] == "plan"))

    def test_storage_category_preserves_all_inventory(self):
        scan = FakeCommands().scan
        self.assertIn("file-1", render_review(scan, category="storage"))
        self.assertIn("file-2", render_review(scan, category="storage"))

    def test_history_restoration_fetches_exact_targets_before_approval(self):
        _, commands, output, _ = self.run_wizard(["history", "restore receipt-test", "restore"])
        detail = commands.calls.index(["history", "--id", "receipt-test"])
        mutation = commands.calls.index(["restore", "receipt-test", "--approve", "receipt-test"])
        self.assertLess(detail, mutation)
        self.assertTrue(any("/fixture/Downloads/file-2" in line for line in output))

    def test_eof_does_not_create_or_apply_plan(self):
        result, commands, _, _ = self.run_wizard(["quick", "scan", "select 2"])
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(any(c[0] in ("plan", "apply") for c in commands.calls))

    def test_failed_plan_does_not_request_approval(self):
        commands = FakeCommands()
        commands.plan_error = CleanMacError("The file changed; rescan.")
        _, commands, output, prompts = self.run_wizard(["quick", "scan", "select 2", "plan", "done"], commands)
        self.assertFalse(any("Move exactly" in prompt for prompt in prompts))
        self.assertFalse(any(c[0] == "apply" for c in commands.calls))
        self.assertTrue(any("rescan" in line for line in output))

    def test_keep_folder_deselects_descendants_but_not_prefix_collision(self):
        directory = item(1, "/fixture/a")
        directory["kind"] = "directory"
        commands = FakeCommands([directory, item(2, "/fixture/a/file"), item(3, "/fixture/ab/file")])
        result, _, _, _ = self.run_wizard(["quick", "scan", "select 2,3", "keep 1", "done"], commands)
        self.assertEqual(result["selected_ids"], ["item-3"])

    def test_plan_preview_shows_exact_keeper_and_storage_caveat(self):
        text = render_plan({"id": "p", "digest": "d", "items": [item(2)], "keepers": [item(1)]})
        self.assertIn("file-2", text)
        self.assertIn("file-1", text)
        self.assertIn("Retain these duplicate keepers", text)
        self.assertIn("does not empty Trash", text)


if __name__ == "__main__":
    unittest.main()

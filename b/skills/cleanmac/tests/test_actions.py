"""Provider/system action boundaries, using only disposable fixture state."""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cleanmac_core.actions import (
    ActionNotStarted, checkpoint_action, create_action_plan, execute_action_plan,
    validate_action_plan, validate_action_receipt,
)
from cleanmac_core.common import CleanMacError, digest
from cleanmac_core.state import StateStore


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temporary.name).resolve() / "state")
        self.plan = create_action_plan(
            self.store, "fixture-cloud", "trash", {"account": "fixture", "item_id": "chosen"},
            "Move this exact fixture item to its provider Trash.",
            {"revision": "before", "account": "fixture"},
        )

    def tearDown(self):
        self.temporary.cleanup()

    def run_action(self, validate=lambda plan: None, execute=lambda plan: {"item_id": "chosen"}, **kwargs):
        return execute_action_plan(
            self.store, self.plan["id"], self.plan["digest"], "fixture-cloud", "trash",
            validate, execute, **kwargs,
        )

    def assert_error(self, code, function, *args, **kwargs):
        with self.assertRaises(CleanMacError) as context:
            function(*args, **kwargs)
        self.assertEqual(context.exception.code, code)

    def test_plan_is_concrete_private_and_does_not_mutate_input(self):
        payload = {"ids": ["one"]}
        plan = create_action_plan(self.store, "fixture", "update", payload, {"action": "Update one item"})
        payload["ids"].append("two")
        self.assertEqual(plan["payload"], {"ids": ["one"]})
        self.assertEqual(validate_action_plan(plan, tool="fixture", action="update"), plan)
        self.assertEqual(self.store.list("receipts"), [])
        self.assertEqual((self.store.root / "plans" / (plan["id"] + ".json")).stat().st_mode & 0o777, 0o600)

    def test_approval_mismatch_never_creates_receipt_or_calls_adapter(self):
        adapter = mock.Mock()
        self.assert_error("approval_required", execute_action_plan, self.store, self.plan["id"],
                          "0" * 64, "fixture-cloud", "trash", adapter, adapter)
        adapter.assert_not_called()
        self.assertEqual(self.store.list("receipts"), [])

    def test_cross_tool_or_action_is_rejected(self):
        for tool, action in [("other", "trash"), ("fixture-cloud", "delete")]:
            with self.subTest(tool=tool, action=action):
                self.assert_error("action_scope_mismatch", execute_action_plan, self.store,
                                  self.plan["id"], self.plan["digest"], tool, action, lambda p: None, lambda p: {})

    def test_changed_plan_and_unknown_keys_are_rejected(self):
        changed = copy.deepcopy(self.plan)
        changed["payload"]["item_id"] = "another"
        self.store.put("plans", changed)
        self.assert_error("action_plan_changed", self.run_action)
        changed["digest"] = digest({k: v for k, v in changed.items() if k != "digest"})
        self.store.put("plans", changed)
        self.assert_error("approval_required", self.run_action)
        changed["command"] = ["arbitrary"]
        changed["digest"] = digest({k: v for k, v in changed.items() if k != "digest"})
        self.assert_error("invalid_action_plan", validate_action_plan, changed, tool="fixture-cloud", action="trash")

    def test_invalid_json_inputs_and_missing_summary_rejected(self):
        for payload in ({1: "key"}, {"value": float("inf")}, {"value": (1, 2)}, {"value": object()}):
            with self.subTest(payload=payload):
                self.assert_error("invalid_action_plan", create_action_plan, self.store,
                                  "fixture", "update", payload, "A concrete summary")
        self.assert_error("invalid_action_plan", create_action_plan, self.store, "fixture", "update", {}, "")
        self.assert_error("invalid_action_plan", create_action_plan, self.store, "fixture", "update",
                          {"oversize": "a" * (1024 * 1024)}, "Summary")

    def test_validation_failure_has_no_execution_and_can_retry(self):
        execute = mock.Mock(return_value={"verified": True})
        validate = mock.Mock(side_effect=CleanMacError("The remote revision changed.", code="revision_changed"))
        self.assert_error("revision_changed", self.run_action, validate, execute)
        receipt = self.store.list("receipts")[0]
        self.assertEqual(receipt["status"], "failed")
        self.assertFalse(receipt["execution_started"])
        self.assertEqual(receipt["attempts"], 0)
        execute.assert_not_called()
        receipt = self.run_action(execute=execute)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(execute.call_count, 1)

    def test_false_validator_and_missing_callbacks_are_rejected(self):
        execute = mock.Mock()
        self.assert_error("action_precondition_failed", self.run_action, lambda p: False, execute)
        execute.assert_not_called()
        self.assert_error("invalid_action_adapter", self.run_action, None, execute)

    def test_validator_cannot_change_execution_payload(self):
        seen = []

        def validate(plan):
            plan["payload"]["item_id"] = "unapproved"

        def execute(plan):
            seen.append(plan["payload"]["item_id"])
            return {}

        self.run_action(validate, execute)
        self.assertEqual(seen, ["chosen"])

    def test_success_is_durable_and_replay_does_not_call_either_callback(self):
        receipt = self.run_action(execute=lambda p: {"status": "completed", "provider_revision": "after"})
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["attempts"], 1)
        self.assertEqual(receipt["plan_snapshot"], self.plan)
        self.assertEqual(validate_action_receipt(receipt, self.plan), receipt)
        called = mock.Mock(side_effect=AssertionError("replay must not call adapter"))
        self.assertEqual(self.run_action(called, called), receipt)
        self.assert_error("recovery_retention", self.store.delete, "receipts", receipt["id"])

    def test_callback_exception_marks_ambiguous_and_sanitizes_provider_text(self):
        execute = mock.Mock(side_effect=TimeoutError("secret token=never-persist"))
        receipt = self.run_action(execute=execute)
        self.assertEqual(receipt["status"], "recovery_needed")
        self.assertNotIn("never-persist", str(receipt))
        self.assertEqual(receipt["error"]["exception_type"], "TimeoutError")
        replay = self.run_action(execute=execute)
        self.assertEqual(replay, receipt)
        self.assertEqual(execute.call_count, 1)

    def test_non_success_result_is_never_reported_completed(self):
        for result in (None, {"status": "partial"}, {"status": "failed"}, {"status": "unavailable"}, {"secret": object()}):
            with self.subTest(result=result):
                self.plan = create_action_plan(self.store, "fixture-cloud", "trash", {}, "One fixture action")
                receipt = self.run_action(execute=lambda p: result)
                self.assertEqual(receipt["status"], "recovery_needed")

    def test_cancellation_before_or_after_validation_never_starts_mutation(self):
        calls = mock.Mock()
        receipt = self.run_action(calls, calls, cancel=lambda: True)
        self.assertEqual(receipt["status"], "not_started")
        calls.assert_not_called()
        checks = iter([False, True])
        validator = mock.Mock(return_value=None)
        receipt = self.run_action(validator, calls, cancel=lambda: next(checks))
        self.assertEqual(validator.call_count, 1)
        self.assertEqual(receipt["status"], "not_started")
        calls.assert_not_called()

    def test_keyboard_interrupt_after_start_is_journaled_and_not_retried(self):
        calls = mock.Mock(side_effect=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.run_action(execute=calls)
        receipt = self.run_action(execute=calls)
        self.assertEqual(receipt["status"], "recovery_needed")
        self.assertEqual(calls.call_count, 1)

    def test_running_receipt_from_interrupted_process_is_frozen(self):
        receipt = self.run_action()
        receipt.update(status="running", phase="execution", result=None)
        receipt["integrity_digest"] = digest({k: v for k, v in receipt.items() if k != "integrity_digest"})
        self.store.put("receipts", receipt)
        calls = mock.Mock()
        recovery = self.run_action(calls, calls)
        self.assertEqual(recovery["status"], "recovery_needed")
        self.assertEqual(recovery["error"]["code"], "action_interrupted")
        calls.assert_not_called()

    def test_explicit_proven_no_effect_failure_allows_safe_retry(self):
        fail = mock.Mock(side_effect=ActionNotStarted("Transport rejected before dispatch."))
        receipt = self.run_action(execute=fail)
        self.assertEqual(receipt["status"], "failed")
        self.assertFalse(receipt["execution_started"])
        self.assertIsNone(receipt["restore_handle"])
        completed = self.run_action()
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["attempts"], 2)

    def test_checkpoint_is_durable_before_effect_and_retained_after_timeout(self):
        captured = []

        def execute(plan):
            checkpoint_action({"phase": "before_mutation", "item_id": "chosen"})
            captured.append(self.store.list("receipts")[0])
            checkpoint_action({"phase": "mutation_dispatched", "item_id": "chosen", "provider_receipt": "fixture-receipt"})
            raise TimeoutError("Unknown provider response")

        receipt = self.run_action(execute=execute)
        self.assertEqual(captured[0]["status"], "running")
        self.assertEqual(captured[0]["checkpoint"]["phase"], "before_mutation")
        self.assertEqual(receipt["status"], "recovery_needed")
        self.assertEqual(receipt["checkpoint"]["provider_receipt"], "fixture-receipt")
        self.assertEqual(validate_action_receipt(receipt, self.plan), receipt)
        self.assert_error("invalid_action_checkpoint", checkpoint_action, {})

    def test_checkpoint_cannot_be_written_during_validation(self):
        self.assert_error("invalid_action_checkpoint", self.run_action, lambda p: checkpoint_action({}))
        self.assertFalse(self.store.list("receipts")[0]["execution_started"])

    def test_receipt_substitution_and_forged_snapshot_are_rejected(self):
        receipt = self.run_action()
        for field, value in (("plan_digest", "f" * 64), ("tool", "other"),
                             ("plan_snapshot", dict(self.plan, payload={"item_id": "other"})),
                             ("status", []), ("execution_started", False)):
            with self.subTest(field=field):
                forged = copy.deepcopy(receipt)
                forged[field] = value
                forged["integrity_digest"] = digest({k: v for k, v in forged.items() if k != "integrity_digest"})
                self.assert_error("invalid_action_receipt", validate_action_receipt, forged, self.plan)
        changed = copy.deepcopy(receipt)
        changed["result"] = {"provider_receipt": "substituted"}
        self.store.put("receipts", changed)
        self.assert_error("invalid_action_receipt", self.run_action)

    def test_storage_failure_before_running_prevents_action(self):
        original = self.store.put

        def put(kind, value):
            if kind == "receipts" and value.get("status") == "running":
                raise CleanMacError("Fixture disk full", code="state_io_error")
            return original(kind, value)

        execute = mock.Mock()
        with mock.patch.object(self.store, "put", side_effect=put):
            self.assert_error("state_io_error", self.run_action, execute=execute)
        execute.assert_not_called()
        self.assertEqual(self.store.list("receipts")[0]["status"], "not_started")

    def test_failed_completion_write_leaves_ambiguous_nonrepeatable_receipt(self):
        original = self.store.put

        def put(kind, value):
            if kind == "receipts" and value.get("status") == "completed":
                raise CleanMacError("Fixture disk full", code="state_io_error")
            return original(kind, value)

        execute = mock.Mock(return_value={"verified": True})
        with mock.patch.object(self.store, "put", side_effect=put):
            receipt = self.run_action(execute=execute)
        self.assertEqual(receipt["status"], "recovery_needed")
        self.assertEqual(self.run_action(execute=execute), receipt)
        self.assertEqual(execute.call_count, 1)

    def test_two_concurrent_requests_execute_once(self):
        entered = threading.Event()
        release = threading.Event()
        effects = []

        def execute(plan):
            entered.set()
            self.assertTrue(release.wait(3))
            effects.append(plan["payload"]["item_id"])
            return {"verified": True}

        with ThreadPoolExecutor(max_workers=2) as threads:
            first = threads.submit(self.run_action, execute=execute)
            self.assertTrue(entered.wait(3))
            second = threads.submit(self.run_action, execute=execute)
            release.set()
            one, two = first.result(timeout=5), second.result(timeout=5)
        self.assertEqual(effects, ["chosen"])
        self.assertEqual(one, two)

    def test_progress_failure_does_not_duplicate_or_hide_effect(self):
        progress = mock.Mock(side_effect=OSError("fixture disconnected output"))
        receipt = self.run_action(progress=progress)
        self.assertEqual(receipt["status"], "completed")


if __name__ == "__main__":
    unittest.main()

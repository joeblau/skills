"""Deletion cannot orphan credentials, recovery, or an action starting concurrently."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import copy
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cleanmac_core import executor, oauth
from cleanmac_core.actions import create_action_plan, execute_action_plan
from cleanmac_core.common import CleanMacError, digest
from cleanmac_core.inventory import scan_roots
from cleanmac_core.plans import make_plan
from cleanmac_core.state import StateStore


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cleanmac-retention-")
        self.base = Path(self.temp.name).resolve()
        self.store = StateStore(self.base / "state")
        self.connection_id = "oauth-connection-" + "a" * 32
        self.session_id = "oauth-session-" + "b" * 32

    def tearDown(self):
        self.temp.cleanup()

    def save_connection(self, status):
        return self.store.put("scans", {"id": self.connection_id, "record_type": "oauth_connection", "tool": "accounts", "status": status})

    def save_session(self, status):
        return self.store.put("scans", {"id": self.session_id, "record_type": "oauth_session", "tool": "accounts", "status": status})

    def action_plan(self, payload=None):
        return create_action_plan(self.store, "fixture", "change", payload or {"selected": "exact"}, "Only this fixture action")

    def file_plan(self):
        root = self.base / "files"
        root.mkdir(exist_ok=True)
        path = root / "untouched.txt"
        path.write_bytes(b"fixture only")
        scan = scan_roots([str(root)])
        plan = make_plan(scan, [next(item["id"] for item in scan["items"] if item["path"] == str(path))])
        self.store.put("plans", plan)
        return path, plan

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(CleanMacError) as raised:
            function(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_active_connections_cannot_be_forgotten_or_bypass_disposal_flag(self):
        with mock.patch.object(oauth, "Keychain") as keychain:
            for status in ("connecting", "connected", "refreshing", "disconnecting", "reauthentication_required", "unknown"):
                with self.subTest(status=status):
                    original = self.save_connection(status)
                    self.assert_code("oauth_account_retention", self.store.delete, "scans", self.connection_id, discard_recovery=True)
                    self.assertEqual(self.store.get("scans", self.connection_id), original)
        keychain.assert_not_called()

    def test_disconnected_connection_requires_verified_keychain_absence(self):
        original = self.save_connection("disconnected")
        with mock.patch.object(oauth, "Keychain") as keychain:
            keychain.return_value.contains.return_value = True
            self.assert_code("oauth_account_retention", self.store.delete, "scans", self.connection_id)
            self.assertEqual(self.store.get("scans", self.connection_id), original)
            keychain.return_value.contains.return_value = False
            self.store.delete("scans", self.connection_id)
            keychain.return_value.contains.assert_called_with(self.connection_id)
        self.assert_code("state_not_found", self.store.get, "scans", self.connection_id)

    def test_keychain_denial_preserves_disconnected_metadata(self):
        self.save_connection("disconnected")
        with mock.patch.object(oauth, "Keychain") as keychain:
            keychain.return_value.contains.side_effect = CleanMacError("Handle account access yourself.", code="oauth_keychain_denied")
            self.assert_code("oauth_keychain_denied", self.store.delete, "scans", self.connection_id)
        self.assertEqual(self.store.get("scans", self.connection_id)["status"], "disconnected")

    def test_pending_consent_requires_cancel_or_expiry(self):
        for status in ("starting", "waiting_for_consent", "exchanging"):
            self.save_session(status)
            self.assert_code("oauth_session_retention", self.store.delete, "scans", self.session_id)

    def test_terminal_consent_requires_worker_to_have_stopped(self):
        self.save_session("cancelled")
        with self.store.lock("worker-" + "b" * 32):
            self.assert_code("oauth_session_retention", self.store.delete, "scans", self.session_id)
        self.store.delete("scans", self.session_id)
        for status in ("expired", "failed", "denied", "connected"):
            self.save_session(status)
            self.store.delete("scans", self.session_id)

    def test_ordinary_scan_and_unexecuted_plan_can_be_forgotten(self):
        self.store.put("scans", {"id": "scan-fixture", "items": []})
        self.store.delete("scans", "scan-fixture")
        plan = self.action_plan()
        self.store.delete("plans", plan["id"])
        self.assertEqual(self.store.list("scans"), [])
        self.assertEqual(self.store.list("plans"), [])

    def test_cancelled_unexecuted_action_plan_and_receipt_can_be_forgotten(self):
        plan = self.action_plan()
        receipt = execute_action_plan(self.store, plan["id"], plan["digest"], "fixture", "change",
                                      lambda _: self.fail("Cancelled action validated"), lambda _: self.fail("Cancelled action executed"), cancel=lambda: True)
        self.assertFalse(receipt["execution_started"])
        self.store.delete("plans", plan["id"])
        self.store.delete("receipts", receipt["id"])
        self.assertEqual(self.store.list("plans"), [])
        self.assertEqual(self.store.list("receipts"), [])

    def test_started_action_plan_and_audit_receipt_are_retained(self):
        plan = self.action_plan()
        receipt = execute_action_plan(self.store, plan["id"], plan["digest"], "fixture", "change", lambda _: None, lambda _: {"verified": True})
        self.assert_code("plan_retention", self.store.delete, "plans", plan["id"])
        self.assert_code("recovery_retention", self.store.delete, "receipts", receipt["id"], discard_recovery=True)

    def test_embedded_file_plan_referenced_by_started_action_is_retained(self):
        _, inner = self.file_plan()
        outer = self.action_plan({"file_plan": inner})
        execute_action_plan(self.store, outer["id"], outer["digest"], "fixture", "change", lambda _: None, lambda _: {"verified": True})
        self.assert_code("plan_retention", self.store.delete, "plans", inner["id"])

    def test_not_started_receipt_cannot_race_transition_to_execution(self):
        plan = self.action_plan()
        validating, proceed, deleting = threading.Event(), threading.Event(), threading.Event()
        mutation_count = []
        deletion_store = StateStore(self.store.root)
        original_lock = deletion_store.lock

        @contextmanager
        def observed_lock(name, **kwargs):
            if name.startswith("action-"):
                deleting.set()
            with original_lock(name, **kwargs):
                yield

        def validate(_):
            validating.set()
            if not proceed.wait(5):
                raise AssertionError("test did not release validation")

        def execute(_):
            mutation_count.append(True)
            return {"verified": True}

        with ThreadPoolExecutor(max_workers=2) as pool, mock.patch.object(deletion_store, "lock", observed_lock):
            applying = pool.submit(execute_action_plan, self.store, plan["id"], plan["digest"], "fixture", "change", validate, execute)
            self.assertTrue(validating.wait(3))
            removal = pool.submit(deletion_store.delete, "plans", plan["id"])
            try:
                self.assertTrue(deleting.wait(3))
                self.assertFalse(removal.done())
            finally:
                proceed.set()
            receipt = applying.result(timeout=5)
            with self.assertRaises(CleanMacError) as raised:
                removal.result(timeout=5)
        self.assertEqual(raised.exception.code, "plan_retention")
        self.assertTrue(receipt["execution_started"])
        self.assertEqual(len(mutation_count), 1)

    def test_forgotten_core_plan_cannot_run_from_loaded_memory(self):
        path, plan = self.file_plan()
        loaded = self.store.get("plans", plan["id"])
        handshake, proceed = threading.Event(), threading.Event()

        def native_gate(_):
            handshake.set()
            if not proceed.wait(5):
                raise AssertionError("test did not release native preflight")
            return Path(sys.executable)

        with mock.patch.object(executor, "_native_executable", side_effect=native_gate), mock.patch.object(executor, "_rename_function"), mock.patch.object(executor, "_preflight_recovery") as preflight, mock.patch.object(executor, "_native_trash") as trash:
            with ThreadPoolExecutor(max_workers=1) as pool:
                applying = pool.submit(executor.apply_plan, loaded, approval_digest=loaded["digest"], store=self.store)
                try:
                    self.assertTrue(handshake.wait(3))
                    self.store.delete("plans", plan["id"])
                finally:
                    proceed.set()
                with self.assertRaises(CleanMacError) as raised:
                    applying.result(timeout=5)
        self.assertEqual(raised.exception.code, "plan_not_saved")
        preflight.assert_not_called()
        trash.assert_not_called()
        self.assertEqual(path.read_bytes(), b"fixture only")
        self.assertEqual(self.store.list("receipts"), [])

    def test_changed_persisted_core_plan_rejects_old_loaded_copy(self):
        path, plan = self.file_plan()
        changed = copy.deepcopy(plan)
        changed["created_at"] = "2026-11-13T00:00:00+00:00"
        changed["digest"] = digest({key: value for key, value in changed.items() if key != "digest"})
        self.store.put("plans", changed)
        with mock.patch.object(executor, "_native_executable", return_value=Path(sys.executable)), mock.patch.object(executor, "_rename_function"), mock.patch.object(executor, "_native_trash") as trash:
            self.assert_code("plan_changed", executor.apply_plan, plan, approval_digest=plan["digest"], store=self.store)
        trash.assert_not_called()
        self.assertTrue(path.exists())

    def test_cancelled_core_apply_holds_mutation_lock_until_no_effects_verified(self):
        path, plan = self.file_plan()
        preflighting, proceed, cancelled, deleting = (threading.Event() for _ in range(4))
        deletion_store = StateStore(self.store.root)
        original_lock = deletion_store.lock

        @contextmanager
        def observed_lock(name, **kwargs):
            if name == "mutation":
                deleting.set()
            with original_lock(name, **kwargs):
                yield

        def preflight(*_):
            preflighting.set()
            if not proceed.wait(5):
                raise AssertionError("test did not release preflight")

        with mock.patch.object(executor, "_native_executable", return_value=Path(sys.executable)), mock.patch.object(executor, "_rename_function"), mock.patch.object(executor, "_preflight_recovery", side_effect=preflight), mock.patch.object(executor, "_native_trash") as trash, mock.patch.object(deletion_store, "lock", observed_lock):
            with ThreadPoolExecutor(max_workers=2) as pool:
                applying = pool.submit(executor.apply_plan, plan, approval_digest=plan["digest"], store=self.store, cancel=cancelled.is_set)
                self.assertTrue(preflighting.wait(3))
                removal = pool.submit(deletion_store.delete, "plans", plan["id"])
                try:
                    self.assertTrue(deleting.wait(3))
                    self.assertFalse(removal.done())
                finally:
                    cancelled.set()
                    proceed.set()
                receipt = applying.result(timeout=5)
                removal.result(timeout=5)
        self.assertEqual(receipt["status"], "cancelled")
        self.assertTrue(all(operation["status"] == "not_started" for operation in receipt["operations"]))
        trash.assert_not_called()
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()

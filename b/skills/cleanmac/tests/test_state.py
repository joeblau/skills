"""State storage invariants, exercised only in disposable private directories."""

from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cleanmac_core.common import CleanMacError
from cleanmac_core.state import StateStore


def _locked_writer(root, identifier, result):
    try:
        store = StateStore(root)
        with store.lock("shared", timeout=0.1):
            store.put("scans", {"id": identifier})
        result.put("written")
    except CleanMacError as error:
        result.put(error.code)


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        # macOS's system /var alias is not a user-controlled state symlink.
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "state"
        self.store = StateStore(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def assert_error(self, code, function, *args, **kwargs):
        with self.assertRaises(CleanMacError) as raised:
            function(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_create_and_round_trip_with_private_permissions(self):
        source = {"id": "scan-a", "created_at": "2026-11-13T10:00:00Z", "items": ["a\nb", "猫"]}
        saved = self.store.put("scans", source)
        self.assertEqual(saved["schema_version"], 1)
        self.assertNotIn("schema_version", source)
        self.assertEqual(self.store.get("scans", "scan-a"), saved)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.root / "scans" / "scan-a.json").stat().st_mode), 0o600)
        saved["items"].append("changed")
        self.assertEqual(len(self.store.get("scans", "scan-a")["items"]), 2)
        self.assertEqual(list((self.root / "scans").glob(".pending-*")), [])

    def test_atomic_failure_preserves_original(self):
        self.store.put("plans", {"id": "plan-a", "value": "old"})
        with mock.patch("cleanmac_core.state.os.replace", side_effect=OSError(5, "fixture failure")):
            self.assert_error("state_io_error", self.store.put, "plans", {"id": "plan-a", "value": "new"})
        self.assertEqual(self.store.get("plans", "plan-a")["value"], "old")
        self.assertEqual(list((self.root / "plans").glob(".pending-*")), [])

    def test_list_is_deterministic_and_delete_only_requested_record(self):
        self.store.put("scans", {"id": "scan-old", "created_at": "2026-11-12T10:00:00Z"})
        self.store.put("scans", {"id": "scan-new", "created_at": "2026-11-13T10:00:00Z"})
        self.store.put("plans", {"id": "plan-other"})
        self.assertEqual([record["id"] for record in self.store.list("scans")], ["scan-new", "scan-old"])
        self.store.delete("scans", "scan-old")
        self.assert_error("state_not_found", self.store.get, "scans", "scan-old")
        self.assertEqual(self.store.get("plans", "plan-other")["id"], "plan-other")

    def test_traversal_and_invalid_records_are_rejected(self):
        for identifier in ["../outside", "/absolute", ".", "", "a/b", "a\\b", "line\nbreak", "a" * 129, None]:
            with self.subTest(identifier=identifier):
                self.assert_error("state_invalid_record", self.store.put, "scans", {"id": identifier})
        for kind in ["../../tmp", "users", [], None]:
            with self.subTest(kind=kind):
                self.assert_error("state_invalid_record", self.store.list, kind)
        for value in [float("nan"), float("inf"), {"not", "JSON"}]:
            self.assert_error("state_invalid_record", self.store.put, "scans", {"id": "scan-invalid", "value": value})
        self.assert_error("state_invalid_record", self.store.put, "scans", {"id": "scan-invalid", "schema_version": True})
        self.assertEqual(self.store.list("scans"), [])

    def test_symlink_root_and_ancestor_rejected_without_touching_target(self):
        outside = self.base / "outside"
        outside.mkdir(mode=0o700)
        alias = self.base / "alias"
        alias.symlink_to(outside, target_is_directory=True)
        self.assert_error("state_unsafe_path", StateStore, alias)
        self.assert_error("state_unsafe_path", StateStore, alias / "nested")
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlink_subdirectory_and_record_rejected(self):
        outside = self.base / "outside"
        outside.mkdir(mode=0o700)
        (self.root / "scans").rmdir()
        (self.root / "scans").symlink_to(outside, target_is_directory=True)
        self.assert_error("state_unsafe_path", self.store.put, "scans", {"id": "scan-a"})
        self.assertEqual(list(outside.iterdir()), [])
        self.store.put("plans", {"id": "plan-a"})
        real = self.root / "plans" / "plan-a.json"
        alias = self.root / "plans" / "plan-alias.json"
        alias.symlink_to(real)
        self.assert_error("state_unsafe_path", self.store.get, "plans", "plan-alias")
        self.assert_error("state_unsafe_path", self.store.put, "plans", {"id": "plan-alias"})
        self.assertEqual(self.store.get("plans", "plan-a")["id"], "plan-a")

    def test_hardlinked_or_public_record_rejected(self):
        self.store.put("scans", {"id": "scan-a"})
        source = self.root / "scans" / "scan-a.json"
        linked = self.base / "linked.json"
        os.link(source, linked)
        self.assert_error("state_unsafe_path", self.store.get, "scans", "scan-a")
        self.assert_error("state_unsafe_path", self.store.put, "scans", {"id": "scan-a"})
        linked.unlink()
        source.chmod(0o644)
        self.assert_error("state_unsafe_path", self.store.get, "scans", "scan-a")

    def test_nonregular_record_is_rejected_without_blocking(self):
        fifo = self.root / "scans" / "scan-fifo.json"
        os.mkfifo(fifo, 0o600)
        self.assert_error("state_unsafe_path", self.store.get, "scans", "scan-fifo")

    def test_existing_public_root_is_not_silently_repaired(self):
        public = self.base / "public"
        public.mkdir(mode=0o755)
        self.assert_error("state_unsafe_path", StateStore, public)
        self.assertEqual(stat.S_IMODE(public.stat().st_mode), 0o755)
        self.assertEqual(list(public.iterdir()), [])

    def test_replaced_root_is_detected(self):
        moved = self.base / "moved"
        self.root.rename(moved)
        self.root.mkdir(mode=0o700)
        self.assert_error("state_unsafe_path", self.store.put, "scans", {"id": "scan-a"})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_corrupt_and_mismatched_records_are_preserved(self):
        path = self.root / "scans" / "scan-corrupt.json"
        path.write_text("{partial")
        path.chmod(0o600)
        self.assert_error("state_corrupt", self.store.get, "scans", "scan-corrupt")
        self.assertEqual(path.read_text(), "{partial")
        path.write_text(json.dumps({"id": "scan-wrong", "schema_version": 1}))
        self.assert_error("state_corrupt", self.store.get, "scans", "scan-corrupt")
        path.write_text(json.dumps({"id": "scan-corrupt", "schema_version": 2}))
        self.assert_error("state_corrupt", self.store.get, "scans", "scan-corrupt")

    def test_settings_are_private_and_atomically_replaced(self):
        self.assertEqual(self.store.get_settings(), {"schema_version": 1})
        value = {"ignored_paths": ["/fixture/cache"], "favorites": ["duplicates"]}
        saved = self.store.save_settings(value)
        self.assertEqual(self.store.get_settings(), saved)
        self.assertEqual(stat.S_IMODE((self.root / "settings.json").stat().st_mode), 0o600)
        self.assertNotIn("schema_version", value)
        self.store.save_settings({"ignored_paths": []})
        self.assertEqual(self.store.get_settings()["ignored_paths"], [])

    def test_recovery_receipt_retention_is_explicit(self):
        record = {"id": "receipt-a", "items": [{"status": "trashed", "trash_path": "/fixture/Trash/file"}]}
        self.store.put("receipts", record)
        self.assert_error("recovery_retention", self.store.delete, "receipts", "receipt-a")
        self.assertEqual(self.store.get("receipts", "receipt-a")["items"], record["items"])
        self.assert_error("state_invalid_record", self.store.delete, "receipts", "receipt-a", discard_recovery="yes")
        self.store.delete("receipts", "receipt-a", discard_recovery=True)
        self.assert_error("state_not_found", self.store.get, "receipts", "receipt-a")

    def test_process_lock_is_bounded_and_released(self):
        context = multiprocessing.get_context("spawn")
        result = context.Queue()
        with self.store.lock("shared"):
            process = context.Process(target=_locked_writer, args=(str(self.root), "scan-blocked", result))
            process.start()
            process.join(timeout=10)
            self.assertFalse(process.is_alive())
            self.assertEqual(result.get(timeout=2), "state_busy")
        process = context.Process(target=_locked_writer, args=(str(self.root), "scan-written", result))
        process.start()
        process.join(timeout=10)
        self.assertFalse(process.is_alive())
        self.assertEqual(result.get(timeout=2), "written")
        self.assertEqual(self.store.get("scans", "scan-written")["id"], "scan-written")
        result.close()
        result.join_thread()

    def test_lock_exception_releases_lock_and_bad_timeouts_reject(self):
        with self.assertRaisesRegex(RuntimeError, "fixture"):
            with self.store.lock("shared"):
                raise RuntimeError("fixture")
        with self.store.lock("shared", timeout=0):
            pass
        for timeout in [float("nan"), float("inf"), -1, 61]:
            with self.subTest(timeout=timeout):
                self.assert_error("state_invalid_record", self.store.lock("shared", timeout=timeout).__enter__)

    def test_environment_precedence(self):
        explicit = self.base / "explicit"
        override = self.base / "override"
        xdg = self.base / "xdg"
        with mock.patch.dict(os.environ, {"CLEANMAC_DATA_DIR": str(override), "XDG_DATA_HOME": str(xdg)}):
            self.assertEqual(StateStore(explicit).root, explicit)
            self.assertEqual(StateStore().root, override)
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(xdg)}):
            with mock.patch.dict(os.environ):
                os.environ.pop("CLEANMAC_DATA_DIR", None)
                self.assertEqual(StateStore().root, xdg / "b-skills" / "cleanmac")


if __name__ == "__main__":
    unittest.main()

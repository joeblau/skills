from __future__ import annotations

import copy
import datetime as dt
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cleanmac_core import cleanup_tools as c, executor, inventory, rules
from cleanmac_core.common import CleanMacError, digest
from cleanmac_core.state import StateStore


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cleanmac-cleanup-tools-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir(mode=0o700)
        self.store = StateStore(self.base / "state")
        self.home_patch = patch("pathlib.Path.home", return_value=self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.process_patch = patch.object(rules, "detect_active_apps", return_value=[])
        self.process_mock = self.process_patch.start()
        self.addCleanup(self.process_patch.stop)

    def file(self, name, data=b"fixture"):
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def inspect(self, name, root=None, **configuration):
        return c.dispatch(name, "inspect", {"roots": [str(root)] if root else [], "configuration": configuration}, store=self.store)

    def report(self, name="CrashApp", age=45, timestamp=None, metadata=None):
        when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=age)
        header = {"name": name, "timestamp": timestamp if timestamp is not None else when.isoformat(), "bug_type": "309", "incident_id": "62ae2b96-08f3-42c4-a3a2-25b63ab367a4"}
        if metadata:
            header.update(metadata)
        raw = json.dumps(header).encode() + b"\n" + json.dumps({"procName": name, "pid": 123, "captureTime": when.isoformat()}).encode()
        path = self.file(f"Library/Logs/DiagnosticReports/{name}.ips", raw)
        os.utime(path, (when.timestamp(), when.timestamp()))
        return path


class CleanupToolsTests(_Fixture):
    def test_every_filesystem_tool_requires_scope_without_process_probe(self):
        for name in ("user-caches", "system-caches", "user-logs", "system-logs", "preferences", "home-archives", "device-backups", "ai-data"):
            with self.subTest(name=name), self.assertRaises(CleanMacError) as ctx:
                self.inspect(name)
            self.assertEqual(ctx.exception.code, "scope_required")
        self.process_mock.assert_not_called()

    def test_normal_files_and_report_only_caches_do_not_probe_processes(self):
        plain = self.file("Downloads/hello.txt")
        cache = self.file("Library/Caches/Unknown/cache.data")
        for path in (plain, cache):
            scan = inventory.scan_roots([str(path)])
            rules.classify_items(scan["items"])
        self.process_mock.assert_not_called()

    def test_old_report_plan_is_bound_to_content_and_requires_selection(self):
        path = self.report()
        report = self.inspect("user-logs", path.parent)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(len(report["eligible_ids"]), 1)
        self.assertTrue(all(i["selected"] is False for i in report["items"]))
        plan = c.dispatch("user-logs", "plan", {"record_id": report["id"], "selected": report["eligible_ids"]}, store=self.store)
        self.assertEqual(plan["action"], "trash")
        self.assertIn("content_sha256", plan["items"][0])
        self.assertIn("rule_content_sha512", plan["items"][0])
        self.assertTrue(path.exists())

    def test_new_active_and_unknown_logs_are_preserved(self):
        old = self.report()
        recent = self.report("Recent", age=5)
        unknown = self.file("Library/Logs/DiagnosticReports/live.log", b"active log")
        self.process_mock.return_value = ["CrashApp"]
        report = self.inspect("user-logs", old.parent)
        self.assertFalse(report["eligible_ids"])
        self.assertTrue(all(path.exists() for path in (old, recent, unknown)))

    def test_crash_report_apply_and_restore_use_existing_recovery_receipt(self):
        path = self.report()
        original = path.read_bytes()
        scan = self.inspect("user-logs", path.parent)
        plan = c.dispatch("user-logs", "plan", {"record_id": scan["id"], "selected": scan["eligible_ids"]}, store=self.store)
        fixture_trash = self.base / "fixture-trash"
        fixture_trash.mkdir(mode=0o700)
        def move(native, stage_path, receipt_path):
            destination = fixture_trash / stage_path.name
            os.rename(stage_path, destination)
            return {"status": "trashed", "original_path": str(stage_path), "trash_path": str(destination)}
        with patch.object(executor, "_native_executable", return_value=self.base / "not-executed"), patch.object(executor, "_preflight_recovery", return_value=str(fixture_trash)), patch.object(executor, "_native_trash", side_effect=move):
            receipt = c.dispatch("user-logs", "apply", {"record_id": plan["id"], "approve": plan["digest"]}, store=self.store)
            self.assertEqual(receipt["operations"][0]["status"], "trashed")
            self.assertFalse(path.exists())
            restored = executor.restore_receipt(receipt, store=self.store)
        self.assertEqual(restored["operations"][0]["status"], "restored")
        self.assertEqual(path.read_bytes(), original)

    def test_report_without_timestamp_or_wrong_bug_type_is_not_eligible(self):
        for metadata in ({"timestamp": None}, {"timestamp": "2020-01-01"}, {"bug_type": "288"}, {"name": "Other"}):
            with self.subTest(metadata=metadata):
                path = self.report(metadata=metadata)
                report = self.inspect("user-logs", path.parent)
                self.assertEqual(report["eligible_ids"], [])

    def test_report_retention_can_be_stricter_and_uses_timezone(self):
        path = self.report(age=45)
        report = self.inspect("user-logs", path.parent, retention_days=90)
        self.assertFalse(report["eligible_ids"])
        self.assertTrue(report["retention_cutoff_utc"].endswith("+00:00"))
        from cleanmac_core.plans import make_plan
        with self.assertRaises(CleanMacError):
            make_plan(report, [report["items"][0]["id"]])
        with self.assertRaises(CleanMacError):
            self.inspect("user-logs", path.parent, cutoff="2020-01-01")
        with self.assertRaises(CleanMacError):
            self.inspect("user-logs", path.parent, retention_days=0)

    def test_changed_report_fails_old_plan(self):
        path = self.report()
        scan = self.inspect("user-logs", path.parent)
        path.write_bytes(b"replacement")
        with self.assertRaises(CleanMacError):
            c.dispatch("user-logs", "plan", {"record_id": scan["id"], "selected": scan["eligible_ids"]}, store=self.store)

    def test_report_read_does_not_expose_sensitive_body(self):
        path = self.report("PrivateProgram")
        raw = path.read_bytes()
        path.write_bytes(raw.replace(b'"pid": 123', b'"pid": 123, "secret": "sensitive-stack-content"'))
        before = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=45)
        os.utime(path, (before.timestamp(), before.timestamp()))
        report = self.inspect("user-logs", path.parent)
        self.assertNotIn("sensitive-stack-content", json.dumps(report))
        self.assertEqual(len(report["eligible_ids"]), 1)

    def test_system_diagnostics_never_enter_plan(self):
        path = self.report()
        report = self.inspect("system-logs", path.parent)
        self.assertEqual(report["eligible_ids"], [])
        with self.assertRaises(CleanMacError):
            c.dispatch("system-logs", "plan", {"record_id": report["id"], "selected": [report["items"][0]["id"]]}, store=self.store)

    def test_symlink_root_does_not_read_reports(self):
        path = self.report()
        link = self.home / "linked"
        link.symlink_to(path.parent, target_is_directory=True)
        report = self.inspect("user-logs", link)
        self.assertFalse(report["eligible_ids"])
        self.assertNotEqual(report["status"], "complete")

    def test_preferences_classify_binary_xml_and_malformed_without_reset(self):
        good_binary = self.file("Library/Preferences/com.example.binary.plist", plistlib.dumps({"ok": True}, fmt=plistlib.FMT_BINARY))
        self.file("Library/Preferences/com.example.xml.plist", plistlib.dumps({"ok": True}))
        bad = self.file("Library/Preferences/com.example.broken.plist", b"<?xml version='1.0'?><plist><dict>")
        protected = self.file("Library/Preferences/com.apple.security.plist", b"broken protected")
        originals = {p: p.read_bytes() for p in good_binary.parent.iterdir()}
        report = self.inspect("preferences", good_binary.parent)
        by_name = {Path(i["path"]).name: i for i in report["items"]}
        self.assertEqual(by_name[good_binary.name]["validation"], "valid")
        self.assertEqual(by_name[bad.name]["validation"], "malformed")
        self.assertEqual(by_name[protected.name]["validation"], "protected")
        self.assertEqual(originals, {p: p.read_bytes() for p in originals})
        self.assertEqual(report["eligible_ids"], [])

    def test_archive_review_never_mounts_or_reads_contents(self):
        path = self.file("Deleted Users/currentuser.dmg", b"do not read")
        self.file("Deleted Users/olduser.sparseimage")
        with patch.object(c, "_account_homes", return_value={"currentuser": str(self.home)}), patch.object(c, "_read_file_at", side_effect=AssertionError("content read")):
            report = self.inspect("home-archives", path.parent)
        self.assertEqual(len(report["items"]), 2)
        current = next(i for i in report["items"] if i["path"] == str(path))
        self.assertEqual(current["account_status"], "matching-account")
        self.assertFalse(any(i["eligible"] for i in report["items"]))

    def test_unknown_account_state_is_not_deleted_account_claim(self):
        path = self.file("Deleted Users/olduser.dmg")
        with patch.object(c, "_account_homes", return_value=None):
            report = self.inspect("home-archives", path.parent)
        self.assertEqual(report["items"][0]["account_status"], "unverified")

    def backup(self, folder, when, *, state="finished", encrypted=False, device="device-one"):
        root = self.home / "MobileSync" / "Backup" / folder
        root.mkdir(parents=True, exist_ok=True)
        for name, payload in {
            "Info.plist": {"Target Identifier": device, "Device Name": "Fixture phone", "Last Backup Date": when},
            "Manifest.plist": {"IsEncrypted": encrypted},
            "Status.plist": {"SnapshotState": state},
        }.items():
            (root / name).write_bytes(plistlib.dumps(payload))
        (root / "payload.bin").write_bytes(b"encrypted contents never read")
        return root

    def test_backups_latest_and_incomplete_stay_protected(self):
        old = self.backup("older", dt.datetime(2020, 1, 1), encrypted=True)
        latest = self.backup("latest", dt.datetime(2021, 1, 1))
        self.backup("inprogress", dt.datetime(2022, 1, 1), state="copying")
        report = self.inspect("device-backups", old.parent)
        by_name = {Path(i["path"]).name: i for i in report["items"]}
        self.assertTrue(by_name[latest.name]["latest_known"])
        self.assertTrue(by_name[old.name]["encrypted"])
        self.assertFalse(by_name[old.name]["latest_known"])
        self.assertEqual(by_name["inprogress"]["state"], "incomplete-or-unknown")
        self.assertTrue(all(i["protected"] and not i["eligible"] for i in report["items"]))
        self.assertNotIn("encrypted contents", json.dumps(report))

    def test_backup_symlink_metadata_is_not_followed(self):
        backup = self.backup("one", dt.datetime(2020, 1, 1))
        metadata = backup / "Info.plist"
        outside = self.file("outside.plist", metadata.read_bytes())
        metadata.unlink()
        metadata.symlink_to(outside)
        report = self.inspect("device-backups", backup.parent)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["items"][0]["state"], "unverified")

    def test_ai_sessions_and_credentials_are_preserved(self):
        session = self.file(".codex/sessions/current.jsonl", b"agent session")
        auth = self.file(".codex/auth.json", b"secret")
        report = self.inspect("ai-data", self.home / ".codex")
        self.assertEqual(report["eligible_ids"], [])
        self.assertTrue(session.exists() and auth.exists())
        self.assertNotIn("secret", json.dumps(report))

    def test_cancellation_and_denied_scope_are_explicit(self):
        path = self.file("Deleted Users/old.dmg")
        report = c.dispatch("home-archives", "inspect", {"roots": [str(path.parent)]}, store=self.store, cancel=lambda: True)
        self.assertEqual(report["status"], "cancelled")
        with patch("cleanmac_core.permissions._open_directory", side_effect=PermissionError(1, "denied")):
            report = self.inspect("device-backups", path.parent)
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["errors"][0]["coverage"], "unscanned")


class ModelOperationTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.models = [{"name": "fixture:latest", "digest": "a" * 64, "size": 1234}, {"name": "other:latest", "digest": "b" * 64, "size": 2345}]
        self.loaded = []
        self.calls = []

        def api(endpoint, path, method="GET", payload=None):
            self.calls.append((endpoint, path, method, payload))
            if path == "/api/version":
                return {"version": "0.12.0"}
            if path == "/api/tags":
                return {"models": copy.deepcopy(self.models)}
            if path == "/api/ps":
                return {"models": copy.deepcopy(self.loaded)}
            if path == "/api/delete" and method == "DELETE":
                self.models[:] = [m for m in self.models if m["name"] != payload["model"]]
                return {}
            raise AssertionError((path, method, payload))
        self.api_patch = patch.object(c, "_http_json", side_effect=api)
        self.api_mock = self.api_patch.start()
        self.addCleanup(self.api_patch.stop)

    def plan(self):
        scan = self.inspect("models")
        item = next(i for i in scan["items"] if i["name"] == "fixture:latest")
        return c.dispatch("models", "plan", {"record_id": scan["id"], "selected": [item["id"]]}, store=self.store)

    def apply(self, plan, approve=None):
        return c.dispatch("models", "apply", {"record_id": plan["id"], "approve": approve or plan["digest"]}, store=self.store)

    def test_model_removal_targets_one_and_replay_is_idempotent(self):
        plan = self.plan()
        receipt = self.apply(plan)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual([m["name"] for m in self.models], ["other:latest"])
        before = list(self.calls)
        self.assertEqual(self.apply(plan)["id"], receipt["id"])
        self.assertEqual(before, self.calls)
        self.assertIn("No Trash recovery", plan["summary"]["recovery"])

    def test_wrong_approval_does_not_send_delete(self):
        plan = self.plan()
        with self.assertRaises(CleanMacError):
            self.apply(plan, "0" * 64)
        self.assertFalse(any(call[2] == "DELETE" for call in self.calls))

    def test_loaded_model_or_shared_loaded_digest_protects_it(self):
        self.loaded = [{"name": "alias", "digest": "a" * 64}]
        scan = self.inspect("models")
        item = next(i for i in scan["items"] if i["name"] == "fixture:latest")
        self.assertFalse(item["eligible"])
        with self.assertRaises(CleanMacError):
            c.dispatch("models", "plan", {"record_id": scan["id"], "selected": [item["id"]]}, store=self.store)

    def test_model_loaded_after_approval_does_not_delete(self):
        plan = self.plan()
        self.loaded = [self.models[0]]
        with self.assertRaises(CleanMacError):
            self.apply(plan)
        self.assertFalse(any(call[2] == "DELETE" for call in self.calls))

    def test_model_replaced_after_plan_is_rejected(self):
        plan = self.plan()
        self.models[0]["digest"] = "c" * 64
        with self.assertRaises(CleanMacError):
            self.apply(plan)
        self.assertFalse(any(call[2] == "DELETE" for call in self.calls))

    def test_ambiguous_api_failure_is_never_replayed(self):
        plan = self.plan()
        original = self.api_mock.side_effect
        def failed(endpoint, path, method="GET", payload=None):
            if method == "DELETE":
                raise CleanMacError("timeout after dispatch", "model_api_unavailable")
            return original(endpoint, path, method, payload)
        self.api_mock.side_effect = failed
        receipt = self.apply(plan)
        self.assertEqual(receipt["status"], "recovery_needed")
        calls = self.api_mock.call_count
        self.assertEqual(self.apply(plan)["status"], "recovery_needed")
        self.assertEqual(self.api_mock.call_count, calls)

    def test_non_loopback_endpoints_never_connect(self):
        for endpoint in ("http://example.com", "http://localhost:11434", "http://127.0.0.1:11434/private", "http://name:password@127.0.0.1:11434", "file:///tmp/api", "http://127.0.0.1:99999"):
            with self.subTest(endpoint=endpoint), self.assertRaises(CleanMacError):
                self.inspect("models", endpoint=endpoint)
        self.api_mock.assert_not_called()

    def test_malformed_loaded_state_is_unverified_not_empty(self):
        self.loaded = ["unknown"]
        report = self.inspect("models")
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["items"], [])


class SimulatorOperationTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.environment = {"developer_dir": "/fixture/Xcode.app/Contents/Developer", "developer_identity": {"device": 1, "inode": 2}, "simctl": "/fixture/simctl", "simctl_identity": {"inode": 3}, "delete_help_digest": "d" * 64}
        device = {"udid": "62ae2b96-08f3-42c4-a3a2-25b63ab367a4", "runtime": "missing-runtime", "name": "Test phone", "state": "Shutdown", "available": False}
        self.devices = [dict(device, id="simulator-" + digest(device)[:24], eligible=True, reason="fixture")]
        self.snapshot_patch = patch.object(c, "_sim_snapshot", side_effect=lambda configuration: {**copy.deepcopy(self.environment), "items": copy.deepcopy(self.devices)})
        self.snapshot_patch.start()
        self.addCleanup(self.snapshot_patch.stop)

    def plan(self):
        scan = self.inspect("xcode-cleanup", simulators=True)
        return c.dispatch("xcode-cleanup", "plan", {"record_id": scan["id"], "selected": [scan["items"][0]["id"]]}, store=self.store)

    def test_one_unavailable_simulator_uses_exact_udid(self):
        plan = self.plan()
        udid = self.devices[0]["udid"]
        def remove(argv, **kwargs):
            self.assertEqual(argv, ["/fixture/simctl", "delete", udid])
            self.devices.clear()
            return ""
        with patch.object(c, "_run", side_effect=remove) as run:
            receipt = c.dispatch("xcode-cleanup", "apply", {"record_id": plan["id"], "approve": plan["digest"]}, store=self.store)
        self.assertEqual(receipt["status"], "completed")
        run.assert_called_once()

    def test_changed_developer_identity_refuses_delete(self):
        plan = self.plan()
        self.environment["developer_identity"]["inode"] = 999
        with patch.object(c, "_run") as run, self.assertRaises(CleanMacError):
            c.dispatch("xcode-cleanup", "apply", {"record_id": plan["id"], "approve": plan["digest"]}, store=self.store)
        run.assert_not_called()

    def test_booted_or_now_available_device_refuses_delete(self):
        plan = self.plan()
        self.devices[0].update(state="Booted", eligible=False)
        with patch.object(c, "_run") as run, self.assertRaises(CleanMacError):
            c.dispatch("xcode-cleanup", "apply", {"record_id": plan["id"], "approve": plan["digest"]}, store=self.store)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

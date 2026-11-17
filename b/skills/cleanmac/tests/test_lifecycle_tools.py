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

from cleanmac_core.common import CleanMacError
from cleanmac_core.state import StateStore
from cleanmac_core import lifecycle_tools as lifecycle


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cleanmac-lifecycle-")
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "selected folder"
        self.root.mkdir()
        (self.root / "one.txt").write_bytes(b"same selected content")
        (self.root / "two.txt").write_bytes(b"same selected content")
        self.agents = self.base / "LaunchAgents"
        self.store = StateStore(self.base / "state")
        self.loaded = set()
        self.commands = []
        self.platform = patch.object(lifecycle.sys, "platform", "darwin")
        self.platform.start()
        self.location = patch.object(lifecycle, "_launchagents_path", return_value=self.agents)
        self.location.start()
        self.launchctl = patch.object(lifecycle, "_launchctl", side_effect=self.fake_launchctl)
        self.launchctl.start()

    def tearDown(self):
        self.launchctl.stop()
        self.location.stop()
        self.platform.stop()
        self.tmp.cleanup()

    def fake_launchctl(self, arguments):
        self.commands.append(arguments)
        label = arguments[-1].split("/")[-1]
        if arguments[0] == "print":
            return subprocess.CompletedProcess(arguments, 0 if label in self.loaded else 113, "", "")
        if arguments[0] == "bootstrap":
            with open(arguments[-1], "rb") as source:
                self.loaded.add(plistlib.load(source)["Label"])
        elif arguments[0] == "bootout":
            self.loaded.remove(label)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    def call(self, name, action, **options):
        return lifecycle.dispatch(name, action, options, store=self.store)

    def create_plan(self, **configuration):
        config = {"name": "fixture", "require_ac_power": False, "max_load_per_cpu": 4,
                  "areas": ["files", "duplicates", "storage"], **configuration}
        return self.call("schedules", "plan", roots=[str(self.root)], configuration=config)

    def apply(self, plan):
        return self.call("schedules", "apply", record_id=plan["id"], approve=plan["digest"])

    def install(self, **configuration):
        plan = self.create_plan(**configuration)
        receipt = self.apply(plan)
        self.assertEqual(receipt["status"], "completed")
        return plan["payload"]["schedule"]["id"]

    def test_inspect_never_installs_persistence(self):
        self.assertEqual(self.call("schedules", "inspect")["schedules"], [])
        self.assertFalse(self.agents.exists())
        self.assertEqual(self.commands, [])

    def test_preset_roundtrip_is_explicit_and_has_no_approval(self):
        result = self.call("saved-scopes", "save", roots=[str(self.root)],
                           configuration={"name": "downloads", "areas": ["files", "duplicates"], "search": "one"})
        self.assertIsNone(result["preset"]["scan"]["approval"])
        scan = self.call("saved-scopes", "run", configuration={"name": "downloads"})
        self.assertEqual(scan["status"], "complete")
        self.assertEqual(scan["preset_name"], "downloads")
        self.assertEqual(scan["saved_filters"], {"search": "one"})
        self.assertEqual(len(scan["duplicate_groups"]), 1)
        self.assertEqual(scan["summary"]["selected_count"], 0)
        self.assertEqual(self.store.list("plans"), [])

    def test_preset_cannot_override_scope(self):
        self.call("saved-scopes", "save", roots=[str(self.root)], configuration={"name": "one"})
        with self.assertRaises(CleanMacError) as error:
            self.call("saved-scopes", "run", roots=[str(self.base)], configuration={"name": "one"})
        self.assertEqual(error.exception.code, "scope_mismatch")

    def test_changed_root_invalidates_saved_scope(self):
        self.call("saved-scopes", "save", roots=[str(self.root)], configuration={"name": "one"})
        self.root.rename(self.base / "original")
        self.root.mkdir()
        (self.root / "new.txt").write_text("not selected")
        with self.assertRaises(CleanMacError) as error:
            self.call("saved-scopes", "run", configuration={"name": "one"})
        self.assertEqual(error.exception.code, "stale_scope")
        self.assertEqual(self.store.list("scans"), [])

    def test_symlink_root_rejected(self):
        link = self.base / "alias"
        link.symlink_to(self.root)
        with self.assertRaises(CleanMacError):
            self.call("saved-scopes", "save", roots=[str(link)], configuration={"name": "link"})

    def test_presets_and_favorites_preserve_ignore_settings(self):
        self.store.save_settings({"ignore_paths": ["/example/ignored"]})
        self.call("saved-scopes", "save", roots=[str(self.root)], configuration={"name": "one"})
        self.call("favorites", "add", configuration={"name": "smart-care"})
        self.call("favorites", "add", configuration={"name": "smart-care"})
        self.assertEqual(self.call("favorites", "inspect")["favorites"], ["smart-care"])
        self.assertEqual(self.store.get_settings()["ignore_paths"], ["/example/ignored"])
        self.call("favorites", "remove", configuration={"name": "smart-care"})
        self.call("saved-scopes", "remove", configuration={"name": "one"})
        self.assertTrue((self.root / "one.txt").exists())

    def test_smart_care_counts_inventory_once_and_malware_unscanned(self):
        result = self.call("smart-care", "inspect", roots=[str(self.root)],
                           configuration={"modules": ["files", "storage", "duplicates", "malware"]})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["summary"]["unique_file_count"], 2)
        self.assertEqual(result["summary"]["logical_bytes"], 2 * len(b"same selected content"))
        malware = [tile for tile in result["tiles"] if tile["name"] == "malware"][0]
        self.assertEqual(malware["status"], "unscanned")
        self.assertIsNone(malware["result"])
        self.assertEqual(self.call("smart-care", "review", record_id=result["id"])["tiles"], result["tiles"])

    def test_schedule_plan_is_read_only_and_argv_is_not_shell(self):
        plan = self.create_plan()
        self.assertFalse(self.agents.exists())
        self.assertEqual(len(self.store.list("plans")), 1)
        args = plan["payload"]["program"]
        self.assertEqual(args[1], "-I")
        self.assertNotIn("sh", args)
        self.assertNotIn("apply", args)
        self.assertNotIn(str(self.root), args)
        self.assertTrue(plan["summary"]["read_only"])
        proposed = plan["payload"]["launchagent"]
        self.assertEqual(proposed["ProgramArguments"], args)
        self.assertEqual(proposed["StartInterval"], 86400)
        self.assertEqual(proposed["EnvironmentVariables"], {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONNOUSERSITE": "1"})
        self.assertFalse(proposed["RunAtLoad"])

    def test_schedule_create_disable_enable_remove(self):
        sid = self.install()
        path = self.agents / (lifecycle._label(sid) + ".plist")
        self.assertTrue(path.exists())
        definition = plistlib.loads(path.read_bytes())
        self.assertFalse(definition["RunAtLoad"])
        for operation, exists in (("disable", False), ("enable", True), ("remove", False)):
            plan = self.call("schedules", "plan", record_id=sid, configuration={"operation": operation})
            self.assertEqual(self.apply(plan)["status"], "completed")
            self.assertEqual(path.exists(), exists)
        self.assertEqual(self.call("schedules", "inspect")["schedules"], [])
        self.assertTrue((self.root / "one.txt").exists())

    def test_wrong_approval_does_not_create_agent(self):
        plan = self.create_plan()
        with self.assertRaises(CleanMacError):
            self.call("schedules", "apply", record_id=plan["id"], approve="wrong")
        self.assertFalse(self.agents.exists())

    def test_completed_action_replay_does_not_load_twice(self):
        plan = self.create_plan()
        first = self.apply(plan)
        count = len(self.commands)
        second = self.apply(plan)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.commands), count)

    def test_scope_change_rejects_install(self):
        plan = self.create_plan()
        self.root.rename(self.base / "moved")
        self.root.mkdir()
        with self.assertRaises(CleanMacError):
            self.apply(plan)
        self.assertFalse(self.agents.exists())

    def test_changed_agent_is_never_overwritten_or_removed(self):
        sid = self.install()
        plan = self.call("schedules", "plan", record_id=sid, configuration={"operation": "remove"})
        path = self.agents / (lifecycle._label(sid) + ".plist")
        path.write_bytes(b"user changed this")
        with self.assertRaises(CleanMacError):
            self.apply(plan)
        self.assertEqual(path.read_bytes(), b"user changed this")

    def test_failed_bootstrap_retains_receipt_and_can_explicitly_remove(self):
        plan = self.create_plan()
        def fail_bootstrap(args):
            if args[0] == "bootstrap":
                return subprocess.CompletedProcess(args, 5, "", "failure")
            return self.fake_launchctl(args)
        with patch.object(lifecycle, "_launchctl", side_effect=fail_bootstrap):
            outcome = self.apply(plan)
            self.assertEqual(outcome["status"], "recovery_needed")
        sid = plan["payload"]["schedule"]["id"]
        records = self.store.list("receipts")
        self.assertEqual(records[0]["status"], "recovery_needed")
        self.assertEqual(records[0]["checkpoint"]["phase"], "bootstrap")
        self.assertFalse(lifecycle._snapshot(self.store)["schedules"][sid]["enabled"])
        cleanup = self.call("schedules", "plan", record_id=sid, configuration={"operation": "remove"})
        self.assertEqual(self.apply(cleanup)["status"], "completed")
        self.assertFalse((self.agents / (lifecycle._label(sid) + ".plist")).exists())

    def test_run_respects_quiet_hours_and_keeps_status(self):
        sid = self.install(quiet_hours=[22, 7])
        with patch.object(lifecycle, "_run_scan") as scan:
            result = lifecycle.run_schedule(self.store, sid, now=dt.datetime(2026, 1, 1, 23, tzinfo=dt.timezone.utc))
        self.assertEqual(result["reason"], "quiet-hours")
        scan.assert_not_called()
        self.assertEqual(self.store.get("scans", result["id"])["status"], "skipped")

    def test_run_respects_ac_and_load_policy(self):
        sid = self.install(require_ac_power=True)
        with patch.object(lifecycle, "_power_source", return_value="unknown"), patch.object(lifecycle, "_run_scan") as scan:
            result = lifecycle.run_schedule(self.store, sid)
        self.assertEqual(result["reason"], "ac-power-unavailable-or-unverified")
        scan.assert_not_called()
        with patch.object(lifecycle, "_power_source", return_value="ac"), patch.object(lifecycle.os, "getloadavg", return_value=(9999, 0, 0)), patch.object(lifecycle, "_run_scan") as scan:
            result = lifecycle.run_schedule(self.store, sid)
        self.assertEqual(result["reason"], "load-policy")
        scan.assert_not_called()

    def test_notifications_are_opt_in_measured_and_deduplicated(self):
        sid = self.install(notify=True, low_space_bytes=2**62)
        instant = dt.datetime(2026, 1, 1, 12, tzinfo=dt.timezone.utc)
        with patch.object(lifecycle.os, "getloadavg", return_value=(0, 0, 0)), patch.object(lifecycle, "_notify_alerts", return_value="submitted") as notify:
            first = lifecycle.run_schedule(self.store, sid, now=instant)
            second = lifecycle.run_schedule(self.store, sid, now=instant + dt.timedelta(minutes=10))
        self.assertEqual(first["status"], "complete")
        self.assertEqual(first["notification"], "submitted")
        self.assertEqual(second["notification"], "deduplicated")
        self.assertEqual(notify.call_count, 1)
        self.assertEqual(len(self.store.list("plans")), 1)
        self.assertTrue((self.root / "one.txt").exists())

    def test_default_schedule_has_no_notifications_or_cleanup(self):
        sid = self.install()
        with patch.object(lifecycle.os, "getloadavg", return_value=(0, 0, 0)), patch.object(lifecycle, "_notify_alerts") as notify:
            result = lifecycle.run_schedule(self.store, sid)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["notification"], "not-requested")
        notify.assert_not_called()
        self.assertEqual(len(self.store.list("plans")), 1)
        self.assertTrue(result["read_only"])

    def test_explicit_cancel_runs_no_scan(self):
        sid = self.install()
        with patch.object(lifecycle, "_run_scan") as scan:
            result = lifecycle.run_schedule(self.store, sid, cancel=lambda: True)
        self.assertEqual(result["status"], "cancelled")
        scan.assert_not_called()

    def test_unknown_mutating_settings_rejected(self):
        for extra in ({"cleanup": True}, {"command": "echo bad"}, {"credentials": "none"}):
            with self.assertRaises(CleanMacError):
                self.create_plan(**extra)
        self.assertEqual(self.store.list("plans"), [])

    def test_schedule_run_scope_override_rejected(self):
        sid = self.install()
        with self.assertRaises(CleanMacError):
            self.call("schedules", "run", record_id=sid, roots=[str(self.base)])

    def test_non_macos_schedule_is_unavailable(self):
        with patch.object(lifecycle.sys, "platform", "linux"):
            with self.assertRaises(CleanMacError) as error:
                self.create_plan()
        self.assertEqual(error.exception.code, "unsupported_platform")

    def test_smart_care_runs_only_explicitly_configured_extra_checks(self):
        response = {"status": "complete", "id": "malware-fixture", "counts": {"detections": 0}, "engine": {"status": "available"}}
        with patch.object(lifecycle, "_run_check", return_value=response) as inspect:
            result = self.call("smart-care", "inspect", roots=[str(self.root)], configuration={
                "modules": ["files", "malware", "updates"],
                "checks": {"malware": {"engine_path": "/example/clamscan", "mode": "quick"}}})
        inspect.assert_called_once()
        self.assertEqual(inspect.call_args.args[0], "malware")
        self.assertEqual(inspect.call_args.args[2], [str(self.root)])
        self.assertEqual([tile for tile in result["tiles"] if tile["name"] == "updates"][0]["status"], "unscanned")
        self.assertEqual(self.store.list("plans"), [])

    def test_smart_care_maintenance_cannot_dispatch_actions_or_dns(self):
        with self.assertRaises(CleanMacError):
            self.call("smart-care", "inspect", roots=[str(self.root)], configuration={
                "modules": ["maintenance"], "checks": {"maintenance": {"tool": "dns"}}})
        with self.assertRaises(CleanMacError):
            self.call("smart-care", "inspect", roots=[str(self.root)], configuration={
                "modules": ["updates"], "checks": {"updates": {"action": "apply"}}})

    def test_unknown_measurements_never_become_alerts(self):
        schedule = {"battery_charge_percent": 20}
        inputs = [
            ("memory", {"status": "complete", "measurements": {"pressure": {"status": "unknown", "level": "critical"}}}),
            ("battery", {"status": "partial", "items": [{"charge_percent": None, "external_power": False}]}),
            ("malware", {"status": "unavailable", "counts": {"detections": 1}, "engine": {"status": "unavailable"}}),
            ("updates", {"status": "complete", "items": [{"update_available": None, "source": "homebrew"}]}),
        ]
        for name, result in inputs:
            self.assertEqual(lifecycle._measured_alerts(name, result, schedule), [])

    def test_scheduled_measured_alerts_deduplicate_and_never_mutate(self):
        checks = {"memory": {}, "battery": {}, "updates": {"brew_path": "/example/brew"},
                  "malware": {"engine_path": "/example/clamscan"}}
        sid = self.install(checks=checks, notify=True, low_space_bytes=1)
        values = {
            "memory": {"status": "complete", "measurements": {"pressure": {"status": "available", "level": "warning"}}},
            "battery": {"status": "complete", "items": [{"charge_percent": 10, "external_power": False}]},
            "updates": {"status": "complete", "items": [{"token": "fixture", "available_version": "2", "update_available": True, "source": "homebrew"}]},
            "malware": {"status": "partial", "counts": {"detections": 1}, "engine": {"status": "available"}},
        }
        instant = dt.datetime(2026, 1, 1, 12, tzinfo=dt.timezone.utc)
        with patch.object(lifecycle.os, "getloadavg", return_value=(0, 0, 0)), patch.object(lifecycle, "_run_check", side_effect=lambda name, *args: values[name]) as inspect, patch.object(lifecycle, "_notify_alerts", return_value="submitted") as notify:
            first = lifecycle.run_schedule(self.store, sid, now=instant)
            second = lifecycle.run_schedule(self.store, sid, now=instant + dt.timedelta(minutes=10))
        self.assertEqual(first["status"], "partial")
        self.assertEqual({x["kind"] for x in first["alerts"]}, {"memory-pressure", "battery-charge", "app-updates", "malware-detections"})
        self.assertEqual(second["notification"], "deduplicated")
        self.assertEqual(inspect.call_count, 8)
        self.assertEqual(notify.call_count, 1)
        self.assertEqual(len(self.store.list("plans")), 1)
        self.assertTrue((self.root / "one.txt").exists())

    def test_cancelled_additional_check_cannot_notify(self):
        sid = self.install(checks={"memory": {}}, notify=True, low_space_bytes=2**62)
        with patch.object(lifecycle.os, "getloadavg", return_value=(0, 0, 0)), patch.object(lifecycle, "_run_check", return_value={"status": "cancelled"}), patch.object(lifecycle, "_notify_alerts") as notify:
            report = lifecycle.run_schedule(self.store, sid)
        self.assertEqual(report["status"], "cancelled")
        notify.assert_not_called()

    def test_schedule_check_requires_specific_engine_without_credentials(self):
        with self.assertRaises(CleanMacError):
            self.create_plan(checks={"malware": {}})
        with self.assertRaises(CleanMacError):
            self.create_plan(checks={"mail": {"token": "do-not-store"}})
        with self.assertRaises(CleanMacError):
            self.create_plan(checks={"malware": {"engine_path": "/example/clamscan", "targets": [{}]}})

    def test_favorites_require_known_tool_and_report_removed_registrations(self):
        with self.assertRaises(CleanMacError) as error:
            self.call("favorites", "add", configuration={"name": "fictional-tool"})
        self.assertEqual(error.exception.code, "tool_unavailable")
        lifecycle._settings_change(self.store, lambda settings: lifecycle._lifecycle(settings)["favorites"].append("removed-tool"))
        result = self.call("favorites", "inspect")
        self.assertEqual(result["tools"][0]["implementation"], "unavailable")
        self.call("favorites", "remove", configuration={"name": "removed-tool"})

    def test_smart_care_dedicated_only_complete_is_not_falsely_partial(self):
        with patch.object(lifecycle, "_run_check", return_value={"status": "complete", "items": []}):
            result = self.call("smart-care", "inspect", configuration={"modules": ["health"], "checks": {"health": {}}})
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["items"], [])

    def test_schedule_copies_preset_and_later_changes_cannot_widen_scope(self):
        self.call("saved-scopes", "save", roots=[str(self.root)], configuration={"name": "Downloads", "areas": ["files"]})
        plan = self.call("schedules", "plan", configuration={"name": "weekly", "preset": "Downloads"})
        self.call("saved-scopes", "save", roots=[str(self.base)], configuration={"name": "Downloads", "areas": ["files", "caches"]})
        self.assertEqual(self.apply(plan)["status"], "completed")
        scheduled = lifecycle._snapshot(self.store)["schedules"][plan["payload"]["schedule"]["id"]]
        self.assertEqual(scheduled["scan"]["areas"], ["files"])
        self.assertEqual([x["path"] for x in scheduled["scan"]["roots"]], [str(self.root)])
        self.assertEqual(scheduled["source_preset"], "Downloads")

    def test_schedule_preset_rejects_root_or_filter_override(self):
        self.call("saved-scopes", "save", roots=[str(self.root)], configuration={"name": "Downloads"})
        for options in ({"roots": [str(self.base)]}, {"configuration": {"areas": ["caches"]}}):
            config = {"name": "weekly", "preset": "Downloads", **options.pop("configuration", {})}
            with self.assertRaises(CleanMacError):
                self.call("schedules", "plan", configuration=config, **options)

    def test_corrupt_saved_check_cannot_dispatch_cleanup(self):
        sid = self.install()
        lifecycle._settings_change(self.store, lambda settings: lifecycle._lifecycle(settings)["schedules"][sid].update(checks={"updates": {"action": "apply"}}))
        with patch.object(lifecycle, "_run_scan") as scan, patch.object(lifecycle, "_run_check") as check:
            result = lifecycle.run_schedule(self.store, sid)
        self.assertEqual(result["status"], "failed")
        scan.assert_not_called()
        check.assert_not_called()

    @unittest.skipUnless(sys.platform == "darwin", "Native scheduled runner requires macOS")
    def test_isolated_runner_subprocess_scans_only_fixture_and_prints_no_paths(self):
        sid = self.install()
        runner = Path(lifecycle.__file__).with_name("schedule_runner.py")
        result = subprocess.run([sys.executable, "-I", str(runner), "--data-dir", str(self.store.root),
                                 "--schedule", sid], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)
        self.assertIn(response["status"], {"complete", "skipped"})
        self.assertNotIn(str(self.root), result.stdout + result.stderr)
        self.assertTrue((self.root / "one.txt").exists())
        self.assertEqual(len(self.store.list("plans")), 1)


if __name__ == "__main__":
    unittest.main()

"""Maintenance behavior with generated files and mocked native transports only."""
import os
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cleanmac_core import maintenance_tools as module
from cleanmac_core.common import CleanMacError
from cleanmac_core.state import StateStore


def output(text="", *, code=0, reason=None):
    result = {"status": "available" if code == 0 else "unavailable", "returncode": code, "stdout": text, "stderr": ""}
    if reason:
        result["reason"] = reason
    return result


class MaintenanceToolsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.store = StateStore(self.root / "state")
        self.platform = patch.object(module.sys, "platform", "darwin")
        self.platform.start()
        self.commands = []
        self.run_patch = patch.object(module, "_run", side_effect=self.native)
        self.run_patch.start()
        self.networks = ["Home", "Public"]
        self.snapshot_names = ["com.apple.TimeMachine.2026-11-12-120000.local", "com.apple.TimeMachine.2026-11-13-120000.local"]
        self.backup_running = False
        self.indexing = "enabled"
        self.disabled = False
        self.vm_pressure = "1"
        self.before_addresses = "192.0.2.1"
        self.volume = {"path": str(self.root), "mount_point": str(self.root), "volume_uuid": "01234567-89AB-CDEF-0123-456789ABCDEF",
                       "device": 42, "inode": 17, "read_only": False, "filesystem": "apfs", "apfs_container": "disk1", "disk_identifier": "disk1s1"}

    def tearDown(self):
        self.run_patch.stop()
        self.platform.stop()
        self.temporary.cleanup()

    def native(self, argv, **kwargs):
        self.commands.append(argv)
        if argv[0] == "/usr/sbin/networksetup":
            if argv[1] == "-listallhardwareports":
                return output("Hardware Port: Wi-Fi\nDevice: en0\nEthernet Address: 00:00:00:00:00:00\n")
            if argv[1] == "-listpreferredwirelessnetworks":
                return output("Preferred networks on en0:\n" + "".join("\t" + value + "\n" for value in self.networks))
            if argv[1] == "-getairportnetwork":
                return output("Current Wi-Fi Network: Home\n")
        if argv[0] == "/usr/sbin/diskutil":
            return output(plistlib.dumps({"MountPoint": str(self.root), "VolumeUUID": self.volume["volume_uuid"], "FilesystemType": "apfs"}).decode())
        if argv[0] == "/usr/bin/tmutil":
            if argv[1] == "listlocalsnapshots":
                return output("Snapshots for disk /:\n" + "\n".join(self.snapshot_names))
            if argv[1] == "status":
                return output("Backup session status:\n{\n Running = %d;\n}" % self.backup_running)
            if argv[1] == "thinlocalsnapshots":
                self.snapshot_names = self.snapshot_names[1:]
                return output("Thinned local snapshots\n")
            if argv[1] == "deletelocalsnapshots":
                self.snapshot_names = []
                return output("Deleted local snapshots\n")
        if argv[0] == "/usr/bin/mdutil":
            return output("%s:\n Indexing %s.\n" % (self.root, self.indexing))
        if argv[0] == "/usr/bin/dscacheutil":
            if argv[1] == "-q":
                return output("name: example.test\nip_address: %s\n" % self.before_addresses)
            if argv[1] == "-flushcache":
                self.before_addresses = "192.0.2.2"
                return output()
        if argv[0] == "/usr/bin/vm_stat":
            return output("Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: 1.\nPages occupied by compressor: 500.\nSwapins: 20.\nSwapouts: 12.\n")
        if argv[0] == "/usr/sbin/sysctl":
            return output(self.vm_pressure) if argv[-1] == "kern.memorystatus_vm_pressure_level" else output("total = 1024.00M used = 12.00M free = 1012.00M")
        if argv[0] == "/bin/launchctl":
            if argv[1] == "print-disabled":
                return output('disabled services = {\n "com.example.backup" => %s\n}' % ("true" if self.disabled else "false"))
            if argv[1] == "print":
                return output("state = running\n")
            if argv[1] in ("enable", "disable"):
                self.disabled = argv[1] == "disable"
                return output()
        if argv[0] in ("/usr/bin/tccutil", "/usr/bin/open", "/usr/bin/periodic"):
            return output()
        raise AssertionError("Unexpected native command: %r" % argv)

    def call(self, name, action="inspect", *, config=None, roots=None, selected=None, plan=None, record=None, approve=None):
        return module.dispatch(name, action, {"configuration": config or {}, "roots": roots or [], "selected": selected or [],
                               "record_id": record or (plan["id"] if plan else None), "approve": approve or (plan["digest"] if plan else None)}, store=self.store)

    def make_job(self, *, label="com.example.backup", exists=True):
        home = self.root / "home"
        directory = home / "Library" / "LaunchAgents"
        directory.mkdir(parents=True, exist_ok=True)
        program = self.root / "custom-worker"
        if exists:
            program.write_bytes(b"fixture executable")
        target = directory / "worker.plist"
        target.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": [str(program)], "RunAtLoad": True}))
        target.chmod(0o600)
        return home, directory, target

    def test_wifi_inventory_identifies_active_without_password(self):
        report = self.call("wifi")
        self.assertEqual(report["status"], "complete")
        self.assertTrue(report["networks"][0]["active"])
        self.assertFalse(report["networks"][1]["active"])
        self.assertFalse(any("password" in " ".join(argv) for argv in self.commands))
        self.assertEqual(self.store.get("scans", report["id"])["tool"], "wifi")

    def test_wifi_handoff_cancel_verifies_unchanged(self):
        report = self.call("wifi")
        plan = self.call("wifi", "plan", config={"operation": "open-settings"}, selected=[report["networks"][1]["id"]])
        receipt = self.call("wifi", "apply", plan=plan)
        self.assertFalse(receipt["result"]["data_changed_by_tool"])
        verification = self.call("wifi", "verify", record=receipt["id"])
        self.assertEqual(verification["outcome"], "unchanged")
        self.networks = ["Home"]
        verification = self.call("wifi", "verify", record=receipt["id"])
        self.assertEqual(verification["outcome"], "changed")
        self.assertEqual(verification["removed_networks"][0]["ssid"], "Public")

    def test_wifi_changed_selected_network_blocks_handoff(self):
        report = self.call("wifi")
        plan = self.call("wifi", "plan", config={"operation": "open-settings"}, selected=[report["networks"][1]["id"]])
        self.networks = ["Home"]
        with self.assertRaises(CleanMacError):
            self.call("wifi", "apply", plan=plan)
        self.assertFalse(any(argv[0] == "/usr/bin/open" for argv in self.commands))

    def test_permission_inventory_does_not_read_tcc(self):
        report = self.call("app-permissions")
        self.assertEqual(report["status"], "partial")
        self.assertTrue(all(value["grant_state"] == "unknown" for value in report["permissions"]))
        self.assertEqual(self.commands, [])

    def test_permission_reset_is_one_service_one_app(self):
        plan = self.call("app-permissions", "plan", config={"operation": "reset", "service": "Camera", "bundle_id": "com.example.app"})
        self.assertEqual(self.commands, [])
        receipt = self.call("app-permissions", "apply", plan=plan)
        self.assertEqual(self.commands, [["/usr/bin/tccutil", "reset", "Camera", "com.example.app"]])
        self.assertEqual(receipt["result"]["grant_state"], "unknown")
        self.call("app-permissions", "apply", plan=plan)
        self.assertEqual(len(self.commands), 1)

    def test_permission_reset_all_and_shell_strings_rejected(self):
        for service, bundle in (("All", "com.example.app"), ("Camera", "com.example.app;whoami"), ("Camera", ""), ("Camera", "--help")):
            with self.subTest(service=service, bundle=bundle), self.assertRaises(CleanMacError):
                self.call("app-permissions", "plan", config={"operation": "reset", "service": service, "bundle_id": bundle})
        self.assertEqual(self.commands, [])

    def test_wrong_digest_never_runs_reset(self):
        plan = self.call("app-permissions", "plan", config={"operation": "reset", "service": "Camera", "bundle_id": "com.example.app"})
        with self.assertRaises(CleanMacError):
            self.call("app-permissions", "apply", plan=plan, approve="0" * 64)
        self.assertEqual(self.commands, [])

    def test_dns_only_flushes_on_approved_apply_and_keeps_before_after(self):
        self.call("dns", config={"hostname": "example.test"})
        plan = self.call("dns", "plan", config={"operation": "flush", "hostname": "example.test"})
        self.assertFalse(any(argv[-1] == "-flushcache" for argv in self.commands))
        receipt = self.call("dns", "apply", plan=plan)
        self.assertEqual(receipt["result"]["before"]["addresses"], ["192.0.2.1"])
        self.assertEqual(receipt["result"]["after"]["addresses"], ["192.0.2.2"])
        self.assertEqual(receipt["result"]["problem_resolved"], "not-established")
        self.assertFalse(receipt["result"]["mdnsresponder_signalled"])

    def test_dns_requires_named_hostname(self):
        for value in (None, "", "example.test;id", "$(whoami)", "-example.test", "a b.test"):
            with self.subTest(host=value), self.assertRaises(CleanMacError):
                self.call("dns", config={"hostname": value})

    def test_native_failure_does_not_escalate_or_retry(self):
        plan = self.call("dns", "plan", config={"operation": "flush", "hostname": "example.test"})
        def failure(argv, **kwargs):
            if argv[-1] == "-flushcache":
                self.commands.append(argv)
                return output(code=1, reason="permission-needed")
            return self.native(argv, **kwargs)
        with patch.object(module, "_run", side_effect=failure):
            receipt = self.call("dns", "apply", plan=plan)
        self.assertEqual(receipt["status"], "recovery_needed")
        self.assertEqual(sum(argv[-1] == "-flushcache" for argv in self.commands), 1)
        self.assertFalse(any("sudo" in argv or "killall" in argv for argv in self.commands))
        self.call("dns", "apply", plan=plan)
        self.assertEqual(sum(argv[-1] == "-flushcache" for argv in self.commands), 1)

    def test_capacity_deduplicates_device_without_adding_purgeable(self):
        other = self.root / "other"
        other.mkdir()
        report = self.call("purgeable", roots=[str(self.root), str(other)])
        self.assertEqual(len(report["volumes"]), 1)
        self.assertIsNone(report["aggregate_bytes"])
        self.assertIsNone(report["volumes"][0]["purgeable_bytes"])

    def test_low_free_memory_is_not_a_problem(self):
        report = self.call("memory-diagnosis")
        self.assertEqual(report["pressure"], "normal")
        self.assertEqual(report["pages"]["Pages free"], 1)
        self.assertEqual(report["available_actions"], [])
        self.assertEqual(report["swap_used_bytes"], 12 * 1024 * 1024)
        self.assertNotIn("purge", " ".join(" ".join(value) for value in self.commands))

    def test_unknown_pressure_stays_partial(self):
        self.vm_pressure = "unknown"
        report = self.call("memory-diagnosis")
        self.assertEqual(report["pressure"], "unknown")
        self.assertEqual(report["status"], "partial")

    def test_modern_periodic_never_executes_missing_job(self):
        with patch.object(module.platform, "mac_ver", return_value=("27.2", (), "")):
            report = self.call("periodic")
            self.assertEqual(report["operation_status"], "unsupported")
            with self.assertRaises(CleanMacError):
                self.call("periodic", "plan", config={"operation": "run", "schedule": "daily"})
        self.assertEqual(self.commands, [])

    def test_legacy_periodic_selects_only_one_schedule(self):
        with patch.object(module.platform, "mac_ver", return_value=("13.7", (), "")), patch.object(module.os.path, "isfile", return_value=True), patch.object(module.os, "access", return_value=True):
            plan = self.call("periodic", "plan", config={"operation": "run", "schedule": "weekly"})
            result = self.call("periodic", "apply", plan=plan)
        self.assertEqual(self.commands, [["/usr/bin/periodic", "weekly"]])
        self.assertEqual(result["result"]["performance_improvement"], "not-measured")

    def test_spotlight_one_volume_and_asynchronous_completion(self):
        with patch.object(module, "_volume", return_value=self.volume):
            plan = self.call("spotlight", "plan", roots=[str(self.root)], config={"operation": "rebuild"})
            receipt = self.call("spotlight", "apply", plan=plan)
        self.assertIn(["/usr/bin/mdutil", "-E", str(self.root)], self.commands)
        self.assertFalse(any("-a" in argv for argv in self.commands))
        self.assertFalse(receipt["result"]["rebuild_completed"])

    def test_disabled_spotlight_cannot_be_rebuilt(self):
        self.indexing = "disabled"
        with patch.object(module, "_volume", return_value=self.volume), self.assertRaises(CleanMacError):
            self.call("spotlight", "plan", roots=[str(self.root)], config={"operation": "rebuild"})
        self.assertFalse(any("-E" in argv for argv in self.commands))

    def test_changed_volume_prevents_rebuild(self):
        with patch.object(module, "_volume", return_value=self.volume):
            plan = self.call("spotlight", "plan", roots=[str(self.root)], config={"operation": "rebuild"})
        with patch.object(module, "_volume", return_value={**self.volume, "volume_uuid": "FEDCBA98-7654-3210-FEDC-BA9876543210"}), self.assertRaises(CleanMacError):
            self.call("spotlight", "apply", plan=plan)
        self.assertFalse(any("-E" in argv for argv in self.commands))

    def test_snapshot_thinning_is_volume_scoped_and_measures_removed_names(self):
        original = list(self.snapshot_names)
        with patch.object(module, "_volume", return_value=self.volume):
            plan = self.call("snapshots", "plan", roots=[str(self.root)], config={"operation": "thin", "bytes": 1000})
            receipt = self.call("snapshots", "apply", plan=plan)
        self.assertIn(["/usr/bin/tmutil", "thinlocalsnapshots", str(self.root), "1000", "1"], self.commands)
        self.assertEqual(receipt["result"]["removed_snapshots"], original[:1])
        self.assertIsNone(receipt["result"]["reclaimed_bytes"])

    def test_snapshot_deletion_requires_all_current_ids_and_uses_mount_not_date(self):
        with patch.object(module, "_volume", return_value=self.volume):
            with self.assertRaises(CleanMacError):
                self.call("snapshots", "plan", roots=[str(self.root)], selected=self.snapshot_names[:1], config={"operation": "delete-volume-snapshots"})
            plan = self.call("snapshots", "plan", roots=[str(self.root)], selected=list(self.snapshot_names), config={"operation": "delete-volume-snapshots"})
            receipt = self.call("snapshots", "apply", plan=plan)
        self.assertEqual(receipt["status"], "completed")
        self.assertIn(["/usr/bin/tmutil", "deletelocalsnapshots", str(self.root)], self.commands)
        self.assertEqual(receipt["result"]["remaining_snapshots"], [])

    def test_active_backup_blocks_snapshot_plan_and_apply(self):
        with patch.object(module, "_volume", return_value=self.volume):
            plan = self.call("snapshots", "plan", roots=[str(self.root)], config={"operation": "thin", "bytes": 1000})
            self.backup_running = True
            with self.assertRaises(CleanMacError):
                self.call("snapshots", "apply", plan=plan)
            with self.assertRaises(CleanMacError):
                self.call("snapshots", "plan", roots=[str(self.root)], config={"operation": "thin", "bytes": 1000})
        self.assertFalse(any(argv[1] == "thinlocalsnapshots" for argv in self.commands))

    def test_changed_snapshot_set_blocks_action(self):
        with patch.object(module, "_volume", return_value=self.volume):
            plan = self.call("snapshots", "plan", roots=[str(self.root)], config={"operation": "thin", "bytes": 1000})
            self.snapshot_names.append("com.apple.TimeMachine.2026-11-13-130000.local")
            with self.assertRaises(CleanMacError):
                self.call("snapshots", "apply", plan=plan)
        self.assertFalse(any(argv[1] == "thinlocalsnapshots" for argv in self.commands))

    def test_unknown_backup_state_blocks_thinning(self):
        def unknown(argv, **kwargs):
            if argv == ["/usr/bin/tmutil", "status"]:
                return output("Unexpected diagnostic")
            return self.native(argv, **kwargs)
        with patch.object(module, "_run", side_effect=unknown), patch.object(module, "_volume", return_value=self.volume):
            report = self.call("snapshots", roots=[str(self.root)])
            self.assertIsNone(report["backup_running"])
            with self.assertRaises(CleanMacError):
                self.call("snapshots", "plan", roots=[str(self.root)], config={"operation": "thin", "bytes": 1000})

    def test_disk_permission_diagnosis_never_repairs(self):
        report = self.call("disk-permissions", roots=[str(self.root)])
        self.assertEqual(report["repair_status"], "unsupported")
        self.assertEqual(self.commands, [])

    def test_mail_handoff_requires_mailbox_scope_and_records_cancel(self):
        with self.assertRaises(CleanMacError):
            self.call("mail-rebuild")
        config = {"operation": "open-mail", "account": "Personal", "mailbox": "Archive", "account_kind": "local-only", "network_state": "offline"}
        plan = self.call("mail-rebuild", "plan", config=config)
        receipt = self.call("mail-rebuild", "apply", plan=plan)
        self.assertEqual(self.commands, [["/usr/bin/open", "-b", "com.apple.mail"]])
        verified = self.call("mail-rebuild", "verify", record=receipt["id"], config={"outcome": "cancelled"})
        self.assertEqual(verified["outcome"], "reported-unchanged")
        self.assertFalse(verified["verified"])

    def test_background_inventory_distinguishes_missing_and_malformed(self):
        home, directory, target = self.make_job(exists=False)
        (directory / "bad.plist").write_bytes(b"not a plist")
        (directory / "link.plist").symlink_to(target)
        with patch.object(module.Path, "home", return_value=home):
            report = self.call("background-items", roots=[str(directory)])
        self.assertEqual(len(report["broken_targets"]), 1)
        self.assertEqual(len(report["errors"]), 1)
        malformed = next(value for value in report["items"] if value["path"].endswith("bad.plist"))
        self.assertEqual(malformed["target_state"], "unknown")
        self.assertFalse(malformed["eligible"])

    def test_background_disable_enable_preserves_running_state_and_plist(self):
        home, directory, target = self.make_job()
        original = target.read_bytes()
        with patch.object(module.Path, "home", return_value=home), patch.object(module, "_dependency_paths", return_value=set()):
            report = self.call("background-items", roots=[str(directory)])
            identifier = report["items"][0]["id"]
            for operation in ("disable", "enable"):
                plan = self.call("background-items", "plan", roots=[str(directory)], selected=[identifier], config={"operation": operation})
                receipt = self.call("background-items", "apply", plan=plan)
                self.assertEqual(receipt["status"], "completed")
                self.assertFalse(receipt["result"]["current_state_changed"])
                self.assertFalse(receipt["result"]["process_start_stop_requested"])
        self.assertFalse(self.disabled)
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse(any(argv[1] in ("bootout", "bootstrap", "kill", "unload", "load") for argv in self.commands))

    def test_apple_and_current_dependency_cannot_be_disabled(self):
        home, directory, target = self.make_job(label="com.apple.fixture")
        with patch.object(module.Path, "home", return_value=home):
            report = self.call("background-items", roots=[str(directory)])
            with self.assertRaises(CleanMacError):
                self.call("background-items", "plan", roots=[str(directory)], selected=[report["items"][0]["id"]], config={"operation": "disable"})
        home, directory, target = self.make_job()
        with patch.object(module.Path, "home", return_value=home), patch.object(module, "_dependency_paths", return_value={str(self.root / "custom-worker")}):
            report = self.call("background-items", roots=[str(directory)])
            with self.assertRaises(CleanMacError):
                self.call("background-items", "plan", roots=[str(directory)], selected=[report["items"][0]["id"]], config={"operation": "disable"})

    def test_changed_job_stops_apply(self):
        home, directory, target = self.make_job()
        with patch.object(module.Path, "home", return_value=home), patch.object(module, "_dependency_paths", return_value=set()):
            report = self.call("background-items", roots=[str(directory)])
            plan = self.call("background-items", "plan", roots=[str(directory)], selected=[report["items"][0]["id"]], config={"operation": "disable"})
            target.write_bytes(plistlib.dumps({"Label": "com.example.other", "Program": str(self.root / "custom-worker")}))
            with self.assertRaises(CleanMacError):
                self.call("background-items", "apply", plan=plan)
        self.assertFalse(any(argv[1] == "disable" for argv in self.commands))

    def test_cancelled_inspection_runs_no_more_probes(self):
        result = module.dispatch("background-items", "inspect", {"roots": [str(self.root)]}, store=self.store, cancel=lambda: True)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.commands, [])

    def test_nonmac_runs_no_commands(self):
        with patch.object(module.sys, "platform", "linux"):
            result = self.call("dns", config={"hostname": "example.test"})
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(self.commands, [])

    def test_volume_identity_parser_uses_native_uuid(self):
        report = module._volume(str(self.root))
        self.assertEqual(report["volume_uuid"], self.volume["volume_uuid"])
        self.assertEqual(report["device"], self.root.stat().st_dev)

    def test_symlink_root_not_followed(self):
        link = self.root / "link"
        link.symlink_to(self.root, target_is_directory=True)
        result = self.call("purgeable", roots=[str(link)])
        self.assertEqual(result["volumes"], [])
        self.assertEqual(result["status"], "partial")


if __name__ == "__main__":
    unittest.main()

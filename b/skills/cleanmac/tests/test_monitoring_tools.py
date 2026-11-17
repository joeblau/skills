"""Monitoring fixtures never inspect real processes, volumes or personal files."""
import json
import os
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cleanmac_core import monitoring_tools as monitoring
from cleanmac_core import native_tools
from cleanmac_core.common import CleanMacError
from cleanmac_core.state import StateStore


def output(value):
    if isinstance(value, str):
        value = value.encode()
    return {"status": "available", "data": value}


def psline(pid=123, start="Fri Nov 13 10:00:00 2026", cpu="0:01.50", rss=2048, name="Compiler", uid=None):
    return f"{pid} 1 {os.getuid() if uid is None else uid} {start} {cpu} {rss} R {name}\n"


VM = b"""Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free: 100.
Pages active: 300.
Pages inactive: 40.
Pages wired down: 20.
Pages occupied by compressor: 10.
Pages stored in compressor: 30.
File-backed pages: 200.
Pages purgeable: 2.
"""


def netstat(incoming=1000, outgoing=2000, link=4, *, vpn=True):
    text = f"Name Mtu Network Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll\nen0 1500 <Link#{link}> aa:bb:cc:dd:ee:ff 10 0 {incoming} 20 0 {outgoing} 0\nen0 1500 192.0.2 192.0.2.1 10 - {incoming} 20 - {outgoing} -\n"
    if vpn:
        text += "utun0 1380 <Link#9> 8 0 800 7 0 700 0\n"
    return text


def disk(**extra):
    return plistlib.dumps({"DeviceIdentifier": "disk3s1", "VolumeUUID": "test-volume", "MountPoint": "/Volumes/Test",
                          "TotalSize": 100000, "VolumeFreeSpace": 5000, "Internal": False,
                          "APFSContainerReference": "disk3", "APFSContainerSize": 200000,
                          "APFSContainerFree": 10000, "SerialNumber": "DO-NOT-RETURN", **extra})


def battery(**extra):
    return plistlib.dumps([{"BatteryInstalled": True, "IORegistryEntryID": 12345,
                           "CurrentCapacity": 60, "MaxCapacity": 100,
                           "CycleCount": 140, "ExternalConnected": False, "IsCharging": False,
                           "SerialNumber": "SECRET-SERIAL", **extra}])


POWER = json.dumps({"SPPowerDataType": [{"sppower_battery_health_info": {
    "sppower_battery_health": "Good", "sppower_battery_maximum_capacity": "92%"},
    "sppower_battery_model_info": {"sppower_battery_serial_number": "PRIVATE"}}]}).encode()


class ProbeTests(unittest.TestCase):
    def test_probe_reports_output_without_stderr(self):
        result = monitoring._probe([sys.executable, "-c", "import sys; print('ok'); print('private error',file=sys.stderr)"])
        self.assertEqual(result["data"], b"ok\n")

    def test_probe_output_bound_kills_only_probe(self):
        result = monitoring._probe([sys.executable, "-c", "import os; os.write(1,b'x'*10000)"], max_output=100)
        self.assertEqual(result["code"], "command_output_limit")
        self.assertNotIn("data", result)

    def test_probe_has_bounded_timeout(self):
        result = monitoring._probe([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.05)
        self.assertEqual(result["code"], "command_timeout")

    def test_cancel_before_probe_does_not_launch(self):
        with patch.object(monitoring.subprocess, "Popen", side_effect=AssertionError("must not launch")):
            self.assertEqual(monitoring._probe(["/not-a-program"], cancel=lambda: True)["status"], "cancelled")

    def test_probe_failed_command_does_not_expose_output(self):
        result = monitoring._probe([sys.executable, "-c", "import sys; print('secret'); sys.exit(4)"])
        self.assertEqual(result["exit_code"], 4)
        self.assertNotIn("data", result)


class FixtureBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cleanmac-monitoring-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = StateStore(self.root / "state")
        self.darwin = patch.object(monitoring.platform, "system", return_value="Darwin")
        self.darwin.start()
        self.addCleanup(self.darwin.stop)

    def inspect(self, name, configuration=None, roots=None, **kwargs):
        return monitoring.dispatch(name, "inspect", {"configuration": configuration or {}, "roots": roots or [], **kwargs}, store=self.store)



class MonitoringTests(FixtureBase):
    def test_non_macos_runs_no_commands(self):
        with patch.object(monitoring.platform, "system", return_value="Linux"), patch.object(monitoring, "_probe", side_effect=AssertionError("no probe")):
            with self.assertRaises(CleanMacError) as error:
                self.inspect("memory")
        self.assertEqual(error.exception.code, "unsupported_platform")

    def test_unknown_action_rejected_before_probe(self):
        with patch.object(monitoring, "_probe", side_effect=AssertionError("no probe")):
            with self.assertRaises(CleanMacError):
                monitoring.dispatch("cpu", "kill", {}, store=self.store)

    def test_specs_have_unique_names_and_mutations_are_separate_tools(self):
        names = [spec["name"] for spec in monitoring.TOOL_SPECS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual([spec["name"] for spec in monitoring.TOOL_SPECS if "apply" in spec["actions"]], ["app-process", "network-speed"])

    def test_cpu_sample_uses_time_delta_and_pid_start_identity(self):
        first = psline(cpu="0:01.50") + psline(200, cpu="0:08.00")
        second = psline(cpu="0:03.50") + psline(200, start="Fri Nov 13 10:00:01 2026", cpu="0:10.00")
        with patch.object(monitoring, "_probe", side_effect=[output(first), output(second)]) as probe, patch.object(monitoring, "_pause", return_value=True), patch.object(monitoring, "_clock", side_effect=[0, 2]), patch.object(monitoring.os, "getloadavg", return_value=(1, 2, 3)):
            report = self.inspect("cpu")
        self.assertEqual(report["items"][0]["interval_cpu_percent"], 100)
        self.assertIsNone(report["items"][1]["interval_cpu_percent"])
        self.assertEqual(report["items"][0]["resident_bytes"], 2048 * 1024)
        self.assertEqual(report["items"][0]["unresponsive"], "unknown")
        self.assertTrue(all(call.args[0] == monitoring._PS for call in probe.call_args_list))
        self.assertFalse(report["cleanup_performed"])

    def test_cpu_parse_bounded_name_and_invalid_records_are_partial(self):
        data = psline(name="An App\x1b[31m") + "malformed process\n"
        items, invalid = monitoring._parse_processes(data.encode())
        self.assertEqual(invalid, 1)
        self.assertIn("\\u001b", items[0]["name"])
        self.assertNotIn("\x1b", items[0]["name"])

    def test_cpu_protects_system_agent_and_ancestors(self):
        first = psline(pid=os.getpid(), cpu="0:00.00") + psline(1, uid=0, name="launchd")
        with patch.object(monitoring, "_probe", side_effect=[output(first), output(first)]), patch.object(monitoring, "_pause", return_value=True):
            report = self.inspect("cpu")
        self.assertTrue(all(item["protected"] for item in report["items"]))

    def test_memory_runtime_page_size_and_pressure_and_swap(self):
        values = [output("17179869184\n"), output(VM), output("total = 1024.00M used = 128.25M free = 895.75M (encrypted)\n"), output("2\n"), output(psline())]
        with patch.object(monitoring, "_probe", side_effect=values):
            report = self.inspect("memory")
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["measurements"]["vm"]["bytes"]["compressed_storage"], 10 * 16384)
        self.assertEqual(report["measurements"]["pressure"]["level"], "warning")
        self.assertEqual(report["measurements"]["swap"]["used_bytes"], int(128.25 * 1024 ** 2))

    def test_unknown_pressure_does_not_become_healthy(self):
        values = [output("1024"), output(VM), output("total = 0M used = 0M free = 0M"), output("99"), output(psline())]
        with patch.object(monitoring, "_probe", side_effect=values):
            report = self.inspect("memory")
        self.assertEqual(report["measurements"]["pressure"]["status"], "unknown")
        self.assertEqual(report["measurements"]["pressure"]["level"], "unknown")

    def test_failed_memory_probe_is_unavailable_not_zero(self):
        with patch.object(monitoring, "_probe", return_value={"status": "unavailable", "code": "command_failed"}):
            report = self.inspect("memory")
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["measurements"]["physical"]["status"], "unavailable")
        self.assertIsNone(report["measurements"]["physical"]["value"])

    def test_memory_cancellation_stops_following_probes(self):
        with patch.object(monitoring, "_probe", return_value={"status": "cancelled", "code": "cancelled"}) as probe:
            report = self.inspect("memory")
        self.assertEqual(report["status"], "cancelled")
        self.assertEqual(probe.call_count, 1)

    def test_vm_malformed_page_size_is_rejected(self):
        with self.assertRaises(ValueError):
            monitoring._parse_vm(b"Pages free: 100.")

    def test_disk_shared_capacity_never_summed_or_health_fabricated(self):
        with patch.object(monitoring, "_probe", side_effect=[output(disk()), output(disk(DeviceIdentifier="disk3s2", VolumeUUID="different"))]):
            report = self.inspect("disks", roots=["/", "/Volumes/Other"])
        self.assertEqual(len(report["items"]), 2)
        self.assertIsNone(report["measurements"]["total_across_volumes_bytes"])
        self.assertTrue(report["items"][0]["low_space"])
        self.assertEqual(report["items"][0]["available_percent"], 5)
        self.assertEqual(report["items"][0]["smart_status"], "unknown")
        self.assertIsNone(report["items"][0]["health_percent"])
        self.assertNotIn("DO-NOT-RETURN", json.dumps(report))

    def test_disk_replacement_changes_measurement_identity(self):
        first = monitoring._disk_info(disk(), "/")
        second = monitoring._disk_info(disk(VolumeUUID="replaced"), "/")
        self.assertNotEqual(first["id"], second["id"])

    def test_disk_malformed_response_not_empty_success(self):
        with patch.object(monitoring, "_probe", return_value=output(plistlib.dumps({}))):
            report = self.inspect("disks")
        self.assertTrue(report["errors"])
        self.assertEqual(report["items"], [])

    def test_battery_only_emits_whitelisted_fields(self):
        with patch.object(monitoring, "_probe", side_effect=[output(battery()), output(POWER)]):
            report = self.inspect("battery")
        self.assertEqual(report["items"][0]["charge_percent"], 60)
        self.assertEqual(report["items"][0]["condition"], "Good")
        self.assertEqual(report["items"][0]["maximum_capacity_percent"], 92)
        self.assertEqual(report["measurements"]["drain"], "not_measured")
        self.assertNotIn("SECRET-SERIAL", json.dumps(report))
        self.assertNotIn("PRIVATE", json.dumps(report))

    def test_desktop_absent_battery_not_zero_health(self):
        with patch.object(monitoring, "_probe", return_value=output(plistlib.dumps([]))) as probe:
            report = self.inspect("battery")
        self.assertEqual(report["items"], [])
        self.assertEqual(report["measurements"]["internal_battery"], "not_reported")
        self.assertEqual(probe.call_count, 1)

    def test_battery_charging_transition_never_generates_drain_alert(self):
        with patch.object(monitoring, "_probe", side_effect=[output(battery()), output(POWER), output(battery(CurrentCapacity=59, ExternalConnected=True, IsCharging=True))]), patch.object(monitoring, "_pause", return_value=True), patch.object(monitoring, "_clock", side_effect=[0, 5]):
            report = self.inspect("battery", {"sample_seconds": 5})
        self.assertIsNone(report["items"][0]["observed_discharge_percentage_points_per_hour"])
        self.assertEqual(report["items"][0]["drain_status"], "unknown-or-power-transition")

    def test_battery_replacement_is_not_drain(self):
        with patch.object(monitoring, "_probe", side_effect=[output(battery()), output(POWER), output(battery(CurrentCapacity=20, IORegistryEntryID=777))]), patch.object(monitoring, "_pause", return_value=True):
            report = self.inspect("battery", {"sample_seconds": 1})
        self.assertIsNone(report["items"][0]["observed_discharge_percentage_points_per_hour"])

    def test_network_deduplicates_per_address_and_labels_vpn(self):
        parsed = monitoring._parse_interfaces(netstat().encode())
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed["utun0"]["kind"], "tunnel/VPN")
        self.assertEqual(parsed["en0"]["received_bytes"], 1000)
        self.assertNotIn("192.0.2.1", json.dumps(parsed))
        self.assertNotIn("aa:bb", json.dumps(parsed))

    def test_network_counter_reset_and_changed_identity_stay_unknown(self):
        before = monitoring._parse_interfaces(netstat().encode())
        reset = monitoring._parse_interfaces(netstat(100, 200).encode())
        item = monitoring._network_delta(before, reset, 1)[0]
        self.assertIsNone(item["received_bytes_per_second"])
        changed = monitoring._parse_interfaces(netstat(1100, 2100, link=5).encode())
        self.assertIsNone(monitoring._network_delta(before, changed, 1)[0]["sent_bytes_per_second"])

    def test_network_only_runs_local_counter_commands(self):
        with patch.object(monitoring, "_probe", side_effect=[output(netstat()), output(netstat(1200, 2400))]) as probe, patch.object(monitoring, "_pause", return_value=True), patch.object(monitoring, "_clock", side_effect=[0, 2]):
            report = self.inspect("network")
        self.assertEqual(report["items"][0]["received_bytes_per_second"], 100)
        self.assertEqual(report["items"][0]["sent_bytes_per_second"], 200)
        self.assertIsNone(report["measurements"]["total_bytes_per_second"])
        self.assertEqual(report["measurements"]["network_requests"], 0)
        self.assertTrue(all(call.args[0] == monitoring._NETSTAT for call in probe.call_args_list))

    def test_devices_removed_mid_scan_are_partial_and_serials_omitted(self):
        listing = plistlib.dumps({"AllDisks": ["disk3", "disk3s1"]})
        with patch.object(monitoring, "_probe", side_effect=[output(listing), output(disk(DeviceIdentifier="disk3")), {"status": "unavailable", "code": "command_failed"}]):
            report = self.inspect("devices")
        self.assertEqual(report["status"], "partial")
        self.assertNotIn("DO-NOT-RETURN", json.dumps(report))
        self.assertEqual(report["measurements"]["bluetooth_battery"], "unsupported")

    def test_device_identifier_cannot_inject_diskutil_option(self):
        with patch.object(monitoring, "_probe", return_value=output(plistlib.dumps({"AllDisks": ["--erase"]}))) as probe:
            report = self.inspect("devices")
        self.assertEqual(probe.call_count, 1)
        self.assertTrue(report["errors"])

    def test_health_uses_receipt_evidence_and_no_score(self):
        self.store.put("receipts", {"id": "receipt-example", "status": "needs-recovery", "summary": {"selected_count": 2, "trashed_count": 1, "verified_trashed_count": 0, "trash_retained_bytes_estimate": 100}, "measured_free_space_delta": -500})
        cpu = {**monitoring._report("cpu"), "items": [{"pid": 123}]}
        memory = {**monitoring._report("memory"), "measurements": {"pressure": {"level": "critical"}}}
        disks = {**monitoring._report("disks"), "items": [{"id": "volume-one", "low_space": True, "available_percent": 5, "low_space_threshold_percent": 10}]}
        with patch.object(monitoring, "_cpu", return_value=cpu), patch.object(monitoring, "_memory", return_value=memory), patch.object(monitoring, "_disks", return_value=disks), patch.object(monitoring, "_battery", return_value=monitoring._report("battery")):
            report = self.inspect("health")
        ledger = report["measurements"]["activity"]
        self.assertEqual(ledger["moved_to_trash_operations"], 1)
        self.assertEqual(ledger["verified_trash_operations"], 0)
        self.assertEqual(ledger["pending_recovery_receipts"], ["receipt-example"])
        self.assertIsNone(ledger["reclaimed_bytes"])
        self.assertIsNone(report["measurements"]["score"])
        self.assertEqual(report["measurements"]["malware_status"], "not_scanned")
        self.assertEqual(len(report["recommendations"]), 2)

    def test_invalid_options_rejected_before_commands(self):
        with patch.object(monitoring, "_probe", side_effect=AssertionError("No probe")):
            for config in ({"sample_seconds": True}, {"sample_seconds": float("nan")}, {"sample_seconds": 100}):
                with self.subTest(config=config), self.assertRaises(CleanMacError):
                    self.inspect("cpu", config)

    def test_network_speed_requires_exact_plan_and_never_runs_during_plan(self):
        identity = {"device": 1, "inode": 1, "size": 100, "mtime_ns": 1}
        with patch.object(monitoring, "_network_executable_identity", return_value=identity), patch.object(monitoring, "_probe", side_effect=AssertionError("No transfer during plan")):
            plan = monitoring.dispatch("network-speed", "plan", {"configuration": {"max_seconds": 5}}, store=self.store)
            with self.assertRaises(CleanMacError):
                monitoring.dispatch("network-speed", "apply", {"record_id": plan["id"], "approve": "wrong"}, store=self.store)
        self.assertIn("not capped", plan["summary"]["data_usage"])
        with patch.object(monitoring, "_network_executable_identity", return_value=identity), patch.object(monitoring, "_probe", return_value=output(json.dumps({"dl_throughput": 1000000, "ul_throughput": 500000, "interface_name": "private", "extra": "secret"}))) as probe:
            receipt = monitoring.dispatch("network-speed", "apply", {"record_id": plan["id"], "approve": plan["digest"]}, store=self.store)
            replay = monitoring.dispatch("network-speed", "apply", {"record_id": plan["id"], "approve": plan["digest"]}, store=self.store)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(probe.call_args.args[0], ["/usr/bin/networkQuality", "-c", "-M", "5"])
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(replay["id"], receipt["id"])
        self.assertNotIn("secret", json.dumps(receipt["result"]))

    def test_network_speed_changed_executable_prevents_transfer(self):
        identity = {"device": 1, "inode": 1, "size": 100, "mtime_ns": 1}
        with patch.object(monitoring, "_network_executable_identity", return_value=identity):
            plan = monitoring.dispatch("network-speed", "plan", {}, store=self.store)
        with patch.object(monitoring, "_network_executable_identity", return_value={**identity, "inode": 2}), patch.object(monitoring, "_probe", side_effect=AssertionError("No changed executable")):
            with self.assertRaises(CleanMacError):
                monitoring.dispatch("network-speed", "apply", {"record_id": plan["id"], "approve": plan["digest"]}, store=self.store)

    def test_network_speed_ambiguous_result_freezes_retry(self):
        identity = {"device": 1, "inode": 1, "size": 100, "mtime_ns": 1}
        with patch.object(monitoring, "_network_executable_identity", return_value=identity):
            plan = monitoring.dispatch("network-speed", "plan", {}, store=self.store)
            with patch.object(monitoring, "_probe", return_value={"status": "unavailable", "code": "command_timeout"}) as probe:
                first = monitoring.dispatch("network-speed", "apply", {"record_id": plan["id"], "approve": plan["digest"]}, store=self.store)
                second = monitoring.dispatch("network-speed", "apply", {"record_id": plan["id"], "approve": plan["digest"]}, store=self.store)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.store.list("receipts")[0]["status"], "recovery_needed")


class FileViewsTests(FixtureBase):
    def folder(self):
        path = self.root / "chosen"
        path.mkdir()
        return path

    def test_large_old_file_filters_never_autoselect(self):
        path = self.folder()
        old = path / "old.zip"
        old.write_bytes(b"abcde")
        os.utime(old, (1, 1))
        (path / "new.zip").write_bytes(b"abcdef")
        with patch.object(monitoring, "_probe", side_effect=AssertionError("No system commands")):
            report = self.inspect("large-old-files", {"min_bytes": 1, "older_than_days": 1}, roots=[str(path)])
        self.assertEqual([Path(item["path"]).name for item in report["items"]], ["old.zip"])
        self.assertFalse(report["items"][0]["selected"])
        self.assertIsNone(report["items"][0]["last_used_at"])
        self.assertEqual(report["next_action"]["scan_id"], report["scan_id"])

    def test_source_origins_omit_credentials_paths_and_tokens(self):
        data = plistlib.dumps(["https://alice:password@example.com/private?token=SECRET#fragment", "https://example.com/another", "file:///private/file", "javascript:alert(1)"])
        self.assertEqual(monitoring._source_origins(data), ["https://example.com"])

    def test_darwin_origin_metadata_is_descriptor_bound_and_bounded(self):
        value = plistlib.dumps(["https://example.com/download?token=private"])

        def get(fd, name, buffer, size, position, options):
            self.assertEqual(fd, 123)
            self.assertEqual(name, b"com.apple.metadata:kMDItemWhereFroms")
            self.assertEqual((position, options), (0, 0))
            if buffer is None:
                return len(value)
            self.assertEqual(size, len(value))
            monitoring.ctypes.memmove(buffer, value, len(value))
            return len(value)

        function = Mock(side_effect=get)
        with patch.object(monitoring, "os", SimpleNamespace()), patch.object(monitoring.ctypes, "CDLL", return_value=SimpleNamespace(fgetxattr=function)):
            result = monitoring._get_origin_xattr(123)
        self.assertEqual(monitoring._source_origins(result), ["https://example.com"])
        self.assertEqual(function.call_count, 2)

    def test_darwin_large_origin_metadata_is_rejected_before_read(self):
        function = Mock(return_value=70000)
        with patch.object(monitoring, "os", SimpleNamespace()), patch.object(monitoring.ctypes, "CDLL", return_value=SimpleNamespace(fgetxattr=function)):
            with self.assertRaises(OSError):
                monitoring._get_origin_xattr(123)
        self.assertEqual(function.call_count, 1)

    def test_darwin_changed_xattr_size_does_not_parse_truncated_value(self):
        function = Mock(side_effect=[10, 20])
        with patch.object(monitoring, "os", SimpleNamespace()), patch.object(monitoring.ctypes, "CDLL", return_value=SimpleNamespace(fgetxattr=function)):
            with self.assertRaises(OSError):
                monitoring._get_origin_xattr(123)

    def test_download_incomplete_protected_and_unknown_source_usable(self):
        path = self.folder()
        (path / "inflight.crdownload").write_bytes(b"123")
        (path / "finished.zip").write_bytes(b"12345")
        report = self.inspect("downloads", {"include_origin": False}, roots=[str(path)])
        by_name = {Path(item["path"]).name: item for item in report["items"]}
        self.assertTrue(by_name["inflight.crdownload"]["protected"])
        self.assertEqual(by_name["finished.zip"]["source_status"], "unknown")
        saved = self.store.get("scans", report["scan_id"])
        self.assertTrue(next(item for item in saved["items"] if item["path"].endswith(".crdownload"))["protected"])

    def test_download_view_of_saved_scan_does_not_rewrite_original(self):
        path = self.folder()
        (path / "inflight.part").write_bytes(b"1")
        original = self.inspect("large-old-files", {"min_bytes": 0}, roots=[str(path)])
        before = self.store.get("scans", original["scan_id"])
        result = self.inspect("downloads", {"include_origin": False}, record_id=original["scan_id"])
        self.assertNotEqual(result["scan_id"], original["scan_id"])
        self.assertEqual(self.store.get("scans", original["scan_id"]), before)

    def test_storage_map_counts_hardlink_once_and_unknown_package(self):
        path = self.folder()
        (path / "file").write_bytes(b"12345")
        os.link(path / "file", path / "alias")
        app = path / "Private.app"
        app.mkdir()
        (app / "inside").write_bytes(b"unscanned")
        report = self.inspect("storage-map", roots=[str(path)])
        root = report["items"][0]
        self.assertEqual(root["logical_bytes"], 5)
        self.assertEqual(root["hardlink_aliases"], 1)
        self.assertEqual(root["unknown_subtrees"], 1)
        self.assertIsNone(root["reclaimable_bytes"])

    def test_storage_map_saved_drilldown_has_breadcrumbs_and_escaped_html(self):
        path = self.folder()
        name = '<script>alert("bad")</script>'
        # Slash is a path separator; an HTML-bearing ordinary basename suffices.
        name = '<img src=x onerror=alert(1)>'
        (path / name).write_bytes(b"123")
        first = self.inspect("storage-map", roots=[str(path)])
        report = self.inspect("storage-map", {"path": str(path), "format": "html"}, record_id=first["scan_id"])
        self.assertEqual(report["breadcrumbs"][0]["path"], str(path))
        self.assertIn("&lt;img", report["html"])
        self.assertNotIn("<img", report["html"])
        self.assertIn("default-src 'none'", report["html"])
        self.assertEqual(report["items"][0]["logical_bytes"], 3)

    def test_hidden_toggle_keeps_totals_honest(self):
        path = self.folder()
        (path / ".hidden").write_bytes(b"12345")
        (path / "visible").write_bytes(b"12")
        first = self.inspect("storage-map", roots=[str(path)])
        report = self.inspect("storage-map", {"path": str(path), "include_hidden": False}, record_id=first["scan_id"])
        self.assertEqual(first["items"][0]["logical_bytes"], 7)
        self.assertEqual([Path(item["path"]).name for item in report["items"]], ["visible"])
        self.assertTrue(report["measurements"]["totals_include_hidden"])

    def test_drilldown_cannot_escape_saved_scope(self):
        path = self.folder()
        first = self.inspect("storage-map", roots=[str(path)])
        with self.assertRaises(CleanMacError):
            self.inspect("storage-map", {"path": "/"}, record_id=first["scan_id"])

    def test_no_implicit_folder_scan(self):
        with self.assertRaises(CleanMacError):
            self.inspect("downloads")

    def test_foreign_saved_scan_is_rejected_with_actionable_error(self):
        self.store.put("scans", {"id": "scan-backups", "status": "complete", "roots": [str(self.root)], "items": [{"id": "backup-one", "path": str(self.root / "backup"), "kind": "backup"}], "errors": []})
        for name in ("downloads", "large-old-files", "storage-map"):
            with self.subTest(name=name), self.assertRaises(CleanMacError) as error:
                self.inspect(name, record_id="scan-backups")
            self.assertEqual(error.exception.code, "invalid_scan")

    def test_overlapping_roots_present_one_top_level_total(self):
        path = self.folder()
        child = path / "child"
        child.mkdir()
        (child / "file").write_bytes(b"123")
        report = self.inspect("storage-map", roots=[str(path), str(child)])
        self.assertEqual(len(report["items"]), 1)
        self.assertEqual(report["items"][0]["logical_bytes"], 3)
        self.assertEqual(report["measurements"]["overlapping_roots_shown_within_parent"], [str(child)])


class AppProcessTests(FixtureBase):
    def setUp(self):
        super().setUp()
        self.identity = {"pid": 1234, "launch_date": 1794570000.25, "bundle_id": "example.Editor",
                         "bundle_path": "/Applications/Example.app", "executable_path": "/Applications/Example.app/Contents/MacOS/Example",
                         "uid": os.getuid(), "process_start_sec": 1794570000, "process_start_usec": 250000, "activation_policy": 0,
                         "code_signature": {"valid": True, "cdhash": "a" * 40, "team_id": "TESTTEAM", "identifier": "example.Editor"}}
        self.row = {"id": "a" * 64, "identity": self.identity, "protected": False, "protection_reasons": [], "can_force_quit": True}
        self.handshake = patch.object(native_tools, "_handshake", return_value={"capabilities": ["app-process"]})
        self.handshake.start()
        self.addCleanup(self.handshake.stop)

    def response(self, command, payload, **kwargs):
        if command == "apps-inspect":
            return {"status": "complete", "items": [self.row], "errors": []}
        if command == "apps-validate":
            return {"status": "complete", "eligible": True, "identity": payload["expected"], "action": payload["action"]}
        if command == "apps-act":
            self.assertIs(payload["approved"], True)
            result = {"status": "completed", "prior_identity": payload["expected"]}
            if payload["action"] == "relaunch":
                result["new_identity"] = {**self.identity, "pid": 4321, "launch_date": 1794571000.25, "process_start_sec": 1794571000}
            return result
        self.fail("Unexpected command " + command)

    def plan(self, operation="quit", *, receipt=None):
        if receipt:
            options = {"record_id": receipt["id"], "configuration": {"operation": operation}}
        else:
            record = self.inspect("app-process")
            options = {"record_id": record["id"], "selected": [self.row["id"]], "configuration": {"operation": operation}}
        return monitoring.dispatch("app-process", "plan", options, store=self.store)

    def apply(self, plan):
        return monitoring.dispatch("app-process", "apply", {"record_id": plan["id"], "approve": plan["digest"]}, store=self.store)

    def test_inspection_and_plan_are_read_only(self):
        with patch.object(native_tools, "_call", side_effect=self.response) as native:
            plan = self.plan()
        self.assertEqual([call.args[0] for call in native.call_args_list], ["apps-inspect", "apps-validate"])
        self.assertIn("save", plan["summary"]["consequences"])
        self.assertEqual(plan["action"], "quit")

    def test_missing_companion_capability_does_not_launch_app_command(self):
        with patch.object(native_tools, "_handshake", return_value={"capabilities": []}), patch.object(native_tools, "_call", side_effect=AssertionError("No native action")):
            with self.assertRaises(CleanMacError) as error:
                self.inspect("app-process")
        self.assertEqual(error.exception.code, "native_helper_incompatible")

    def test_protected_agent_or_system_app_cannot_be_planned(self):
        self.row = {**self.row, "protected": True, "protection_reasons": ["current-or-ancestor-process"]}
        with patch.object(native_tools, "_call", side_effect=self.response) as native:
            with self.assertRaises(CleanMacError) as error:
                self.plan()
        self.assertEqual(error.exception.code, "protected_app_process")
        self.assertEqual(native.call_count, 1)

    def test_force_quit_requires_distinct_plan_and_exposes_loss_consequence(self):
        with patch.object(native_tools, "_call", side_effect=self.response) as native:
            plan = self.plan("force-quit")
            receipt = self.apply(plan)
        self.assertEqual(receipt["status"], "completed")
        self.assertIn("Unsaved work may be permanently lost", plan["summary"]["consequences"])
        actions = [call.args[1]["action"] for call in native.call_args_list if call.args[0] == "apps-act"]
        self.assertEqual(actions, ["force-quit"])

    def test_unsupported_native_force_quit_cannot_be_planned(self):
        self.row = {**self.row, "can_force_quit": False}
        with patch.object(native_tools, "_call", side_effect=self.response):
            with self.assertRaises(CleanMacError):
                self.plan("force-quit")

    def test_digest_mismatch_never_calls_native_apply(self):
        with patch.object(native_tools, "_call", side_effect=self.response):
            plan = self.plan()
        with patch.object(native_tools, "_call", side_effect=AssertionError("No action before approval")):
            with self.assertRaises(CleanMacError):
                monitoring.dispatch("app-process", "apply", {"record_id": plan["id"], "approve": "wrong"}, store=self.store)

    def test_apply_revalidates_and_replay_does_not_repeat_action(self):
        with patch.object(native_tools, "_call", side_effect=self.response):
            plan = self.plan()
        with patch.object(native_tools, "_call", side_effect=self.response) as native:
            first = self.apply(plan)
            second = self.apply(plan)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual([call.args[0] for call in native.call_args_list], ["apps-validate", "apps-act"])

    def test_reused_pid_cannot_receive_old_approval(self):
        with patch.object(native_tools, "_call", side_effect=self.response):
            plan = self.plan()
        changed = {**self.identity, "process_start_usec": 777777}
        with patch.object(native_tools, "_call", return_value={"status": "complete", "eligible": True, "identity": changed, "action": "quit"}) as native:
            with self.assertRaises(CleanMacError) as error:
                self.apply(plan)
        self.assertEqual(error.exception.code, "app_identity_changed")
        self.assertEqual(native.call_count, 1)
        self.assertFalse(self.store.list("receipts")[0]["execution_started"])

    def test_normal_quit_pending_save_dialog_never_forces_or_retries(self):
        with patch.object(native_tools, "_call", side_effect=self.response):
            plan = self.plan()

        def pending(command, payload, **kwargs):
            if command == "apps-act":
                self.assertEqual(payload["action"], "quit")
                raise CleanMacError("App is showing a save dialog.", "termination_pending")
            return self.response(command, payload, **kwargs)

        with patch.object(native_tools, "_call", side_effect=pending) as native:
            first = self.apply(plan)
            second = self.apply(plan)
        self.assertEqual(first["status"], "recovery_needed")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual([call.args[0] for call in native.call_args_list], ["apps-validate", "apps-act"])

    def test_relaunch_requires_verified_quit_receipt_and_original_signature(self):
        with patch.object(native_tools, "_call", side_effect=self.response) as native:
            quit_plan = self.plan()
            quit_receipt = self.apply(quit_plan)
            relaunch = self.plan("relaunch", receipt=quit_receipt)
            result = self.apply(relaunch)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["new_identity"]["pid"], 4321)
        self.assertEqual(relaunch["payload"]["source_receipt_id"], quit_receipt["id"])
        self.assertEqual([call.args[1]["action"] for call in native.call_args_list if call.args[0] == "apps-act"], ["quit", "relaunch"])

    def test_ordinary_scan_cannot_authorize_relaunch(self):
        with patch.object(native_tools, "_call", side_effect=self.response):
            record = self.inspect("app-process")
        with self.assertRaises(CleanMacError):
            monitoring.dispatch("app-process", "plan", {"record_id": record["id"], "configuration": {"operation": "relaunch"}}, store=self.store)

    def test_receipt_tampering_blocks_relaunch(self):
        with patch.object(native_tools, "_call", side_effect=self.response):
            plan = self.plan()
            receipt = self.apply(plan)
        receipt["result"]["prior_identity"]["bundle_path"] = "/Applications/Another.app"
        self.store.put("receipts", receipt)
        with patch.object(native_tools, "_call", side_effect=AssertionError("No relaunch with tampered receipt")):
            with self.assertRaises(CleanMacError):
                self.plan("relaunch", receipt=receipt)

    def test_wrong_postlaunch_signature_is_unverified_and_not_repeated(self):
        with patch.object(native_tools, "_call", side_effect=self.response):
            receipt = self.apply(self.plan())
            plan = self.plan("relaunch", receipt=receipt)

        def altered(command, payload, **kwargs):
            result = self.response(command, payload, **kwargs)
            if command == "apps-act":
                result["new_identity"]["code_signature"] = {**self.identity["code_signature"], "cdhash": "b" * 40}
            return result

        with patch.object(native_tools, "_call", side_effect=altered) as native:
            result = self.apply(plan)
            self.apply(plan)
        self.assertEqual(result["status"], "recovery_needed")
        self.assertEqual(native.call_count, 2)


if __name__ == "__main__":
    unittest.main()

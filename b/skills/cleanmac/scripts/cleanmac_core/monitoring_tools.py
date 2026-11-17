"""Bounded macOS measurements and views over the shared local-file inventory.

Commands use documented read-only forms. No process arguments, hardware serial
numbers, packet payloads, or raw system-profiler output enter saved reports.
The optional network-quality test is a separately approved network operation.
"""
from __future__ import annotations

import datetime as dt
import copy
import ctypes
import errno
import html
import json
import math
import os
from pathlib import Path
import platform
import plistlib
import re
import selectors
import stat
import subprocess
import time
from urllib.parse import urlsplit
from xml.parsers.expat import ExpatError

from .common import CleanMacError, digest, display_path, new_id, utc_now


_MAX_OUTPUT = 4 * 1024 * 1024
_clock = time.monotonic
_PS = ["/bin/ps", "-A", "-c", "-o", "pid=,ppid=,uid=,lstart=,time=,rss=,state=,comm="]
_NETSTAT = ["/usr/sbin/netstat", "-ibn"]
_FILE_TOOLS = {"large-old-files", "downloads", "storage-map"}
_INCOMPLETE = (".crdownload", ".part", ".partial", ".download", ".tmp")
SOURCES = [
    "https://support.apple.com/guide/activity-monitor/actmntr1004/mac",
    "https://support.apple.com/guide/mac-help/mh20865/mac",
    "https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/kern_memorystatus_notify.c",
    "man:ps(1)", "man:vm_stat(1)", "man:netstat(1)", "man:ioreg(8)",
    "man:system_profiler(8)", "man:diskutil(8)", "man:networkQuality(8)",
]


def _spec(name, title, feature, description, config=None, *, actions=None, scope=False):
    return {"name": name, "title": title, "feature_ids": [feature],
            "actions": actions or ["inspect"], "description": description,
            "configuration": config or {}, "scope_required": scope,
            "requirements": ["macOS"], "status": "implemented"}


TOOL_SPECS = [
    _spec("cpu", "CPU and process activity", "CM-069", "Two process samples bound by PID and start time; high CPU is not evidence of a hung app.", {"sample_seconds": "0.1–5 seconds, default 1", "limit": "1–1000 process rows, default 20"}),
    {**_spec("app-process", "Quit or relaunch one selected app", "CM-069", "Native app identities, separate normal/force quit approval, and relaunch from a verified quit receipt. Critical, system and agent ancestor processes stay protected.", {"operation": "quit (default), force-quit, or relaunch. Relaunch uses a completed quit receipt as --record.", "pids": "Optional exact positive PID list for inspection; plans select one opaque native app ID."}, actions=["inspect", "plan", "apply"]), "requirements": ["macOS", "native-features-helper"], "approval_actions": ["apply"]},
    _spec("memory", "Memory pressure and resident memory", "CM-070", "VM page size, compressed memory, pressure, swap, and per-process RSS; no memory purge.", {"limit": "1–1000 process rows, default 20"}),
    _spec("disks", "Volume capacity and storage health", "CM-071", "Selected-volume metadata and available space; APFS shared capacity is not summed.", {"low_space_percent": "0–100 threshold, default 10"}),
    _spec("battery", "Battery condition and charge", "CM-072", "macOS-reported condition, cycle count, charge and optional bounded charge-change sample.", {"sample_seconds": "0–5 seconds, default 0; 0 leaves drain unknown"}),
    _spec("network", "Local network activity", "CM-073", "Per-interface byte deltas; no speed test, DNS lookup, packet capture, or network request.", {"sample_seconds": "0.1–5 seconds, default 1"}),
    _spec("network-speed", "Optional internet speed test", "CM-073", "Separately approve Apple's networkQuality service and uncapped transfer data before testing.", {"max_seconds": "5–30 seconds, default 15; runtime limit is not a byte limit"}, actions=["plan", "apply"]),
    _spec("devices", "Connected external storage", "CM-074", "External disk identity and capacity. Bluetooth/mobile battery telemetry remains unavailable; no pairing or connections."),
    _spec("health", "Explainable Mac health summary", "CM-075", "Measurements and receipt-backed activity; no synthetic health score or malware-clean claim.", {"sample_seconds": "0.1–5 seconds, default 1", "limit": "1–1000 process rows, default 20"}),
    _spec("large-old-files", "Large and old files", "CM-048", "Read-only filtered inventory; modification, creation and unknown last-used dates are distinct.", {"min_bytes": "Minimum logical size, default 52428800", "older_than_days": "Optional minimum modification age", "include_hidden": "Default true", "search": "Literal path substring", "offset": "Default 0", "limit": "1–1000, default 20"}, scope=True),
    _spec("downloads", "Downloads by metadata", "CM-051", "Selected folders, file type, dates and bounded source-origin metadata; incomplete downloads are protected.", {"include_origin": "Read source-origin extended attribute, default true", "file_type": "installer/archive/media/document/other", "older_than_days": "Optional minimum modification age", "include_hidden": "Default true", "search": "Literal path substring", "offset": "Default 0", "limit": "1–1000, default 20"}, scope=True),
    _spec("storage-map", "Storage map and folder drilldown", "CM-053", "Shared inventory with distinct allocation estimates, protected/unknown branches, hidden toggle and text or static HTML view.", {"path": "Exact directory path from the saved scan for drilldown", "include_hidden": "Default true; totals always include observed hidden entries", "search": "Literal path substring", "format": "text or html (no scripts or actions)", "offset": "Default 0", "limit": "1–1000, default 20"}, scope=True),
]


def _cancelled(cancel):
    return bool(cancel and cancel())


def _number(config, key, default, lower, upper, *, integer=False):
    value = config.get(key, default)
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not lower <= value <= upper
            or (integer and type(value) is not int)):
        raise CleanMacError(f"{key} must be {'an integer' if integer else 'a number'} between {lower} and {upper}.", "invalid_configuration")
    return value


def _boolean(config, key, default):
    value = config.get(key, default)
    if type(value) is not bool:
        raise CleanMacError(f"{key} must be true or false.", "invalid_configuration")
    return value


def _report(name):
    return {"schema_version": 1, "id": new_id("scan"), "tool": name,
            "created_at": utc_now(), "status": "complete", "items": [],
            "errors": [], "measurements": {}, "cleanup_performed": False,
            "sources": SOURCES, "limitations": []}


def _finish(report):
    if report["status"] != "cancelled" and report["errors"]:
        report["status"] = "partial" if report["items"] or report["measurements"] else "unavailable"
    report["completed_at"] = utc_now()
    return report


def _probe(argv, *, cancel=None, timeout=8, max_output=_MAX_OUTPUT):
    """Bound memory, elapsed time and cancellation, even for a noisy subprocess."""
    if _cancelled(cancel):
        return {"status": "cancelled", "code": "cancelled"}
    started = time.monotonic()
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C", "LANG": "C"},
                                   close_fds=True)
    except OSError as error:
        return {"status": "unavailable", "code": "command_unavailable", "errno": error.errno}
    output = bytearray()
    received = 0
    selector = selectors.DefaultSelector()
    outcome = None
    try:
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            if _cancelled(cancel):
                outcome = {"status": "cancelled", "code": "cancelled"}
                break
            if time.monotonic() - started >= timeout:
                outcome = {"status": "unavailable", "code": "command_timeout"}
                break
            for key, _ in selector.select(0.05):
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                received += len(chunk)
                if received > max_output:
                    outcome = {"status": "unavailable", "code": "command_output_limit"}
                    break
                if key.fileobj is process.stdout:
                    output.extend(chunk)
            if outcome:
                break
        if outcome is None:
            remaining = max(0.01, timeout - (time.monotonic() - started))
            try:
                code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                outcome = {"status": "unavailable", "code": "command_timeout"}
            else:
                outcome = {"status": "available", "data": bytes(output)} if code == 0 else {"status": "unavailable", "code": "command_failed", "exit_code": code}
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()  # Only the read-only probe child, never a monitored process.
            process.wait()
        process.stdout.close()
        process.stderr.close()
    return outcome


def _read(report, argv, *, cancel=None, timeout=8):
    result = _probe(argv, cancel=cancel, timeout=timeout)
    if result["status"] != "available":
        if result["status"] == "cancelled":
            report["status"] = "cancelled"
        report["errors"].append({"source": os.path.basename(argv[0]), **result})
        return None
    return result["data"]


def _pause(seconds, cancel=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if _cancelled(cancel):
            return False
        time.sleep(min(0.05, max(0, end - time.monotonic())))
    return not _cancelled(cancel)


def _cpu_seconds(value):
    if not re.fullmatch(r"(?:\d+-)?\d+:\d{2}(?::\d{2})?(?:\.\d+)?", value):
        raise ValueError("invalid CPU time")
    days, value = value.split("-", 1) if "-" in value else ("0", value)
    parts = [float(piece) for piece in value.split(":")]
    return int(days) * 86400 + sum(number * (60 ** index) for index, number in enumerate(reversed(parts)))


def _parse_processes(data):
    items, invalid = [], 0
    for line in data.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        fields = line.split(None, 11)
        try:
            if len(fields) != 12:
                raise ValueError("shape")
            pid, ppid, uid = map(int, fields[:3])
            started = " ".join(fields[3:8])
            dt.datetime.strptime(started, "%a %b %d %H:%M:%S %Y")
            cpu = _cpu_seconds(fields[8])
            resident = int(fields[9]) * 1024
            if min(pid, ppid, uid, resident, cpu) < 0:
                raise ValueError("negative")
            items.append({"id": "process-" + digest({"pid": pid, "start": started})[:24],
                          "pid": pid, "parent_pid": ppid, "uid": uid, "started_at_local": started,
                          "cpu_seconds": cpu, "resident_bytes": resident, "state": display_path(fields[10][:32]),
                          "name": display_path(fields[11][:512]), "unresponsive": "unknown",
                          "eligible": False, "action": None})
        except (ValueError, OverflowError):
            invalid += 1
    return items, invalid


def _process_snapshot(report, cancel=None):
    data = _read(report, _PS, cancel=cancel)
    if data is None:
        return []
    items, invalid = _parse_processes(data)
    if invalid:
        report["errors"].append({"source": "ps", "code": "unparsed_processes", "count": invalid})
    return items


def _cpu(config, cancel=None):
    report = _report("cpu")
    seconds = _number(config, "sample_seconds", 1, 0.1, 5)
    limit = _number(config, "limit", 20, 1, 1000, integer=True)
    first = _process_snapshot(report, cancel)
    start = _clock()
    if report["errors"] and not first:
        return _finish(report)
    if not _pause(seconds, cancel):
        report["status"] = "cancelled"
        return _finish(report)
    second = _process_snapshot(report, cancel)
    elapsed = max(0.001, _clock() - start)
    before = {item["id"]: item for item in first}
    protected = {0, 1, os.getpid(), os.getppid()}
    parent_by_pid = {item["pid"]: item["parent_pid"] for item in second}
    parent = os.getppid()
    while parent not in (0, 1) and parent in parent_by_pid:
        next_parent = parent_by_pid[parent]
        if next_parent in protected:
            break
        protected.add(next_parent)
        parent = next_parent
    for item in second:
        previous = before.get(item["id"])
        delta = item["cpu_seconds"] - previous["cpu_seconds"] if previous else None
        item["interval_cpu_percent"] = round(100 * delta / elapsed, 2) if delta is not None and delta >= 0 else None
        item["sample_status"] = "sampled" if item["interval_cpu_percent"] is not None else "new-or-changed-process"
        item["protected"] = item["pid"] in protected or item["uid"] != os.getuid()
    report["items"] = sorted(second, key=lambda item: (-(item["interval_cpu_percent"] or 0), item["pid"]))[:limit]
    report["measurements"] = {"interval_seconds": round(elapsed, 4), "processes_observed": len(second),
                              "source": "ps cumulative CPU-time difference; 100% is one logical core"}
    try:
        report["measurements"]["load_average"] = list(os.getloadavg())
    except OSError:
        report["measurements"]["load_average"] = None
    report["limitations"] = ["High CPU or a sleeping process state does not establish unresponsiveness.",
                             "This scan does not terminate processes. Use app-process to separately inspect and approve one supported application action; other processes require manual Activity Monitor review."]
    return _finish(report)


def _parse_vm(data):
    text = data.decode("utf-8", "replace")
    match = re.search(r"page size of (\d+) bytes", text)
    if not match or not 0 < int(match.group(1)) <= 1048576:
        raise ValueError("No valid runtime page size")
    page_size = int(match.group(1))
    names = {"Pages free": "free", "Pages active": "active", "Pages inactive": "inactive",
             "Pages speculative": "speculative", "Pages wired down": "wired",
             "Pages purgeable": "purgeable", "File-backed pages": "file_backed",
             "Anonymous pages": "anonymous", "Pages stored in compressor": "uncompressed_pages_stored_in_compressor",
             "Pages occupied by compressor": "compressed_storage"}
    fields = {}
    for line in text.splitlines():
        field = re.fullmatch(r"([^:]+):\s*(\d+)\.?\s*", line)
        if field and field.group(1) in names:
            fields[names[field.group(1)]] = int(field.group(2)) * page_size
    if not fields:
        raise ValueError("No VM fields")
    return {"page_size_bytes": page_size, "bytes": fields,
            "note": "Categories overlap and must not be added into physical memory; purgeable bytes are not a cleanup promise."}


def _parse_swap(data):
    text = data.decode("ascii", "replace")
    result = {}
    for key in ("total", "used", "free"):
        match = re.search(r"\b" + key + r"\s*=\s*(\d+(?:\.\d+)?)([KMGT]?)\b", text)
        if match:
            result[key + "_bytes"] = round(float(match.group(1)) * (1024 ** " KMGT".index(match.group(2) or " ")))
    return result


def _memory(config, cancel=None):
    report = _report("memory")
    limit = _number(config, "limit", 20, 1, 1000, integer=True)
    for name, args in (("physical", ["/usr/sbin/sysctl", "-n", "hw.memsize"]),
                       ("vm", ["/usr/bin/vm_stat"]),
                       ("swap", ["/usr/sbin/sysctl", "-n", "vm.swapusage"]),
                       ("pressure", ["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"])):
        data = _read(report, args, cancel=cancel)
        if report["status"] == "cancelled":
            return _finish(report)
        value = {"status": "unavailable", "value": None}
        if data is not None:
            try:
                if name == "physical":
                    number = int(data.strip())
                    if number <= 0:
                        raise ValueError("Invalid physical memory")
                    value = {"status": "available", "bytes": number}
                elif name == "vm":
                    value = {"status": "available", **_parse_vm(data)}
                elif name == "swap":
                    parsed = _parse_swap(data)
                    if len(parsed) != 3:
                        raise ValueError("Invalid swap result")
                    value = {"status": "available", **parsed}
                else:
                    raw = int(data.strip())
                    # XNU's read-only sysctl converts internal enum values into
                    # userspace NOTE_MEMORYSTATUS_PRESSURE_* flags: 1, 2, 4.
                    label = {1: "normal", 2: "warning", 4: "critical"}.get(raw)
                    value = {"status": "available" if label else "unknown", "level": label or "unknown", "raw": raw,
                             "source": "kern.memorystatus_vm_pressure_level (availability varies by macOS)"}
            except (ValueError, OverflowError):
                report["errors"].append({"source": name, "code": "unrecognized_measurement"})
        report["measurements"][name] = value
    report["items"] = sorted(_process_snapshot(report, cancel), key=lambda item: -item["resident_bytes"])[:limit]
    report["limitations"] = ["Per-process RSS is in 1024-byte units and includes shared resident pages; summing it does not measure physical RAM usage.",
                             "Free memory alone does not determine memory pressure. No purge or VM-setting change is performed."]
    return _finish(report)


def _unsigned(value):
    return value if type(value) is int and 0 <= value < 2 ** 64 else None


def _text(value, maximum=256):
    return display_path(value[:maximum]) if isinstance(value, str) else None


def _disk_info(data, target, low_percent=10):
    raw = plistlib.loads(data)
    if not isinstance(raw, dict):
        raise ValueError("Invalid disk dictionary")
    if not isinstance(raw.get("DeviceIdentifier"), str) or not re.fullmatch(r"disk\d+(?:s\d+)*", raw["DeviceIdentifier"]):
        raise ValueError("Missing disk identity")
    identity = {key: raw.get(key) for key in ("DeviceIdentifier", "VolumeUUID", "DiskUUID", "MountPoint")}
    item = {"id": "volume-" + digest(identity)[:24], "target": display_path(target),
            "device_identifier": _text(raw.get("DeviceIdentifier")), "mount_point": _text(raw.get("MountPoint")),
            "volume_name": _text(raw.get("VolumeName")), "filesystem": _text(raw.get("FilesystemType")),
            "internal": raw.get("Internal") if type(raw.get("Internal")) is bool else None,
            "total_bytes": _unsigned(raw.get("TotalSize")),
            "volume_free_bytes": _unsigned(raw.get("VolumeFreeSpace")),
            "available_bytes": _unsigned(raw.get("APFSContainerFree")),
            "apfs_container": _text(raw.get("APFSContainerReference")),
            "smart_status": _text(raw.get("SMARTStatus")) or "unknown",
            "health_percent": None, "reclaimable_bytes": None, "eligible": False,
            "capacity_scope": "APFS shared container" if raw.get("APFSContainerReference") else "reported volume/device",
            "source": "diskutil info -plist", "identity_status": "observed"}
    if item["available_bytes"] is None:
        item["available_bytes"] = item["volume_free_bytes"]
    capacity = _unsigned(raw.get("APFSContainerSize")) or item["total_bytes"]
    available = item["available_bytes"]
    fraction = available / capacity if available is not None and capacity and available <= capacity else None
    item["available_percent"] = round(100 * fraction, 2) if fraction is not None else None
    item["low_space"] = 100 * fraction < low_percent if fraction is not None else None
    item["low_space_threshold_percent"] = low_percent
    return item


def _volume_targets(roots):
    if not roots:
        return ["/"]
    if not isinstance(roots, list) or len(roots) > 64:
        raise CleanMacError("Choose at most 64 volume roots.", "invalid_scope")
    result = []
    for root in roots:
        if not isinstance(root, str) or not root or "\x00" in root:
            raise CleanMacError("Choose filesystem roots, not device commands.", "invalid_scope")
        value = os.path.abspath(os.path.expanduser(root))
        if value not in result:
            result.append(value)
    return result


def _disks(roots, config, cancel=None):
    report = _report("disks")
    threshold = _number(config, "low_space_percent", 10, 0, 100)
    for root in _volume_targets(roots):
        data = _read(report, ["/usr/sbin/diskutil", "info", "-plist", root], cancel=cancel)
        if report["status"] == "cancelled":
            break
        if data is not None:
            try:
                item = _disk_info(data, root, threshold)
                if item["id"] not in {row["id"] for row in report["items"]}:
                    report["items"].append(item)
            except (ValueError, plistlib.InvalidFileException, TypeError, OverflowError, ExpatError, RecursionError):
                report["errors"].append({"source": "diskutil", "code": "invalid_plist", "target": display_path(root)})
    report["measurements"] = {"total_across_volumes_bytes": None, "health_percent": None}
    report["limitations"] = ["APFS volumes can share container space; capacities are never summed. Available space is not reclaimable space.",
                             "SMART telemetry may be absent behind external bridges; unknown is not healthy. No disk repair is performed."]
    return _finish(report)


def _parse_battery(data):
    raw = plistlib.loads(data)
    if not isinstance(raw, list):
        raise ValueError("Invalid battery plist")
    items = []
    for index, value in enumerate(raw[:8]):
        if not isinstance(value, dict) or value.get("BatteryInstalled") is False:
            continue
        maximum, current = _unsigned(value.get("MaxCapacity")), _unsigned(value.get("CurrentCapacity"))
        charge = round(100 * current / maximum, 2) if maximum and current is not None and current <= maximum else None
        items.append({"id": "internal-battery-" + str(index), "charge_percent": charge,
                      "measurement_identity": digest(value["IORegistryEntryID"])[:24] if type(value.get("IORegistryEntryID")) is int else None,
                      "cycle_count": _unsigned(value.get("CycleCount")),
                      "charging": value.get("IsCharging") if type(value.get("IsCharging")) is bool else None,
                      "external_power": value.get("ExternalConnected") if type(value.get("ExternalConnected")) is bool else None,
                      "condition": "unknown", "maximum_capacity_percent": None,
                      "source": "IOKit AppleSmartBattery; schema-dependent fields remain unknown"})
    return items


def _battery_health(data):
    raw = json.loads(data)
    entries = raw.get("SPPowerDataType") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise ValueError("Invalid power report")
    for item in entries:
        if isinstance(item, dict) and isinstance(item.get("sppower_battery_health_info"), dict):
            health = item["sppower_battery_health_info"]
            maximum = health.get("sppower_battery_maximum_capacity")
            match = re.fullmatch(r"(\d{1,3})%", maximum or "") if isinstance(maximum, str) else None
            return {"condition": _text(health.get("sppower_battery_health")) or "unknown",
                    "maximum_capacity_percent": int(match.group(1)) if match and int(match.group(1)) <= 100 else None}
    return {"condition": "unknown", "maximum_capacity_percent": None}


def _battery(config, cancel=None):
    report = _report("battery")
    seconds = _number(config, "sample_seconds", 0, 0, 5)
    command = ["/usr/sbin/ioreg", "-r", "-c", "AppleSmartBattery", "-a"]
    data = _read(report, command, cancel=cancel)
    if data is None:
        return _finish(report)
    sample_started = _clock() if seconds else None
    try:
        report["items"] = _parse_battery(data)
    except (ValueError, plistlib.InvalidFileException, TypeError, OverflowError, ExpatError, RecursionError):
        report["errors"].append({"source": "ioreg", "code": "invalid_battery_report"})
        return _finish(report)
    report["measurements"] = {"internal_battery": "present" if report["items"] else "not_reported", "drain": "not_measured"}
    if report["items"]:
        power = _read(report, ["/usr/sbin/system_profiler", "SPPowerDataType", "-json", "-detailLevel", "mini"], cancel=cancel, timeout=12)
        if power is not None:
            try:
                report["items"][0].update(_battery_health(power))
            except (ValueError, TypeError, OverflowError):
                report["errors"].append({"source": "system_profiler", "code": "invalid_power_report"})
    if seconds and report["items"] and report["status"] != "cancelled":
        if not _pause(seconds, cancel):
            report["status"] = "cancelled"
            return _finish(report)
        second_data = _read(report, command, cancel=cancel)
        if second_data is not None:
            try:
                after = _parse_battery(second_data)
                elapsed = max(0.001, _clock() - sample_started)
                for first, second in zip(report["items"], after):
                    same_power = (first["measurement_identity"] is not None and first["measurement_identity"] == second["measurement_identity"]
                                  and first["external_power"] is False and second["external_power"] is False and first["charging"] is False and second["charging"] is False)
                    a, b = first["charge_percent"], second["charge_percent"]
                    first["sample_end_charge_percent"] = b
                    first["observed_discharge_percentage_points_per_hour"] = round((a - b) * 3600 / elapsed, 2) if same_power and a is not None and b is not None and a >= b else None
                    first["drain_status"] = "short-sample-estimate" if first["observed_discharge_percentage_points_per_hour"] is not None else "unknown-or-power-transition"
                report["measurements"].update(drain="short-sample-estimate", interval_seconds=round(elapsed, 4))
            except (ValueError, plistlib.InvalidFileException, TypeError, OverflowError, ExpatError, RecursionError):
                report["errors"].append({"source": "ioreg", "code": "invalid_battery_report"})
    report["limitations"] = ["Battery condition and maximum capacity are only those reported by macOS; no health percentage is inferred from raw capacity counters.",
                             "A short charge sample is quantized, not a remaining-runtime prediction or evidence that a particular app caused drain. Desktop Macs can have no internal battery."]
    return _finish(report)


def _parse_interfaces(data):
    lines = data.decode("utf-8", "replace").splitlines()
    if not lines:
        raise ValueError("Missing interface table")
    header = lines[0].split()
    required = ["Name", "Mtu", "Network", "Ibytes", "Obytes"]
    if any(key not in header for key in required):
        raise ValueError("Unsupported interface columns")
    result = {}
    for line in lines[1:]:
        values = line.split()
        if len(values) == len(header) - 1 and "Address" in header and len(values) > 2 and values[2].startswith("<Link#"):
            values.insert(header.index("Address"), "")
        if len(values) != len(header):
            continue
        row = dict(zip(header, values))
        if not re.fullmatch(r"<Link#\d+>", row["Network"]):
            continue  # Ignore per-address rows that repeat the same counters.
        name = row["Name"].rstrip("*")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,63}", name):
            continue
        try:
            incoming, outgoing, mtu = int(row["Ibytes"]), int(row["Obytes"]), int(row["Mtu"])
            if min(incoming, outgoing, mtu) < 0 or max(incoming, outgoing) >= 2 ** 64:
                continue
        except ValueError:
            continue
        result[name] = {"name": name, "identity": row["Network"], "mtu": mtu,
                        "received_bytes": incoming, "sent_bytes": outgoing,
                        "kind": "loopback" if name.startswith("lo") else "tunnel/VPN" if name.startswith(("utun", "tun", "tap", "ipsec", "ppp")) else "interface"}
    return result


def _network_delta(before, after, elapsed):
    result = []
    for name, current in sorted(after.items()):
        item = dict(current)
        previous = before.get(name)
        valid = previous is not None and previous["identity"] == current["identity"] and previous["mtu"] == current["mtu"]
        incoming = current["received_bytes"] - previous["received_bytes"] if valid else None
        outgoing = current["sent_bytes"] - previous["sent_bytes"] if valid else None
        valid = valid and incoming >= 0 and outgoing >= 0 and elapsed > 0
        # Over 1 terabyte/second is outside the validated reporting range.
        valid = valid and max(incoming, outgoing) / elapsed <= 10 ** 12
        item.update(sample_status="sampled" if valid else "new-reset-or-changed-interface",
                    received_bytes_per_second=round(incoming / elapsed, 2) if valid else None,
                    sent_bytes_per_second=round(outgoing / elapsed, 2) if valid else None)
        result.append(item)
    return result


def _network(config, cancel=None):
    report = _report("network")
    seconds = _number(config, "sample_seconds", 1, 0.1, 5)
    data = _read(report, _NETSTAT, cancel=cancel)
    if data is None:
        return _finish(report)
    try:
        before = _parse_interfaces(data)
        started = _clock()
        if not _pause(seconds, cancel):
            report["status"] = "cancelled"
            return _finish(report)
        second = _read(report, _NETSTAT, cancel=cancel)
        if second is None:
            return _finish(report)
        elapsed = max(0.001, _clock() - started)
        after = _parse_interfaces(second)
        report["items"] = _network_delta(before, after, elapsed)
        report["measurements"] = {"interval_seconds": round(elapsed, 4), "network_requests": 0,
                                  "total_bytes_per_second": None,
                                  "disappeared_interfaces": sorted(set(before) - set(after))}
    except ValueError:
        report["errors"].append({"source": "netstat", "code": "invalid_interface_report"})
    report["limitations"] = ["VPN/tunnel and physical-interface counters can describe the same traffic, so they are not summed.",
                             "Local interface counters do not prove internet connectivity or internet speed; no remote request or packet inspection was performed."]
    return _finish(report)


def _devices(cancel=None):
    report = _report("devices")
    data = _read(report, ["/usr/sbin/diskutil", "list", "-plist", "external"], cancel=cancel)
    if data is None:
        return _finish(report)
    try:
        raw = plistlib.loads(data)
        names = raw.get("AllDisks", [])
        if not isinstance(names, list) or any(not isinstance(name, str) or not re.fullmatch(r"disk\d+(?:s\d+)*", name) for name in names):
            raise ValueError("Invalid external disk list")
        names = list(dict.fromkeys(names))
        if len(names) > 64:
            report["errors"].append({"source": "diskutil", "code": "device_limit", "limit": 64})
        for name in names[:64]:
            item_data = _read(report, ["/usr/sbin/diskutil", "info", "-plist", name], cancel=cancel)
            if report["status"] == "cancelled":
                break
            if item_data is not None:
                try:
                    item = _disk_info(item_data, name)
                    item["battery_percent"] = None
                    report["items"].append(item)
                except (ValueError, plistlib.InvalidFileException, TypeError, OverflowError, ExpatError, RecursionError):
                    report["errors"].append({"source": "diskutil", "code": "device_changed_or_unavailable", "device": name})
    except (ValueError, plistlib.InvalidFileException, AttributeError, TypeError, ExpatError, RecursionError):
        report["errors"].append({"source": "diskutil", "code": "invalid_device_list"})
    report["measurements"] = {"bluetooth_battery": "unsupported", "mobile_device_battery": "unsupported",
                              "pairing_attempted": False, "total_capacity_bytes": None}
    report["limitations"] = ["Only external storage reported by Disk Arbitration is covered. Unsupported Bluetooth/mobile battery values stay unknown; no private pairing databases are read.",
                             "Serial numbers are omitted. Device IDs can change on reconnect; rescan before acting on a selected volume."]
    return _finish(report)


def _health(roots, config, store, cancel=None):
    report = _report("health")
    factors = {}
    for name, operation in (("cpu", lambda: _cpu(config, cancel)), ("memory", lambda: _memory(config, cancel)),
                            ("disks", lambda: _disks(roots, config, cancel)), ("battery", lambda: _battery({}, cancel))):
        if _cancelled(cancel):
            report["status"] = "cancelled"
            break
        factor = operation()
        factors[name] = factor
        if factor["status"] != "complete":
            report["errors"].append({"source": name, "code": "partial_factor", "status": factor["status"]})
        if factor["status"] == "cancelled":
            report["status"] = "cancelled"
            break
    recommendations = []
    pressure = factors.get("memory", {}).get("measurements", {}).get("pressure", {})
    if pressure.get("level") in ("warning", "critical"):
        recommendations.append({"reason": "macOS reported elevated memory pressure.", "evidence": "memory.measurements.pressure", "action": {"tool": "memory", "action": "inspect"}})
    for item in factors.get("disks", {}).get("items", []):
        if item["low_space"] is True:
            recommendations.append({"reason": "Available space is below the selected threshold.", "evidence": {"volume_id": item["id"], "available_percent": item["available_percent"], "threshold_percent": item["low_space_threshold_percent"]},
                                    "action": {"tool": "storage-map", "action": "inspect", "requires_selected_root": True}})
    ledger = {"receipts": 0, "selected_operations": 0, "moved_to_trash_operations": 0,
              "verified_trash_operations": 0, "restored_operations": 0,
              "trash_retained_bytes_estimate": 0, "pending_recovery_receipts": [],
              "permanently_removed_bytes": None, "reclaimed_bytes": None, "measured_free_space_changes": []}
    if store is not None:
        for receipt in store.list("receipts"):
            ledger["receipts"] += 1
            summary = receipt.get("summary", {})
            if isinstance(summary, dict):
                for source, target in (("selected_count", "selected_operations"), ("trashed_count", "moved_to_trash_operations"),
                                       ("verified_trashed_count", "verified_trash_operations"), ("restored_count", "restored_operations"),
                                       ("trash_retained_bytes_estimate", "trash_retained_bytes_estimate")):
                    ledger[target] += _unsigned(summary.get(source)) or 0
            if receipt.get("status") in ("needs-recovery", "recovery_needed", "running"):
                ledger["pending_recovery_receipts"].append(receipt["id"])
            delta = receipt.get("measured_free_space_delta")
            if type(delta) is int:
                ledger["measured_free_space_changes"].append({"receipt_id": receipt["id"], "bytes": delta})
    report["measurements"] = {"factors": factors, "activity": ledger, "score": None, "malware_status": "not_scanned"}
    report["recommendations"] = recommendations
    report["limitations"] = ["Missing factors remain unknown. Permission choices and unavailable telemetry do not create a health score.",
                             "Receipt counts describe actions, not unique files. Trash-retained estimates are not freed space; free-space deltas include unrelated filesystem activity and are not summed."]
    return _finish(report)


def _source_origins(value):
    if len(value) > 65536:
        return []
    raw = plistlib.loads(value)
    if not isinstance(raw, list):
        return []
    origins = []
    for entry in raw[:20]:
        if not isinstance(entry, str) or len(entry) > 8192:
            continue
        try:
            parsed = urlsplit(entry)
            host = parsed.hostname
            if parsed.scheme.lower() not in ("http", "https", "ftp") or not host:
                continue
            origin = parsed.scheme.lower() + "://" + host
            if parsed.port:
                origin += ":" + str(parsed.port)
            if origin not in origins:
                origins.append(display_path(origin))
        except ValueError:
            continue
    return origins


def _get_origin_xattr(descriptor):
    """Read only this descriptor's bounded provenance attribute on Darwin.

    CPython does not expose getxattr on every macOS build. Darwin fgetxattr has
    position/options parameters in addition to the Linux ABI; never call it
    using the Linux signature or substitute a pathname command.
    """
    if hasattr(os, "getxattr"):
        value = os.getxattr(descriptor, "com.apple.metadata:kMDItemWhereFroms")
        if len(value) > 65536:
            raise OSError(errno.E2BIG, "Origin attribute exceeds the metadata limit")
        return value
    if platform.system() != "Darwin":
        raise OSError(errno.ENOTSUP, "Descriptor metadata unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        getter = libc.fgetxattr
    except AttributeError as error:
        raise OSError(errno.ENOTSUP, "Descriptor metadata unavailable") from error
    getter.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p,
                       ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
    getter.restype = ctypes.c_ssize_t
    name = b"com.apple.metadata:kMDItemWhereFroms"
    size = getter(descriptor, name, None, 0, 0, 0)
    if size < 0:
        number = ctypes.get_errno()
        raise OSError(number, "Origin metadata unavailable")
    if size > 65536:
        raise OSError(errno.E2BIG, "Origin attribute exceeds the metadata limit")
    buffer = ctypes.create_string_buffer(max(1, size))
    length = getter(descriptor, name, buffer, size, 0, 0)
    if length < 0:
        number = ctypes.get_errno()
        raise OSError(number, "Origin metadata changed or is unavailable")
    if length > size:
        raise OSError(errno.ERANGE, "Origin metadata changed during reading")
    return bytes(buffer.raw[:length])


def _file_metadata(item, include_origin):
    from .inventory import safe_open_item, _matches_stat
    result = {"modified_at": dt.datetime.fromtimestamp(item["mtime_ns"] / 10 ** 9, dt.timezone.utc).isoformat(),
              "created_at": None, "last_used_at": None, "source_origins": [], "source_status": "unknown"}
    if item.get("protected") or item.get("is_placeholder"):
        return result
    descriptor = None
    try:
        descriptor = safe_open_item(item)
        info = os.fstat(descriptor)
        created = getattr(info, "st_birthtime", None)
        if created is not None and created > 0:
            result["created_at"] = dt.datetime.fromtimestamp(created, dt.timezone.utc).isoformat()
        if include_origin:
            try:
                value = _get_origin_xattr(descriptor)
                result["source_origins"] = _source_origins(value)
                if result["source_origins"]:
                    result["source_status"] = "origin-metadata"
            except (OSError, ValueError, plistlib.InvalidFileException, TypeError, OverflowError, ExpatError, RecursionError):
                pass
        if not _matches_stat(item, os.fstat(descriptor)):
            return {**result, "created_at": None, "source_origins": [], "source_status": "changed-during-read"}
    except (CleanMacError, OSError, ValueError, OverflowError):
        result["source_status"] = "unavailable"
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return result


def _file_type(path):
    extension = Path(path).suffix.lower()
    for kind, endings in (("installer", {".dmg", ".pkg", ".mpkg"}),
                          ("archive", {".zip", ".gz", ".bz2", ".xz", ".7z", ".rar", ".tar"}),
                          ("media", {".jpg", ".jpeg", ".png", ".gif", ".heic", ".webp", ".mov", ".mp4", ".mkv", ".mp3", ".m4a", ".wav"}),
                          ("document", {".pdf", ".txt", ".md", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pages", ".numbers", ".key"})):
        if extension in endings:
            return kind
    return "other"


def _hidden(path, root):
    return any(part.startswith(".") for part in Path(os.path.relpath(path, root)).parts if part not in (".", ".."))


def _inventory(options, config, store, cancel=None, progress=None):
    from .inventory import scan_roots
    from .rules import classify_items
    record_id = options.get("record_id")
    if record_id:
        if options.get("roots"):
            raise CleanMacError("Choose a saved scan or new roots, not both.", "invalid_scope")
        scan = store.get("scans", record_id)
        roots, items = scan.get("roots"), scan.get("items")
        valid_roots = (isinstance(roots, list) and roots and len(roots) <= 64
                       and all(isinstance(root, str) and os.path.isabs(root) and "\x00" not in root and os.path.normpath(root) == root for root in roots))
        valid_items = isinstance(items, list) and len(items) <= 1000000
        if valid_roots and valid_items:
            for item in items:
                if not isinstance(item, dict):
                    valid_items = False
                    break
                path, root = item.get("path"), item.get("root")
                integer_fields = ("device", "inode", "mtime_ns", "logical_size", "allocated_size")
                if (not isinstance(path, str) or not os.path.isabs(path) or "\x00" in path or os.path.normpath(path) != path
                        or root not in roots or not (path == root or path.startswith(root.rstrip(os.sep) + os.sep))
                        or item.get("kind") not in ("file", "directory")
                        or not re.fullmatch(r"item-[a-f0-9]{24}", str(item.get("id")))
                        or not all(type(item.get(key)) is int for key in integer_fields)
                        or any(item[key] < 0 for key in integer_fields if key != "mtime_ns")
                        or type(item.get("protected")) is not bool or type(item.get("is_placeholder")) is not bool
                        or not isinstance(item.get("ancestors"), list)):
                    valid_items = False
                    break
        if (not valid_roots or not valid_items or scan.get("status") not in ("complete", "partial", "cancelled", "unavailable")
                or not isinstance(scan.get("errors"), list)):
            raise CleanMacError("The selected record is not a local-file inventory.", "invalid_scan")
        return scan
    roots = options.get("roots") or []
    if not roots:
        raise CleanMacError("Choose explicit folder roots or a saved local scan.", "invalid_scope")
    limit = _number(config, "max_items", 100000, 1, 1000000, integer=True)
    ignores = store.get_settings().get("ignore_paths", []) if store else []
    scan = scan_roots(roots, max_items=limit, exclude_paths=[*ignores, *([str(store.root)] if store else [])], cancel=cancel, progress=progress)
    if scan["status"] not in ("cancelled", "unavailable"):
        scan["findings"] = classify_items(scan["items"], cancel=cancel)
    scan.setdefault("findings", [])
    if _cancelled(cancel):
        scan["status"] = "cancelled"
    return scan


def _files(name, options, config, store, cancel=None, progress=None):
    include_hidden = _boolean(config, "include_hidden", True)
    include_origin = _boolean(config, "include_origin", True) if name == "downloads" else False
    offset = _number(config, "offset", 0, 0, 1000000, integer=True)
    limit = _number(config, "limit", 20, 1, 1000, integer=True)
    minimum = _number(config, "min_bytes", 52428800 if name == "large-old-files" else 0, 0, 2 ** 63, integer=True)
    older = _number(config, "older_than_days", 0, 0, 365000)
    search = config.get("search", "")
    if not isinstance(search, str) or len(search) > 4096:
        raise CleanMacError("search must be a bounded literal string.", "invalid_configuration")
    kind = config.get("file_type")
    if kind not in (None, "installer", "archive", "media", "document", "other"):
        raise CleanMacError("Unknown file_type.", "invalid_configuration")
    scan = _inventory(options, config, store, cancel, progress)
    now = time.time()
    for item in scan["items"]:
        if name == "downloads" and any(part.lower().endswith(_INCOMPLETE) for part in Path(item["path"]).parts):
            item.update(protected=True, protection_reason="incomplete-download", incomplete_download=True)
    if name == "downloads" and options.get("record_id"):
        # Protection enrichment creates its own observation; do not rewrite a
        # scan that may already have an approved selection plan.
        scan["source_scan_id"] = scan["id"]
        scan["id"] = new_id("scan")
    if store and (not options.get("record_id") or name == "downloads"):
        store.put("scans", scan)
    report = _report(name)
    report.update(scan_id=scan["id"], status=scan["status"], errors=scan["errors"], roots=scan["roots"],
                  observed_at=scan.get("completed_at", scan.get("created_at")))
    matching = []
    for item in scan["items"]:
        if item["kind"] != "file" or item["logical_size"] < minimum:
            continue
        if now - item["mtime_ns"] / 10 ** 9 < older * 86400:
            continue
        if not include_hidden and _hidden(item["path"], item["root"]):
            continue
        if search.casefold() not in item["path"].casefold() or (kind and _file_type(item["path"]) != kind):
            continue
        matching.append(item)
    matching.sort(key=lambda item: (-item["logical_size"], item["path"]))
    for item in matching[offset:offset + limit]:
        if _cancelled(cancel):
            report["status"] = "cancelled"
            break
        report["items"].append({"id": item["id"], "path": item["path"], "display_path": display_path(item["path"]),
                                "logical_bytes": item["logical_size"], "allocated_bytes_estimate": item["allocated_size"],
                                "protected": item["protected"], "protection_reason": item.get("protection_reason"),
                                "file_type": _file_type(item["path"]), "selected": False,
                                **_file_metadata(item, include_origin)})
    report["pagination"] = {"offset": offset, "limit": limit, "matched": len(matching), "next_offset": offset + limit if offset + limit < len(matching) else None}
    report["measurements"] = {"inventory_counts": scan.get("counts", {}), "reclaimable_bytes": None}
    report["next_action"] = {"command": "plan", "scan_id": scan["id"], "requires_explicit_item_selection": True}
    report["limitations"] = ["Size, file type, modification age, and origin do not make a file disposable. Creation and last-used dates remain unknown when unavailable.",
                             "Source metadata is not verified attribution; credentials, URL paths, queries and fragments are omitted. Browser history is not read."]
    return _finish(report)


def _storage_map(options, config, store, cancel=None, progress=None):
    show_hidden = _boolean(config, "include_hidden", True)
    offset = _number(config, "offset", 0, 0, 1000000, integer=True)
    limit = _number(config, "limit", 20, 1, 1000, integer=True)
    search = config.get("search", "")
    if not isinstance(search, str) or len(search) > 4096:
        raise CleanMacError("search must be a bounded literal string.", "invalid_configuration")
    format_ = config.get("format", "text")
    if format_ not in ("text", "html"):
        raise CleanMacError("format must be text or html.", "invalid_configuration")
    scan = _inventory(options, config, store, cancel, progress)
    if store and not options.get("record_id"):
        store.put("scans", scan)
    nodes = {}
    requested_roots = set(scan["roots"])
    roots = {root for root in requested_roots if not any(root != parent and root.startswith(parent.rstrip(os.sep) + os.sep) for parent in requested_roots)}

    def node(path):
        if path not in nodes:
            nodes[path] = {"id": "tree-" + digest(path)[:24], "path": path, "display_path": display_path(path),
                           "logical_bytes": 0, "allocated_bytes_estimate": 0, "files": 0,
                           "protected_entries": 0, "placeholder_entries": 0, "unknown_subtrees": 0,
                           "unreadable_entries": 0, "hardlink_aliases": 0, "eligible": False, "reclaimable_bytes": None}
        return nodes[path]

    def parents(path, root):
        current = path
        while True:
            yield node(current)
            if current == root or current == os.path.dirname(current):
                break
            current = os.path.dirname(current)

    seen = set()
    for item in sorted(scan["items"], key=lambda item: item["path"]):
        if _cancelled(cancel):
            scan["status"] = "cancelled"
            break
        path, root = item["path"], item["root"]
        identity = item["device"], item["inode"]
        alias = item["kind"] == "file" and identity in seen
        unique_file = item["kind"] == "file" and not alias
        if item["kind"] == "file":
            seen.add(identity)
        node(path).update(kind=item["kind"], item_id=item["id"], root=root, hidden=_hidden(path, root), protection_reason=item.get("protection_reason"))
        for row in parents(path, root):
            row["logical_bytes"] += item["logical_size"] if unique_file else 0
            row["allocated_bytes_estimate"] += item["allocated_size"] if unique_file else 0
            row["files"] += int(unique_file)
            row["hardlink_aliases"] += int(alias)
            row["protected_entries"] += int(item["protected"])
            row["placeholder_entries"] += int(item.get("is_placeholder", False))
            row["unknown_subtrees"] += int(item["kind"] == "directory" and item["protected"])
    for error in scan["errors"]:
        path = error.get("path")
        if not isinstance(path, str):
            continue
        for root in roots:
            if path == root or path.startswith(root.rstrip(os.sep) + os.sep):
                for row in parents(path, root):
                    row["unreadable_entries"] += 1
    requested = config.get("path")
    if requested is not None and (not isinstance(requested, str) or requested not in nodes or nodes[requested].get("kind") != "directory"):
        raise CleanMacError("Drilldown must name an exact directory in this saved inventory.", "invalid_scope")
    candidates = [row for path, row in nodes.items()
                  if (os.path.dirname(path) == requested and path != requested if requested else path in roots)
                  and (show_hidden or not row.get("hidden", False)) and search.casefold() in path.casefold()]
    candidates.sort(key=lambda row: (-row["logical_bytes"], row["path"]))
    report = _report("storage-map")
    report.update(scan_id=scan["id"], status=scan["status"], errors=scan["errors"], roots=scan["roots"],
                  observed_at=scan.get("completed_at", scan.get("created_at")), items=candidates[offset:offset + limit])
    report["pagination"] = {"offset": offset, "limit": limit, "matched": len(candidates), "next_offset": offset + limit if offset + limit < len(candidates) else None}
    report["breadcrumbs"] = []
    if requested:
        path = requested
        while path in nodes:
            report["breadcrumbs"].insert(0, {"path": path, "display_path": display_path(path)})
            if path in roots:
                break
            path = os.path.dirname(path)
    report["measurements"] = {"inventory_counts": scan.get("counts", {}), "totals_include_hidden": True,
                              "hardlink_attribution": "one lexicographically first observed path per inode",
                              "overlapping_roots_shown_within_parent": sorted(requested_roots - roots), "reclaimable_bytes": None}
    rows = ["Logical bytes | Allocated estimate | Unknown subtrees | Path"]
    rows.extend(f"{row['logical_bytes']} | {row['allocated_bytes_estimate']} | {row['unknown_subtrees']} | {row['display_path']}" for row in report["items"])
    report["text_table"] = "\n".join(rows)
    if format_ == "html":
        body = "".join("<tr>" + "".join("<td>" + html.escape(str(value), quote=True) + "</td>" for value in (row["logical_bytes"], row["allocated_bytes_estimate"], row["unknown_subtrees"], row["display_path"])) + "</tr>" for row in report["items"])
        report["html"] = '<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'"><title>CleanMac storage inventory</title><style>body{font:15px system-ui;margin:24px}table{border-collapse:collapse}td,th{padding:8px;text-align:left;border-bottom:1px solid #bbb}td:last-child{overflow-wrap:anywhere}</style><h1>Observed storage</h1><p>Allocation is an estimate. Protected or unreadable subtrees are incomplete. No cleanup controls are embedded.</p><table><thead><tr><th>Logical bytes</th><th>Allocated estimate</th><th>Unknown subtrees</th><th>Path</th></tr></thead><tbody>' + body + '</tbody></table>'
    report["next_action"] = {"command": "plan", "scan_id": scan["id"], "selection": "Explicit regular-file item IDs only; directory and child selections cannot be combined."}
    report["limitations"] = ["Hidden toggles affect displayed rows, not observed totals. Protected/unreadable subtrees remain unknown; snapshots and cloud remote allocation are outside this inventory.",
                             "Shared extents and APFS clones make allocated bytes estimates, not uniquely reclaimable storage. Hard-link aliases are attributed once; cleanup uses the shared exact-file plan."]
    return _finish(report)


def _network_executable_identity():
    path = "/usr/bin/networkQuality"
    try:
        value = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise CleanMacError("The macOS networkQuality tool is unavailable.", "dependency_needed") from error
    if not stat.S_ISREG(value.st_mode) or value.st_uid != 0 or value.st_mode & 0o022 or not os.access(path, os.X_OK):
        raise CleanMacError("networkQuality must be the trusted macOS executable.", "dependency_needed")
    return {"device": value.st_dev, "inode": value.st_ino, "size": value.st_size, "mtime_ns": value.st_mtime_ns}


def _app_identity(value):
    """Require a full native process identity before accepting a mutation plan."""
    required = {"pid", "launch_date", "bundle_id", "bundle_path", "executable_path", "uid",
                "process_start_sec", "process_start_usec", "code_signature", "activation_policy"}
    if not isinstance(value, dict) or not required.issubset(value):
        raise CleanMacError("The selected app has no complete native identity.", "invalid_app_identity")
    if (type(value["pid"]) is not int or not 1 < value["pid"] < 2 ** 31
            or type(value["uid"]) is not int or value["uid"] != os.getuid()
            or type(value["activation_policy"]) is not int or value["activation_policy"] != 0
            or type(value["process_start_sec"]) is not int or value["process_start_sec"] <= 0
            or type(value["process_start_usec"]) is not int or not 0 <= value["process_start_usec"] < 1000000):
        raise CleanMacError("The selected process is not a supported current-user app.", "protected_app_process")
    launch_date = value["launch_date"]
    if not (type(launch_date) in (int, float) and math.isfinite(launch_date) and launch_date > 0):
        raise CleanMacError("The native app launch identity is incomplete.", "invalid_app_identity")
    for key in ("bundle_path", "executable_path"):
        path = value[key]
        if not isinstance(path, str) or not os.path.isabs(path) or os.path.normpath(path) != path or "\x00" in path or len(path) > 4096:
            raise CleanMacError("The native app paths are invalid.", "invalid_app_identity")
    if (not value["bundle_path"].endswith(".app")
            or not value["executable_path"].startswith(value["bundle_path"].rstrip(os.sep) + os.sep)
            or not isinstance(value["bundle_id"], str) or not value["bundle_id"] or len(value["bundle_id"]) > 512):
        raise CleanMacError("The selected app executable is not bound to its original bundle.", "invalid_app_identity")
    signature = value["code_signature"]
    if (not isinstance(signature, dict) or signature.get("valid") is not True
            or not isinstance(signature.get("cdhash"), str) or not re.fullmatch(r"[a-fA-F0-9]{40,64}", signature["cdhash"])
            or not isinstance(signature.get("identifier"), str) or not signature["identifier"]):
        raise CleanMacError("The original app code signature could not be verified.", "app_signature_unverified")
    return copy.deepcopy(value)


def _app_receipt(store, record_id):
    from .actions import validate_action_receipt
    if not record_id:
        raise CleanMacError("Choose the completed quit receipt for the app to relaunch.", "receipt_required")
    receipt = store.get("receipts", record_id)
    plan_id = receipt.get("plan_id")
    if not isinstance(plan_id, str):
        raise CleanMacError("The selected record is not an app-action receipt.", "invalid_action_receipt")
    plan = store.get("plans", plan_id)
    receipt = validate_action_receipt(receipt, plan)
    if (receipt["tool"] != "app-process" or receipt["action"] not in ("quit", "force-quit")
            or receipt["status"] != "completed" or not isinstance(receipt.get("result"), dict)
            or receipt["result"].get("status") != "completed"):
        raise CleanMacError("Relaunch requires a completed, verified quit or force-quit receipt.", "quit_receipt_required")
    expected = _app_identity(plan["payload"].get("expected"))
    if receipt["result"].get("prior_identity") != expected:
        raise CleanMacError("The quit receipt does not verify this app identity.", "invalid_action_receipt")
    return receipt, expected


def _app_process(action, options, config, store, cancel=None, progress=None):
    from . import native_tools
    from .actions import create_action_plan, execute_action_plan

    def handshake():
        info = native_tools._handshake(cancel)
        if "app-process" not in info.get("capabilities", []):
            raise CleanMacError("Rebuild the optional native features helper to enable app-process actions.", "native_helper_incompatible")

    def verify(operation, expected):
        handshake()
        response = native_tools._call("apps-validate", {"action": operation, "expected": expected}, cancel=cancel, timeout=15)
        if (response.get("status") != "complete" or response.get("eligible") is not True
                or response.get("identity") != expected or response.get("action") != operation):
            raise CleanMacError("The app identity or native action eligibility could not be verified.", "app_identity_changed")
        return response

    if action == "inspect":
        handshake()
        pids = config.get("pids", [])
        if (not isinstance(pids, list) or len(pids) > 256 or len(set(str(pid) for pid in pids)) != len(pids)
                or any(type(pid) is not int or not 0 < pid < 2 ** 31 for pid in pids)):
            raise CleanMacError("pids must be a list of distinct positive process IDs (at most 256).", "invalid_configuration")
        response = native_tools._call("apps-inspect", {"selected_pids": pids}, cancel=cancel, timeout=15)
        if response.get("status") not in ("complete", "partial") or not isinstance(response.get("items"), list) or len(response["items"]) > 2000:
            raise CleanMacError("The native helper returned an invalid app inventory.", "invalid_native_response")
        report = _report("app-process")
        report["status"] = response["status"]
        report["errors"] = response.get("errors", [])
        if not isinstance(report["errors"], list):
            raise CleanMacError("The native helper returned invalid coverage details.", "invalid_native_response")
        known_ids = set()
        for row in response["items"]:
            if (not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]
                    or len(row["id"]) > 128 or row["id"] in known_ids or not isinstance(row.get("identity"), dict)
                    or type(row.get("protected")) is not bool or not isinstance(row.get("protection_reasons", []), list)
                    or type(row.get("can_force_quit")) is not bool):
                raise CleanMacError("The native app inventory contains invalid or repeated identities.", "invalid_native_response")
            if pids and row["identity"].get("pid") not in pids:
                raise CleanMacError("A native app result escaped the selected PID scope.", "invalid_native_response")
            known_ids.add(row["id"])
            if row["protected"] is False:
                _app_identity(row["identity"])
            report["items"].append({"id": row["id"], "identity": row["identity"], "protected": row["protected"],
                                    "protection_reasons": row.get("protection_reasons", []), "can_force_quit": row["can_force_quit"],
                                    "display_name": display_path(Path(str(row["identity"].get("bundle_path", "Unknown app"))).stem),
                                    "selected": False})
        report["limitations"] = ["Only supported signed current-user GUI apps are actionable. Critical/system apps and the helper's complete ancestor chain remain protected.",
                                 "Normal quit may show an unsaved-work dialog. Force quit is a separate action and may lose unsaved changes. Relaunch uses a completed quit receipt and the same verified original app bundle."]
        return store.put("scans", _finish(report))

    if action == "plan":
        operation = config.get("operation", "quit")
        if operation not in ("quit", "force-quit", "relaunch"):
            raise CleanMacError("operation must be quit, force-quit, or relaunch.", "invalid_configuration")
        selected = options.get("selected", [])
        source_receipt = None
        if operation == "relaunch":
            if selected:
                raise CleanMacError("Relaunch selects the app from its completed quit receipt; do not supply another selection.", "invalid_selection")
            source_receipt, expected = _app_receipt(store, options.get("record_id"))
        else:
            if not isinstance(selected, list) or len(selected) != 1 or not isinstance(selected[0], str):
                raise CleanMacError("Select exactly one app from its saved native inspection.", "selection_required")
            record_id = options.get("record_id")
            if not record_id:
                raise CleanMacError("Choose a saved app-process inspection before planning an action.", "scan_required")
            record = store.get("scans", record_id)
            if record.get("tool") != "app-process" or not isinstance(record.get("items"), list):
                raise CleanMacError("This record is not an app-process inspection.", "invalid_scan")
            matches = [row for row in record["items"] if isinstance(row, dict) and row.get("id") == selected[0]]
            if len(matches) != 1:
                raise CleanMacError("The selected app does not appear exactly once in this inspection.", "invalid_selection")
            row = matches[0]
            if row.get("protected") is not False or (operation == "force-quit" and row.get("can_force_quit") is not True):
                raise CleanMacError("This app is protected or the native helper cannot bind the requested action safely.", "protected_app_process")
            expected = _app_identity(row.get("identity"))
        verify(operation, expected)
        payload = {"operation": operation, "expected": expected,
                   "source_receipt_id": source_receipt["id"] if source_receipt else None}
        risks = {"quit": "Request normal Quit. The app may ask to save work; an unresolved dialog leaves an unverified receipt and will not trigger force quit.",
                 "force-quit": "Force quit this one app. Unsaved work may be permanently lost. This never runs as a fallback for normal quit.",
                 "relaunch": "Launch the same original signed app bundle again. This does not restore unsaved work or guarantee restoration of windows."}
        return create_action_plan(store, "app-process", operation, payload,
                                  {"operation": operation, "app": display_path(expected["bundle_path"]),
                                   "process_id": expected["pid"], "launch_date": expected["launch_date"],
                                   "consequences": risks[operation], "scope": "One exact app identity; no automatic follow-up process actions."})

    plan_id = options.get("record_id")
    if not plan_id:
        raise CleanMacError("Choose a reviewed app-process action plan.", "plan_required")
    plan = store.get("plans", plan_id)
    operation = plan.get("action")
    if operation not in ("quit", "force-quit", "relaunch"):
        raise CleanMacError("This is not a supported app-process action plan.", "action_scope_mismatch")

    def validate(saved):
        payload = saved["payload"]
        if payload.get("operation") != operation:
            raise CleanMacError("The app operation changed after planning.", "action_scope_mismatch")
        expected = _app_identity(payload.get("expected"))
        if operation == "relaunch":
            _, prior = _app_receipt(store, payload.get("source_receipt_id"))
            if prior != expected:
                raise CleanMacError("The original quit receipt changed.", "invalid_action_receipt")
        return verify(operation, expected)

    def execute(saved):
        expected = _app_identity(saved["payload"]["expected"])
        result = native_tools._call("apps-act", {"action": operation, "expected": expected, "approved": True}, cancel=cancel, timeout=25)
        if result.get("status") != "completed" or result.get("prior_identity") != expected:
            raise CleanMacError("The app action started but its exact outcome was not verified. Inspect the app and receipt before proceeding.", "app_action_unverified")
        verified = {"status": "completed", "operation": operation, "prior_identity": expected}
        if operation == "relaunch":
            new_identity = _app_identity(result.get("new_identity"))
            keys = ("bundle_id", "bundle_path", "executable_path", "uid", "code_signature")
            if new_identity == expected or any(new_identity[key] != expected[key] for key in keys):
                raise CleanMacError("The launched app did not match the original signed bundle.", "app_action_unverified")
            verified["new_identity"] = new_identity
        return verified

    return execute_action_plan(store, plan_id, options.get("approve"), "app-process", operation,
                               validate, execute, cancel=cancel, progress=progress)


def _network_speed(action, options, config, store, cancel=None, progress=None):
    from .actions import create_action_plan, execute_action_plan
    path = "/usr/bin/networkQuality"

    if action == "plan":
        seconds = _number(config, "max_seconds", 15, 5, 30, integer=True)
        return create_action_plan(store, "network-speed", "test", {"max_seconds": seconds, "executable_identity": _network_executable_identity()},
                                  {"operation": "Run Apple's networkQuality default internet service", "endpoints": "Apple's default networkQuality configuration and the test endpoints it selects; exact transfer hosts are service-selected",
                                   "data_usage": "Uploads and downloads use your internet data plan. Transfer bytes are not capped and may be substantial.",
                                   "runtime_limit_seconds": seconds, "cleanup_performed": False})

    def validate(plan):
        payload = plan["payload"]
        _number(payload, "max_seconds", None, 5, 30, integer=True)
        if payload.get("executable_identity") != _network_executable_identity():
            raise CleanMacError("The native test executable changed; create a new plan.", "stale_plan")

    def execute(plan):
        seconds = plan["payload"]["max_seconds"]
        result = _probe([path, "-c", "-M", str(seconds)], cancel=cancel, timeout=seconds + 10)
        if result["status"] != "available":
            raise CleanMacError("The network test did not produce a verified result. It may already have transferred data; inspect the receipt before retrying.", "network_test_unverified", {key: value for key, value in result.items() if key != "data"})
        try:
            raw = json.loads(result["data"])
            if not isinstance(raw, dict):
                raise ValueError("object required")
            metrics = {key: raw[key] for key in ("dl_throughput", "ul_throughput", "responsiveness", "base_rtt")
                       if key in raw and type(raw[key]) in (int, float) and math.isfinite(raw[key]) and raw[key] >= 0}
            if not metrics:
                raise ValueError("missing measurements")
        except (ValueError, TypeError, OverflowError) as error:
            raise CleanMacError("The native speed test returned an unrecognized result after its network transfer.", "network_test_unverified") from error
        return {"status": "completed", "measurements": metrics, "units": {"dl_throughput": "bits/second", "ul_throughput": "bits/second", "responsiveness": "roundtrips/minute", "base_rtt": "milliseconds"}, "cleanup_performed": False}

    return execute_action_plan(store, options.get("record_id"), options.get("approve"), "network-speed", "test", validate, execute, cancel=cancel, progress=progress)


def dispatch(name, action, options, *, store, cancel=None, progress=None):
    specs = {spec["name"]: spec for spec in TOOL_SPECS}
    if name not in specs or action not in specs[name]["actions"]:
        raise CleanMacError("Unsupported monitoring tool action.", "unsupported_action")
    if platform.system() != "Darwin":
        raise CleanMacError("These measurements require macOS; no system command was run.", "unsupported_platform")
    if not isinstance(options, dict) or not isinstance(options.get("configuration", {}), dict):
        raise CleanMacError("configuration must be an object.", "invalid_configuration")
    config = options.get("configuration", {})
    if name == "app-process":
        return _app_process(action, options, config, store, cancel, progress)
    if name == "network-speed":
        return _network_speed(action, options, config, store, cancel, progress)
    if _cancelled(cancel):
        return {**_report(name), "status": "cancelled", "completed_at": utc_now()}
    if name in _FILE_TOOLS:
        return _storage_map(options, config, store, cancel, progress) if name == "storage-map" else _files(name, options, config, store, cancel, progress)
    handlers = {"cpu": lambda: _cpu(config, cancel), "memory": lambda: _memory(config, cancel),
                "disks": lambda: _disks(options.get("roots", []), config, cancel), "battery": lambda: _battery(config, cancel),
                "network": lambda: _network(config, cancel), "devices": lambda: _devices(cancel),
                "health": lambda: _health(options.get("roots", []), config, store, cancel)}
    result = handlers[name]()
    if store is not None:
        store.put("scans", result)
    return result

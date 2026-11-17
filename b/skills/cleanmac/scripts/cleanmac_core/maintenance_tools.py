"""Scoped maintenance diagnostics and explicit, recorded native operations.

Commands are fixed argument arrays. No elevation, TCC database access, raw
filesystem index deletion, RAM purge, or cross-volume snapshot date deletion is
provided. OS command acceptance is distinct from completing the user's repair.
"""
from __future__ import annotations

import errno
import hashlib
import ipaddress
import os
from pathlib import Path
import platform
import plistlib
import re
import selectors
import stat
import subprocess
import sys
import time

from .common import CleanMacError, digest, display_path, new_id, utc_now
from .inventory import _canonical_root
from .permissions import _open_directory, describe_access_error


TOOL_SPECS = [
    {"name": "wifi", "title": "Remembered Wi-Fi networks", "feature_ids": ["CM-032"], "actions": ["inspect", "plan", "apply", "verify"]},
    {"name": "app-permissions", "title": "App privacy permissions", "feature_ids": ["CM-033"], "actions": ["inspect", "plan", "apply"]},
    {"name": "dns", "title": "DNS troubleshooting", "feature_ids": ["CM-034"], "actions": ["inspect", "plan", "apply"]},
    {"name": "purgeable", "title": "Volume capacity and purgeable space", "feature_ids": ["CM-035"], "actions": ["inspect"]},
    {"name": "memory-diagnosis", "title": "Memory pressure diagnosis", "feature_ids": ["CM-036"], "actions": ["inspect"]},
    {"name": "periodic", "title": "Legacy periodic maintenance", "feature_ids": ["CM-037"], "actions": ["inspect", "plan", "apply"]},
    {"name": "spotlight", "title": "Spotlight index diagnosis", "feature_ids": ["CM-038"], "actions": ["inspect", "plan", "apply"]},
    {"name": "disk-permissions", "title": "File permission diagnosis", "feature_ids": ["CM-039"], "actions": ["inspect"]},
    {"name": "mail-rebuild", "title": "Guided Mail mailbox rebuild", "feature_ids": ["CM-040"], "actions": ["inspect", "plan", "apply", "verify"]},
    {"name": "snapshots", "title": "Time Machine local snapshots", "feature_ids": ["CM-041"], "actions": ["inspect", "plan", "apply"]},
    {"name": "login-items", "title": "Login items and broken registrations", "feature_ids": ["CM-015", "CM-042"], "actions": ["inspect", "plan", "apply", "verify"]},
    {"name": "background-items", "title": "Background agents and daemons", "feature_ids": ["CM-015", "CM-043"], "actions": ["inspect", "plan", "apply"]},
]
_NAMES = {value["name"] for value in TOOL_SPECS}
_SERVICES = {"AddressBook", "Calendar", "Reminders", "Photos", "Camera", "Microphone", "ScreenCapture", "Accessibility", "AppleEvents", "SystemPolicyAllFiles", "SystemPolicyDocumentsFolder", "SystemPolicyDesktopFolder", "SystemPolicyDownloadsFolder", "SystemPolicyNetworkVolumes", "SystemPolicyRemovableVolumes"}
_SETTINGS = {
    "wifi": "x-apple.systempreferences:com.apple.wifi-settings-extension",
    "app-permissions": "x-apple.systempreferences:com.apple.preference.security",
    "login-items": "x-apple.systempreferences:com.apple.LoginItems-Settings.extension",
    "spotlight": "x-apple.systempreferences:com.apple.Spotlight-Settings.extension",
}
_SOURCES = {
    "wifi": ["https://support.apple.com/102480"],
    "app-permissions": ["man:tccutil(1)", "https://support.apple.com/guide/mac-help/control-access-to-information-in-apps-mh32356/mac"],
    "dns": ["man:dscacheutil(1)"],
    "spotlight": ["man:mdutil(1)", "https://support.apple.com/102321"],
    "snapshots": ["man:tmutil(8)", "https://support.apple.com/102154"],
    "mail-rebuild": ["https://support.apple.com/guide/mail/rebuild-mailboxes-mlhlp1227/mac"],
    "background-items": ["man:launchctl(1)", "https://developer.apple.com/documentation/servicemanagement/smappservice"],
    "login-items": ["https://support.apple.com/guide/mac-help/mtusr003/mac"],
    "memory-diagnosis": ["https://support.apple.com/guide/activity-monitor/actmntr34865/mac"],
    "disk-permissions": ["man:diskutil(8)"],
}
_MAX_OUTPUT = 512 * 1024
_COMMANDS = {"/usr/sbin/networksetup", "/usr/bin/tccutil", "/usr/bin/dscacheutil", "/usr/sbin/diskutil", "/usr/sbin/sysctl", "/usr/bin/vm_stat", "/usr/bin/mdutil", "/usr/bin/tmutil", "/bin/launchctl", "/bin/ps", "/usr/bin/periodic", "/usr/bin/open", "/usr/bin/codesign"}


def _run(argv, *, cancel=None, timeout=10):
    """Bound both output and time; terminate only this tool's child on cancel."""
    if not argv or argv[0] not in _COMMANDS or any(not isinstance(arg, str) or "\x00" in arg for arg in argv):
        raise CleanMacError("Unsupported maintenance command.", code="invalid_operation")
    if cancel and cancel():
        return {"status": "cancelled", "returncode": None, "stdout": "", "stderr": ""}
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"})
    except FileNotFoundError:
        return {"status": "unavailable", "reason": "command-missing", "returncode": None, "stdout": "", "stderr": ""}
    except OSError as error:
        return {"status": "unavailable", "reason": "command-unavailable", "errno": error.errno, "returncode": None, "stdout": "", "stderr": ""}
    output = {"stdout": bytearray(), "stderr": bytearray()}
    status = None
    started = time.monotonic()
    try:
        with selectors.DefaultSelector() as selector:
            for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map():
                if cancel and cancel():
                    status = "cancelled"
                    break
                if time.monotonic() - started > timeout:
                    status = "timeout"
                    break
                for key, _ in selector.select(0.05):
                    chunk = os.read(key.fileobj.fileno(), 16384)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        output[key.data].extend(chunk)
                        if sum(map(len, output.values())) > _MAX_OUTPUT:
                            status = "output-limit"
                            break
                if status:
                    break
            if status:
                process.kill()
            process.wait(timeout=1)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()
    result = {key: bytes(value[:_MAX_OUTPUT]).decode("utf-8", "replace") for key, value in output.items()}
    result.update(returncode=process.returncode, status=status or ("available" if process.returncode == 0 else "unavailable"))
    if process.returncode != 0 and not status:
        result["reason"] = "permission-needed" if re.search(r"not permitted|permission denied|must be root|requires root|not authorized", result["stderr"] + result["stdout"], re.I) else "command-failed"
    return result


def _report(name, *, status="complete", **values):
    return {"schema_version": 1, "tool": name, "status": status, "measured_at": utc_now(),
            "cleanup_performed": False, "sources": _SOURCES.get(name, []), **values}


def _command_error(result):
    return {key: result.get(key) for key in ("status", "reason", "returncode", "errno") if result.get(key) is not None}


def _configuration(options):
    value = options.get("configuration", {})
    if not isinstance(value, dict):
        raise CleanMacError("Tool configuration must be an object.", code="invalid_configuration")
    return value


def _roots(options, *, required=True):
    values = options.get("roots", [])
    if not isinstance(values, list) or any(not isinstance(value, str) or not value or "\x00" in value for value in values):
        raise CleanMacError("Provide explicit directory roots.", code="invalid_scope")
    result = list(dict.fromkeys(_canonical_root(value) for value in values))
    if required and not result:
        raise CleanMacError("Choose the folders or volume to inspect.", code="scope_required")
    return result


def _one_root(options):
    roots = _roots(options)
    if len(roots) != 1:
        raise CleanMacError("Choose exactly one volume or folder.", code="invalid_scope")
    return roots[0]


def _volume(path, *, cancel=None):
    descriptor = _open_directory(path)
    try:
        info = os.fstat(descriptor)
        result = _run(["/usr/sbin/diskutil", "info", "-plist", path], cancel=cancel)
        if result["status"] != "available":
            raise CleanMacError("The selected volume cannot be identified.", code="volume_unavailable", details=_command_error(result))
        try:
            data = plistlib.loads(result["stdout"].encode())
        except (ValueError, TypeError, plistlib.InvalidFileException) as error:
            raise CleanMacError("diskutil returned an unrecognized volume response.", code="volume_unavailable") from error
        if not isinstance(data, dict):
            raise CleanMacError("Volume identity is unavailable.", code="volume_unavailable")
        mount, uuid = data.get("MountPoint"), data.get("VolumeUUID")
        if not isinstance(mount, str) or not isinstance(uuid, str) or not re.fullmatch(r"[0-9A-Fa-f-]{32,36}", uuid):
            raise CleanMacError("A mounted volume with a stable UUID is required.", code="volume_unavailable")
        return {"path": path, "mount_point": mount, "volume_uuid": uuid.upper(), "device": info.st_dev,
                "inode": info.st_ino, "read_only": bool(data.get("ReadOnlyVolume")), "filesystem": data.get("FilesystemType"),
                "apfs_container": data.get("APFSContainerReference"), "disk_identifier": data.get("DeviceIdentifier")}
    finally:
        os.close(descriptor)


def _volume_target(options, *, cancel=None):
    target = _volume(_one_root(options), cancel=cancel)
    if os.path.normpath(target["path"]) != os.path.normpath(target["mount_point"]):
        raise CleanMacError("Choose the mounted volume itself for this action.", code="volume_root_required", details={"mount_point": target["mount_point"]})
    return target


def _wifi(options, cancel=None):
    ports = _run(["/usr/sbin/networksetup", "-listallhardwareports"], cancel=cancel)
    if ports["status"] != "available":
        return _report("wifi", status="partial", networks=[], error=_command_error(ports), settings_url=_SETTINGS["wifi"])
    devices = re.findall(r"Hardware Port: (?:Wi-Fi|AirPort)\s+Device: ([A-Za-z0-9]+)", ports["stdout"])
    networks, errors = [], []
    for device in devices:
        if cancel and cancel():
            return _report("wifi", status="cancelled", networks=networks, errors=errors)
        preferred = _run(["/usr/sbin/networksetup", "-listpreferredwirelessnetworks", device], cancel=cancel)
        current = _run(["/usr/sbin/networksetup", "-getairportnetwork", device], cancel=cancel)
        match = re.search(r"^Current (?:Wi-Fi|AirPort) Network: (.*)$", current["stdout"], re.M) if current["status"] == "available" else None
        active = match.group(1) if match else None
        if preferred["status"] != "available" or not re.search(r"Preferred networks on", preferred["stdout"]):
            errors.append({"interface": device, **_command_error(preferred), "reason": preferred.get("reason", "unrecognized-network-list")})
            continue
        for line in preferred["stdout"].splitlines():
            if line.startswith("\t"):
                ssid = line[1:]
                networks.append({"id": "wifi-" + digest([device, ssid])[:20], "interface": device, "ssid": ssid,
                                 "display_name": display_path(ssid), "active": ssid == active if active is not None else None})
    return _report("wifi", status="partial" if errors or not devices else "complete", networks=networks, errors=errors,
                   settings_url=_SETTINGS["wifi"], action_semantics="Forgetting a network removes its saved credentials and can disconnect the current connection; macOS may sync the change across devices.",
                   inventory_scope="Available networksetup metadata only; no passwords or Keychain data were read.")


def _hostname(value):
    if not isinstance(value, str) or len(value) > 253 or not value or value.startswith("-"):
        raise CleanMacError("Choose the hostname whose resolution is failing.", code="hostname_required")
    try:
        host = value.encode("idna").decode("ascii").rstrip(".")
    except UnicodeError as error:
        raise CleanMacError("Invalid hostname.", code="invalid_hostname") from error
    if not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in host.split(".")):
        raise CleanMacError("Invalid hostname.", code="invalid_hostname")
    return host.lower()


def _resolve(hostname, cancel=None):
    result = _run(["/usr/bin/dscacheutil", "-q", "host", "-a", "name", hostname], cancel=cancel)
    addresses = []
    if result["status"] == "available":
        for address in re.findall(r"^(?:ip_address|ipv6_address):\s*(\S+)", result["stdout"], re.M):
            try:
                addresses.append(str(ipaddress.ip_address(address)))
            except ValueError:
                pass
    return {"hostname": hostname, "status": "resolved" if addresses else "unresolved", "addresses": sorted(set(addresses)), "command": _command_error(result), "measured_at": utc_now()}


def _dns(options, cancel=None):
    host = _hostname(_configuration(options).get("hostname"))
    value = _resolve(host, cancel)
    return _report("dns", status="complete" if value["command"]["status"] == "available" else "partial", resolution=value,
                   note="Only this hostname was queried. A Directory Service cache flush is a troubleshooting step, not a routine speed improvement or proof of DNS repair.")


def _purgeable(options, cancel=None):
    volumes, seen, errors = [], set(), []
    for root in _roots(options):
        if cancel and cancel():
            return _report("purgeable", status="cancelled", volumes=volumes, errors=errors)
        try:
            fd = _open_directory(root)
            try:
                info, values = os.fstat(fd), os.fstatvfs(fd)
            finally:
                os.close(fd)
            if info.st_dev in seen:
                continue
            seen.add(info.st_dev)
            volumes.append({"path": root, "filesystem_device": info.st_dev, "total_bytes": values.f_blocks * values.f_frsize,
                            "free_bytes": values.f_bfree * values.f_frsize, "available_bytes": values.f_bavail * values.f_frsize,
                            "purgeable_bytes": None, "reclaimable_bytes": None})
        except OSError as error:
            errors.append(describe_access_error(root, error))
    return _report("purgeable", status="partial" if errors else "complete", volumes=volumes, errors=errors,
                   aggregate_bytes=None, notes=["Capacity measurements for APFS volumes can overlap; they are deliberately not summed.",
                   "Purgeable capacity is not inferred from free space. macOS may reclaim caches and local snapshots as needed.",
                   "Use reviewed cache, cloud-eviction, or Time Machine plans for specific supported actions; no purge-all operation is offered."])


def _memory(options, cancel=None):
    vm = _run(["/usr/bin/vm_stat"], cancel=cancel)
    pressure = _run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"], cancel=cancel)
    swap = _run(["/usr/sbin/sysctl", "-n", "vm.swapusage"], cancel=cancel)
    page = re.search(r"page size of ([0-9]+) bytes", vm["stdout"])
    counts = {}
    if page:
        for name, value in re.findall(r"^([^:\n]+):\s*(\d+)\.?$", vm["stdout"], re.M):
            if name in {"Pages free", "Pages active", "Pages inactive", "Pages wired down", "Pages occupied by compressor", "Swapins", "Swapouts"}:
                counts[name] = int(value)
    level = {"1": "normal", "2": "warning", "4": "critical"}.get(pressure["stdout"].strip(), "unknown") if pressure["status"] == "available" else "unknown"
    swapped = re.search(r"used = ([0-9.]+)([MG])", swap["stdout"])
    swap_bytes = int(float(swapped.group(1)) * (1024 ** (2 if swapped.group(2) == "M" else 3))) if swapped else None
    return _report("memory-diagnosis", status="complete" if counts and level != "unknown" else "partial",
                   pressure=level, pressure_source="kern.memorystatus_vm_pressure_level", page_size=int(page.group(1)) if page else None,
                   pages=counts, swap_used_bytes=swap_bytes, process_architecture=platform.machine(),
                   recommendation="Review heavy applications and unsaved work before choosing to quit any app." if level in ("warning", "critical") else "No RAM cleanup is recommended from low free memory alone.",
                   available_actions=[], notes=["No RAM purge, memory exhaustion, process termination, or swap deletion is performed.", "This is one measurement; cumulative swap counters do not measure the current swap rate."])


def _periodic(options, cancel=None):
    version = platform.mac_ver()[0]
    try:
        major = int(version.split(".")[0])
    except ValueError:
        major = None
    available = major is not None and 10 <= major <= 13 and os.path.isfile("/usr/bin/periodic") and os.access("/usr/bin/periodic", os.X_OK)
    return _report("periodic", status="complete", operation_status="available" if available else "unsupported",
                   macos_version=version, schedules=["daily", "weekly", "monthly"] if available else [],
                   reason="Legacy native facility is present; individual jobs may require privileges this runtime does not request." if available else "The legacy periodic facility is not enabled on this OS. Removed jobs are not recreated.")


def _spotlight(options, cancel=None):
    target = _volume_target(options, cancel=cancel)
    result = _run(["/usr/bin/mdutil", "-s", target["mount_point"]], cancel=cancel)
    text = result["stdout"].lower()
    state = "disabled" if "indexing disabled" in text or "searching disabled" in text else "enabled" if "indexing enabled" in text else "unknown"
    return _report("spotlight", status="complete" if state != "unknown" and result["status"] == "available" else "partial",
                   volume=target, indexing=state, command=_command_error(result), rebuild_completion="not-measured",
                   settings_url=_SETTINGS["spotlight"], note="An enabled index does not prove that every file is indexed. Rebuilds are asynchronous and can consume CPU and disk space.")


def _disk_permissions(options, cancel=None):
    locations = []
    for root in _roots(options):
        if cancel and cancel():
            return _report("disk-permissions", status="cancelled", locations=locations)
        try:
            fd = _open_directory(root)
            try:
                value = os.fstat(fd)
                with os.scandir(fd) as entries:
                    next(entries, None)
                locations.append({"path": root, "owner_uid": value.st_uid, "group_gid": value.st_gid, "mode": oct(stat.S_IMODE(value.st_mode)), "listing_access": "available", "acl_evaluated": False})
            finally:
                os.close(fd)
        except OSError as error:
            locations.append(describe_access_error(root, error))
    return _report("disk-permissions", status="partial" if any("status" in value for value in locations) else "complete",
                   locations=locations, repair_status="unsupported", guidance="Inspect the selected item's Sharing & Permissions in Finder. Diagnose an ACL, ownership, TCC, or filesystem problem separately; First Aid is a different operation.",
                   note="diskutil verifyPermissions/repairPermissions were removed in OS X 10.11. No recursive chmod/chown, home-permission reset, or equivalent repair is attempted.")


def _snapshots(options, cancel=None):
    volume = _volume_target(options, cancel=cancel)
    listing = _run(["/usr/bin/tmutil", "listlocalsnapshots", volume["mount_point"]], cancel=cancel)
    backup = _run(["/usr/bin/tmutil", "status"], cancel=cancel)
    match = re.search(r"\bRunning\s*=\s*([01])\s*;", backup["stdout"])
    running = match.group(1) == "1" if backup["status"] == "available" and match else None
    names, unexpected = [], []
    for line in listing["stdout"].splitlines():
        text = line.strip()
        if re.fullmatch(r"com\.apple\.TimeMachine\.\d{4}-\d{2}-\d{2}-\d{6}\.(?:local|backup)", text):
            names.append(text)
        elif text and not text.startswith("Snapshots for "):
            unexpected.append(text)
    complete = listing["status"] == "available" and not unexpected and (bool(names) or "Snapshots for " in listing["stdout"])
    return _report("snapshots", status="complete" if complete and running is not None else "partial", volume=volume,
                   snapshots=[{"id": name, "name": name} for name in sorted(names)], backup_running=running,
                   listing_complete=complete, errors=[_command_error(value) for value in (listing, backup) if value["status"] != "available"],
                   reclaimable_bytes=None, note="Local snapshots provide recovery history and macOS manages them automatically. Removing or thinning them is irreversible; remote backups are a separate scope.")


def _plist_item(root, descriptor, entry):
    path = os.path.join(root, entry.name)
    item = {"id": "job-" + digest(path)[:20], "path": path, "display_path": display_path(path), "eligible": False,
            "label": None, "domain": "unknown", "target_state": "unknown", "team_identifier": None,
            "evidence": "explicitly-selected-launch-plist"}
    fd = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024 or info.st_nlink != 1:
            return dict(item, reason="unsupported-plist-file")
        with os.fdopen(fd, "rb", closefd=False) as source:
            data = source.read(1024 * 1024 + 1)
        after = os.fstat(fd)
        if (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            return dict(item, reason="plist-changed")
        item.update(device=info.st_dev, inode=info.st_ino, owner_uid=info.st_uid, mode=stat.S_IMODE(info.st_mode), content_sha256=hashlib.sha256(data).hexdigest())
        try:
            metadata = plistlib.loads(data)
        except (ValueError, TypeError, plistlib.InvalidFileException):
            return dict(item, reason="malformed-plist")
        if not isinstance(metadata, dict):
            return dict(item, reason="malformed-plist")
        label = metadata.get("Label")
        args = metadata.get("ProgramArguments")
        program = metadata.get("Program") or (args[0] if isinstance(args, list) and args else None)
        if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", label):
            return dict(item, reason="missing-or-invalid-label")
        item.update(label=label, configured_disabled=metadata.get("Disabled", False) is True,
                    managed=any(metadata.get(key) for key in ("Managed", "ManagedBy", "MDMManaged")))
        user_root = str(Path.home() / "Library" / "LaunchAgents")
        if root == user_root:
            item["domain"] = f"gui/{os.getuid()}"
        elif root == "/Library/LaunchAgents":
            item["domain"] = f"gui/{os.getuid()}"
        elif root == "/Library/LaunchDaemons":
            item["domain"] = "system"
        if isinstance(program, str) and os.path.isabs(program) and "\x00" not in program:
            item["program"] = program
            try:
                target = os.lstat(program)
                item["target_state"] = "symlink-unverified" if stat.S_ISLNK(target.st_mode) else "present" if stat.S_ISREG(target.st_mode) else "not-regular"
                item["program_identity"] = {"device": target.st_dev, "inode": target.st_ino, "size": target.st_size, "mtime_ns": target.st_mtime_ns}
            except FileNotFoundError:
                item["target_state"] = "missing"
            except OSError:
                item["target_state"] = "unreadable"
        else:
            item["reason"] = "executable-not-resolved"
        protected = label.startswith("com.apple.") or re.search(r"apple|mdm|security|antivirus|firewall|endpoint|sentinel|crowdstrike|little.?snitch|lulu|cleanmac|codex|claude|kimi|terminal|iterm", label, re.I)
        if isinstance(program, str) and any(program == prefix or program.startswith(prefix + "/") for prefix in ("/System", "/bin", "/sbin", "/usr/bin", "/usr/sbin")):
            protected = True
        item["eligible"] = bool(root == user_root and info.st_uid == os.getuid() and not info.st_mode & 0o022
                                and not protected and not item["managed"] and item["target_state"] == "present")
        item["reason"] = item.get("reason") or ("user-owned-legacy-job" if item["eligible"] else "review-only")
        return item
    finally:
        os.close(fd)


def _background(options, cancel=None, *, login=False):
    roots = _roots(options, required=False)
    items, errors = [], []
    entries_seen = 0
    status = "complete"
    for root in roots:
        if cancel and cancel():
            status = "cancelled"
            break
        try:
            descriptor = _open_directory(root)
            try:
                with os.scandir(descriptor) as entries:
                    for entry in entries:
                        entries_seen += 1
                        if cancel and cancel():
                            status = "cancelled"
                            break
                        if len(items) >= 1000 or entries_seen > 10000:
                            status = "partial"
                            errors.append({"reason": "item-limit", "item_limit": 1000, "entry_limit": 10000})
                            break
                        if not entry.name.endswith(".plist"):
                            continue
                        try:
                            items.append(_plist_item(root, descriptor, entry))
                        except OSError as error:
                            errors.append(describe_access_error(os.path.join(root, entry.name), error))
            finally:
                os.close(descriptor)
        except OSError as error:
            errors.append(describe_access_error(root, error))
    return _report("login-items" if login else "background-items", status=status if status == "cancelled" else "partial" if errors or not roots else status,
                   roots=roots, items=items, errors=errors, broken_targets=[value["id"] for value in items if value["target_state"] == "missing"],
                   modern_registrations="settings-required", settings_url=_SETTINGS["login-items"],
                   notes=["Missing targets are review candidates; unreadable or malformed registrations are unknown, not broken.",
                          "This inventory covers selected legacy plist directories. Modern third-party registrations must be reviewed in System Settings.",
                          "Unknown ownership or signature is not evidence of malware. Persistent enable/disable does not stop or start a running process."])


def _job_state(item, cancel=None):
    result = _run(["/bin/launchctl", "print-disabled", item["domain"]], cancel=cancel)
    if result["status"] != "available" or "disabled services =" not in result["stdout"]:
        raise CleanMacError("The job's persistent launch state cannot be verified.", code="job_state_unavailable")
    entries = dict(re.findall(r'"([^"\n]+)"\s*=>\s*(true|false)', result["stdout"]))
    disabled = entries[item["label"]] == "true" if item["label"] in entries else item["configured_disabled"]
    live = _run(["/bin/launchctl", "print", item["domain"] + "/" + item["label"]], cancel=cancel)
    state = re.search(r"^\s*state = ([^\n]+)", live["stdout"], re.M)
    current = state.group(1).strip() if live["status"] == "available" and state else "not-loaded" if re.search(r"Could not find service|Could not find specified service", live["stderr"] + live["stdout"], re.I) else "unknown"
    return {"disabled": disabled, "current_state": current, "domain": item["domain"], "label": item["label"]}


def _dependency_paths(cancel=None):
    result = {os.path.abspath(sys.executable)}
    pid = os.getpid()
    seen = set()
    for _ in range(64):
        if pid <= 1:
            return result
        if pid in seen:
            break
        seen.add(pid)
        response = _run(["/bin/ps", "-p", str(pid), "-o", "ppid=,comm="], cancel=cancel)
        match = re.fullmatch(r"\s*(\d+)\s+([^\n]+)\s*", response["stdout"])
        if response["status"] != "available" or not match:
            break
        command = match.group(2).strip()
        if os.path.isabs(command):
            result.add(command)
        pid = int(match.group(1))
    raise CleanMacError("Current execution dependencies could not be determined; use System Settings for this service.", code="dependencies_unverified")


def _permission_values(configuration):
    service, bundle = configuration.get("service"), configuration.get("bundle_id")
    if service not in _SERVICES or service == "All":
        raise CleanMacError("Choose one supported privacy service; reset All is not allowed.", code="invalid_service")
    if not isinstance(bundle, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9][A-Za-z0-9-]*)+", bundle) or len(bundle) > 255:
        raise CleanMacError("Choose the exact app bundle identifier.", code="invalid_bundle_id")
    return service, bundle


def _mail(configuration):
    account, mailbox = configuration.get("account"), configuration.get("mailbox")
    if not all(isinstance(value, str) and value.strip() and len(value) <= 500 and "\x00" not in value for value in (account, mailbox)):
        raise CleanMacError("Name the account and single mailbox whose symptoms you want to investigate.", code="mailbox_scope_required")
    return _report("mail-rebuild", operation_status="guided", account=account, mailbox=mailbox,
                   account_kind=configuration.get("account_kind", "unknown"), network_state=configuration.get("network_state", "unknown"),
                   instructions=["In Mail, select only the named mailbox, then choose Mailbox > Rebuild.",
                                 "For an IMAP or Exchange mailbox, Mail can discard local copies and download them again. Wait for a working server connection.",
                                 "For local-only mail or uncertain synchronization, verify a recoverable backup before starting.",
                                 "Report whether you cancelled, started, or completed the operation; opening Mail alone does not rebuild a mailbox."],
                   verified=False, note="No Envelope Index files or Mail databases are modified.")


def _inspect(name, options, cancel):
    handlers = {"wifi": _wifi, "dns": _dns, "purgeable": _purgeable, "memory-diagnosis": _memory,
                "periodic": _periodic, "spotlight": _spotlight, "disk-permissions": _disk_permissions, "snapshots": _snapshots}
    if name in handlers:
        return handlers[name](options, cancel)
    if name in ("background-items", "login-items"):
        return _background(options, cancel, login=name == "login-items")
    if name == "mail-rebuild":
        return _mail(_configuration(options))
    if name == "app-permissions":
        return _report(name, status="partial", permissions=[{"service": value, "grant_state": "unknown"} for value in sorted(_SERVICES)],
                       settings_url=_SETTINGS[name], inventory_status="settings-required", note="Permission grants are not enumerated by reading TCC databases. Resetting one service for one app removes the remembered decision; the app may ask again. It does not impose permanent denial.")
    raise CleanMacError("Unknown maintenance tool.", code="unknown_tool")


def _selection(options, items, *, exactly_one=False):
    selected = options.get("selected", [])
    if not isinstance(selected, list) or not selected or any(not isinstance(value, str) for value in selected) or len(selected) != len(set(selected)):
        raise CleanMacError("Choose exact item IDs from this tool's inspection.", code="invalid_selection")
    index = {value["id"]: value for value in items}
    if any(value not in index for value in selected) or (exactly_one and len(selected) != 1):
        raise CleanMacError("Selection does not match the inspected items.", code="invalid_selection")
    return [index[value] for value in selected]


def _plan(name, options, store, cancel):
    from .actions import create_action_plan
    config = _configuration(options)
    operation = config.get("operation")
    payload = {"operation": operation, "os_version": platform.mac_ver()[0], "user_uid": os.getuid()}
    if name == "app-permissions":
        if operation != "reset":
            raise CleanMacError("Choose operation reset for one service and one app.", code="invalid_operation")
        service, bundle = _permission_values(config)
        payload.update(service=service, bundle_id=bundle)
        summary = {"action": "Reset one app's remembered privacy decision", "service": service, "bundle_id": bundle,
                   "consequence": "The app may request permission again; this is not a permanent deny. MDM or macOS policy may prevent the reset."}
    elif name == "dns":
        if operation != "flush":
            raise CleanMacError("Choose operation flush for explicit DNS troubleshooting.", code="invalid_operation")
        before = _dns(options, cancel)["resolution"]
        payload.update(hostname=before["hostname"], before=before)
        summary = {"action": "Flush the Directory Service cache", "hostname": before["hostname"],
                   "consequence": "This clears the whole Directory Service cache, not just this hostname. mDNSResponder is not signalled, and resolving the network problem is not guaranteed."}
    elif name == "periodic":
        available = _periodic(options, cancel)
        schedule = config.get("schedule")
        if available["operation_status"] != "available":
            raise CleanMacError(available["reason"], code="unsupported_operation")
        if operation != "run" or schedule not in available["schedules"]:
            raise CleanMacError("Choose one daily, weekly, or monthly legacy schedule.", code="invalid_operation")
        payload["schedule"] = schedule
        summary = {"action": "Run one native legacy maintenance schedule", "schedule": schedule,
                   "consequence": "The native script may require privileges or perform system maintenance; no elevation or replacement scripts are provided."}
    elif name == "spotlight":
        if operation != "rebuild":
            raise CleanMacError("Choose operation rebuild for one volume.", code="invalid_operation")
        report = _spotlight(options, cancel)
        if report["indexing"] != "enabled" or report["status"] != "complete":
            raise CleanMacError("Only a verified enabled index can be rebuilt. Review disabled/excluded volumes in Settings.", code="index_unavailable")
        payload.update(volume=report["volume"], indexing=report["indexing"])
        summary = {"action": "Erase and request rebuilding one Spotlight index", "volume": report["volume"],
                   "consequence": "Search can be incomplete while indexing runs. This may use CPU, power, and disk space; command acceptance does not establish completion."}
    elif name == "snapshots":
        if operation not in ("thin", "delete-volume-snapshots"):
            raise CleanMacError("Choose thin or delete-volume-snapshots; cross-volume date deletion is never supported.", code="invalid_operation")
        report = _snapshots(options, cancel)
        if report["status"] != "complete" or report["backup_running"] is not False or report["volume"]["read_only"]:
            raise CleanMacError("Require a fully identified writable volume, complete snapshot listing, and no active backup.", code="snapshot_preflight_failed")
        names = [value["name"] for value in report["snapshots"]]
        if not names:
            raise CleanMacError("No local Time Machine snapshots were found on this volume.", code="empty_plan")
        if operation == "delete-volume-snapshots":
            selected = _selection(options, report["snapshots"])
            if {value["id"] for value in selected} != set(names):
                raise CleanMacError("The native volume operation removes all current local snapshots. Select that exact full set or use thinning.", code="invalid_selection")
        else:
            amount = config.get("bytes")
            if type(amount) is not int or not 1 <= amount <= 1024 ** 4 or options.get("selected"):
                raise CleanMacError("Thinning needs a byte request from 1 through 1 TiB and cannot target individual snapshots.", code="invalid_configuration")
            payload["bytes"] = amount
        payload.update(volume=report["volume"], snapshots=names, backup_running=False)
        summary = {"action": operation, "volume": report["volume"], "snapshot_pool": names,
                   "requested_bytes": payload.get("bytes"), "irreversible": True,
                   "consequence": "Local recovery history will be lost. The native tool chooses snapshots during thinning; requested bytes are not guaranteed reclaimed. Remote backups are unaffected."}
    elif name == "background-items":
        if operation not in ("enable", "disable"):
            raise CleanMacError("Choose enable or disable; unloading, starting, and deleting jobs are separate unsupported operations.", code="invalid_operation")
        report = _background(options, cancel)
        item = _selection(options, report["items"], exactly_one=True)[0]
        if not item["eligible"] or item.get("program") in _dependency_paths(cancel):
            raise CleanMacError("This item requires System Settings/vendor management or is an execution dependency.", code="protected_service")
        before = _job_state(item, cancel)
        if before["disabled"] == (operation == "disable"):
            raise CleanMacError("The service already has the requested persistent state.", code="empty_plan")
        payload.update(item=item, before=before, roots=report["roots"])
        summary = {"action": operation + " future loading", "label": item["label"], "domain": item["domain"],
                   "original_state": before, "consequence": "Only persistent launch eligibility changes. No process is stopped or started, and the plist remains unchanged. Reverse with a separately reviewed enable/disable plan."}
    elif name in ("wifi", "login-items", "mail-rebuild"):
        if operation != ("open-mail" if name == "mail-rebuild" else "open-settings"):
            raise CleanMacError("This feature uses an explicit native Settings or Mail handoff.", code="invalid_operation")
        if name == "wifi":
            report = _wifi(options, cancel)
            selected = _selection(options, report["networks"])
            payload["networks"] = selected
            summary = {"action": "Open Wi-Fi Settings to review selected networks", "selected": selected,
                       "consequence": report["action_semantics"], "changes_performed": "none; user performs any forgetting in Settings"}
        elif name == "mail-rebuild":
            report = _mail(config)
            payload["mailbox"] = {key: report[key] for key in ("account", "mailbox", "account_kind", "network_state")}
            summary = {"action": "Open Mail for a guided single-mailbox rebuild", **payload["mailbox"], "instructions": report["instructions"], "changes_performed": "No automatic rebuild"}
        else:
            report = _background(options, cancel, login=True)
            selected = _selection(options, report["items"]) if options.get("selected") else []
            payload["items"] = selected
            summary = {"action": "Open Login Items Settings", "selected": selected,
                       "consequence": "Review app ownership and dependencies before changing modern registrations. Opening Settings makes no login-item changes."}
    else:
        raise CleanMacError("This tool provides diagnosis only; no maintenance action is enabled.", code="unsupported_operation")
    return create_action_plan(store, name, operation, payload, summary)


def _same_volume(first, second):
    keys = ("path", "mount_point", "volume_uuid", "device", "inode", "read_only")
    return all(first.get(key) == second.get(key) for key in keys)


def _allowed_operations(name):
    return {"app-permissions": {"reset"}, "dns": {"flush"}, "periodic": {"run"}, "spotlight": {"rebuild"},
            "snapshots": {"thin", "delete-volume-snapshots"}, "background-items": {"enable", "disable"},
            "wifi": {"open-settings"}, "login-items": {"open-settings"}, "mail-rebuild": {"open-mail"}}.get(name, set())


def _validate(name, plan, cancel=None):
    payload = plan["payload"]
    operation = payload.get("operation")
    if operation not in _allowed_operations(name) or plan.get("action") != operation:
        raise CleanMacError("The plan's operation is not supported by this tool.", code="invalid_operation")
    if payload.get("user_uid") != os.getuid() or payload.get("os_version") != platform.mac_ver()[0]:
        raise CleanMacError("The user or macOS version changed. Prepare a new maintenance plan.", code="stale_plan")
    if cancel and cancel():
        raise CleanMacError("Cancelled before the maintenance action.", code="cancelled")
    if name == "app-permissions":
        _permission_values(payload)
    elif name == "dns":
        if _hostname(payload.get("hostname")) != payload.get("hostname"):
            raise CleanMacError("Invalid hostname in plan.", code="invalid_hostname")
    elif name == "periodic":
        report = _periodic({}, cancel)
        if report["operation_status"] != "available" or payload.get("schedule") not in report["schedules"]:
            raise CleanMacError("This legacy schedule is unavailable on the current OS.", code="unsupported_operation")
    elif name in ("spotlight", "snapshots"):
        target = payload.get("volume")
        if not isinstance(target, dict):
            raise CleanMacError("Missing volume identity.", code="stale_plan")
        options = {"roots": [target.get("path")]}
        report = _spotlight(options, cancel) if name == "spotlight" else _snapshots(options, cancel)
        if not _same_volume(target, report["volume"]):
            raise CleanMacError("The selected mount changed; no maintenance was started.", code="volume_changed")
        if name == "spotlight" and (report["status"] != "complete" or report["indexing"] != "enabled"):
            raise CleanMacError("The Spotlight index state changed.", code="stale_plan")
        if name == "snapshots":
            if report["status"] != "complete" or report["backup_running"] is not False or report["volume"]["read_only"] or [value["name"] for value in report["snapshots"]] != payload.get("snapshots"):
                raise CleanMacError("Backup state or the local snapshot pool changed; review a fresh plan.", code="stale_plan")
            if operation == "thin" and (type(payload.get("bytes")) is not int or not 1 <= payload["bytes"] <= 1024 ** 4):
                raise CleanMacError("Invalid thinning amount.", code="invalid_configuration")
    elif name == "background-items":
        item = payload.get("item")
        if not isinstance(item, dict):
            raise CleanMacError("Missing service identity.", code="stale_plan")
        current = _background({"roots": payload.get("roots")}, cancel)
        match = next((value for value in current["items"] if value["id"] == item.get("id")), None)
        if not match or not match["eligible"] or any(match.get(key) != item.get(key) for key in ("path", "device", "inode", "content_sha256", "program_identity", "domain", "label")):
            raise CleanMacError("The selected service or its executable changed.", code="stale_plan")
        if match["program"] in _dependency_paths(cancel) or _job_state(match, cancel) != payload.get("before"):
            raise CleanMacError("Service state or execution dependencies changed.", code="stale_plan")
    elif name == "wifi":
        before = payload.get("networks")
        if not isinstance(before, list) or not before:
            raise CleanMacError("No Wi-Fi networks were selected.", code="invalid_selection")
        current = _wifi({}, cancel)
        by_id = {value["id"]: value for value in current["networks"]}
        if any(value != by_id.get(value.get("id")) for value in before):
            raise CleanMacError("Selected network metadata changed. Review before opening Settings.", code="stale_plan")
    elif name == "mail-rebuild":
        _mail(payload.get("mailbox", {}))


def _success(result):
    if result["status"] != "available":
        raise CleanMacError("The native operation did not report successful completion. Inspect the receipt before retrying; no broader command or elevation was attempted.",
                            code="native_operation_unverified", details=_command_error(result))


def _execute(name, plan, cancel=None):
    from .actions import checkpoint_action
    payload, operation = plan["payload"], plan["action"]
    # Perform a last scope check immediately before crossing into the native tool.
    _validate(name, plan, cancel)
    if name == "app-permissions":
        result = _run(["/usr/bin/tccutil", "reset", payload["service"], payload["bundle_id"]], cancel=cancel)
        _success(result)
        return {"effect": "permission-decision-reset", "service": payload["service"], "bundle_id": payload["bundle_id"],
                "command_completed": True, "grant_state": "unknown", "meaning": "Reset; app may ask again. This is not a permanent deny."}
    if name == "dns":
        before = _resolve(payload["hostname"], cancel)
        checkpoint_action({"before_resolution": before})
        result = _run(["/usr/bin/dscacheutil", "-flushcache"], cancel=cancel)
        _success(result)
        checkpoint_action({"directory_service_flush_completed": True})
        after = _resolve(payload["hostname"], cancel)
        return {"effect": "directory-service-cache-flushed", "command_completed": True, "before": before, "after": after,
                "problem_resolved": "not-established", "mdnsresponder_signalled": False}
    if name == "periodic":
        result = _run(["/usr/bin/periodic", payload["schedule"]], cancel=cancel, timeout=60)
        _success(result)
        return {"effect": "native-periodic-job-completed", "schedule": payload["schedule"], "command_completed": True,
                "performance_improvement": "not-measured", "output": display_path(result["stdout"][:8192]), "diagnostics": display_path(result["stderr"][:8192])}
    if name == "spotlight":
        target = payload["volume"]
        result = _run(["/usr/bin/mdutil", "-E", target["mount_point"]], cancel=cancel, timeout=30)
        _success(result)
        checkpoint_action({"rebuild_command_accepted": True, "volume_uuid": target["volume_uuid"]})
        current = _volume(target["path"], cancel=cancel)
        if not _same_volume(target, current):
            raise CleanMacError("The selected mount changed during the native request.", code="volume_changed")
        return {"effect": "spotlight-rebuild-request-accepted", "command_completed": True, "volume": current,
                "indexing_state": "started; completion unverified", "rebuild_completed": False,
                "note": "The native request returned. Spotlight decides when rebuilding runs; wait for search/indexing to settle before judging the original symptom."}
    if name == "snapshots":
        target = payload["volume"]
        argv = ["/usr/bin/tmutil", "thinlocalsnapshots", target["mount_point"], str(payload["bytes"]), "1"] if operation == "thin" else ["/usr/bin/tmutil", "deletelocalsnapshots", target["mount_point"]]
        result = _run(argv, cancel=cancel, timeout=60)
        _success(result)
        checkpoint_action({"snapshot_command_completed": True, "volume_uuid": target["volume_uuid"]})
        current = _snapshots({"roots": [target["path"]]}, cancel)
        if not _same_volume(target, current["volume"]) or not current["listing_complete"]:
            raise CleanMacError("Snapshot changes could not be verified on the original volume.", code="snapshot_result_unverified")
        remaining = [value["name"] for value in current["snapshots"]]
        if operation == "delete-volume-snapshots" and any(value in remaining for value in payload["snapshots"]):
            raise CleanMacError("Some selected local snapshots remain; inspect current state before retrying.", code="snapshot_result_unverified")
        return {"effect": "local-time-machine-snapshots-processed", "operation": operation, "volume": current["volume"],
                "removed_snapshots": sorted(set(payload["snapshots"]) - set(remaining)), "remaining_snapshots": remaining,
                "new_snapshots": sorted(set(remaining) - set(payload["snapshots"])), "requested_bytes": payload.get("bytes"),
                "reclaimed_bytes": None, "irreversible": True, "remote_backups_modified": False}
    if name == "background-items":
        item = payload["item"]
        result = _run(["/bin/launchctl", operation, item["domain"] + "/" + item["label"]], cancel=cancel)
        _success(result)
        checkpoint_action({"persistent_state_command_completed": True, "label": item["label"], "domain": item["domain"]})
        after = _job_state(item, cancel)
        if after["disabled"] != (operation == "disable"):
            raise CleanMacError("The job's persistent state could not be verified.", code="job_result_unverified")
        return {"effect": "persistent-launch-eligibility-changed", "before": payload["before"], "after": after,
                "current_state_changed": after["current_state"] != payload["before"]["current_state"],
                "process_start_stop_requested": False, "plist_modified": False,
                "recovery": {"tool": name, "operation": "enable" if operation == "disable" else "disable", "item_id": item["id"], "roots": payload["roots"], "requires_new_plan": True}}
    if name == "mail-rebuild":
        result = _run(["/usr/bin/open", "-b", "com.apple.mail"], cancel=cancel)
        _success(result)
        return {"effect": "mail-open-requested", "mailbox": payload["mailbox"], "verified_operation": False,
                "mailbox_rebuilt": False, "data_changed_by_tool": False, "next_step": "Follow the plan's single-mailbox instructions in Mail; report the outcome with verify."}
    if name in ("wifi", "login-items"):
        result = _run(["/usr/bin/open", _SETTINGS[name]], cancel=cancel)
        _success(result)
        return {"effect": "settings-open-requested", "settings_url": _SETTINGS[name], "verified_operation": False,
                "data_changed_by_tool": False, "next_step": "Review the selected items in Settings, then run verify. Opening Settings is not evidence of a change."}
    raise CleanMacError("No native operation was selected.", code="invalid_operation")


def _verify(name, options, store, cancel):
    record_id = options.get("record_id")
    if not isinstance(record_id, str) or not record_id:
        raise CleanMacError("Choose the handoff receipt to verify.", code="record_required")
    from .actions import validate_action_receipt
    receipt = store.get("receipts", record_id)
    validate_action_receipt(receipt, store.get("plans", receipt.get("plan_id")))
    plan = receipt.get("plan_snapshot", {})
    if receipt.get("tool") != name or plan.get("tool") != name or receipt.get("status") != "completed":
        raise CleanMacError("This record is not a completed handoff for the selected tool.", code="invalid_record")
    payload = plan["payload"]
    if name == "wifi":
        current = _wifi({}, cancel)
        if current["status"] != "complete":
            return _report(name, status="partial", receipt_id=record_id, outcome="unverified", current=current)
        existing = {value["id"] for value in current["networks"]}
        removed = [value for value in payload["networks"] if value["id"] not in existing]
        return _report(name, receipt_id=record_id, outcome="changed" if removed else "unchanged", removed_networks=removed,
                       unchanged_networks=[value for value in payload["networks"] if value["id"] in existing],
                       verified=True, attribution="Observed state after the handoff; the tool does not attribute changes to a particular actor.")
    outcome = _configuration(options).get("outcome")
    if outcome not in ("cancelled", "started", "completed", "unchanged"):
        raise CleanMacError("Report the native handoff outcome: cancelled, started, completed, or unchanged.", code="outcome_required")
    return _report(name, receipt_id=record_id, outcome="reported-unchanged" if outcome in ("cancelled", "unchanged") else "reported-" + outcome,
                   verified=False, user_reported=True, data_changed_by_tool=False,
                   note="This supported UI workflow has no stable completion API. The report preserves the user's outcome without claiming independent verification.")


def dispatch(name, action, options, *, store, cancel=None, progress=None):
    if name not in _NAMES:
        raise CleanMacError("Unknown maintenance tool.", code="unknown_tool")
    if not isinstance(options, dict):
        raise CleanMacError("Tool options must be an object.", code="invalid_configuration")
    if sys.platform != "darwin":
        return _report(name, status="unavailable", reason="macOS is required; no native command was run.")
    if action == "inspect":
        result = _report(name, status="cancelled") if cancel and cancel() else _inspect(name, options, cancel)
        if cancel and cancel():
            result["status"] = "cancelled"
    elif action == "plan":
        return _plan(name, options, store, cancel)
    elif action == "apply":
        from .actions import execute_action_plan
        record_id, approve = options.get("record_id"), options.get("approve")
        if not isinstance(record_id, str) or not isinstance(approve, str):
            raise CleanMacError("Apply requires the exact saved plan ID and approval digest.", code="approval_required")
        plan = store.get("plans", record_id)
        operation = plan.get("action")
        if operation not in _allowed_operations(name):
            raise CleanMacError("The plan is not a supported action for this tool.", code="invalid_operation")
        return execute_action_plan(store, record_id, approve, name, operation,
                                   lambda value: _validate(name, value, cancel),
                                   lambda value: _execute(name, value, cancel), cancel=cancel, progress=progress)
    elif action == "verify" and name in ("wifi", "login-items", "mail-rebuild"):
        result = _verify(name, options, store, cancel)
    else:
        raise CleanMacError("Unsupported action for this maintenance tool.", code="unsupported_operation")
    result.update(id=new_id("scan"), created_at=utc_now(), record_type="maintenance-inspection", action=action)
    store.put("scans", result)
    return result


for _spec in TOOL_SPECS:
    _spec["description"] = {
        "wifi": "Inspect available network metadata and verify a user-directed Settings handoff; no password access.",
        "app-permissions": "Review privacy service guidance or reset exactly one app/service decision.",
        "dns": "Query one named hostname and explicitly flush the Directory Service cache for troubleshooting.",
        "purgeable": "Report selected filesystem capacities without adding overlapping APFS or purgeable figures.",
        "memory-diagnosis": "Measure current memory pressure and swap without purging RAM or terminating apps.",
        "periodic": "Run one native daily/weekly/monthly job only when a legacy macOS facility is present.",
        "spotlight": "Inspect or request rebuilding one enabled volume index; asynchronous completion stays unverified.",
        "disk-permissions": "Inspect selected directory permissions and explain supported alternatives to removed repair commands.",
        "mail-rebuild": "Guide a single named mailbox through Mail's supported Rebuild workflow.",
        "snapshots": "Review, thin, or remove a selected volume's Time Machine snapshots with explicit loss-of-history approval.",
        "login-items": "Review selected legacy plists and use Settings for modern registrations.",
        "background-items": "Inspect selected legacy plists; enable/disable one validated user's job without stopping or starting it.",
    }[_spec["name"]]
    _spec["requirements"] = ["macOS", "Tool-specific OS access; no privilege elevation is performed"]
    _spec["scope_required"] = _spec["name"] in {"purgeable", "spotlight", "disk-permissions", "snapshots", "background-items"}
    _spec["configuration"] = {
        "wifi": {"operation": "open-settings (plan); select exact network IDs"},
        "app-permissions": {"operation": "reset", "service": "One service listed by inspect; never All", "bundle_id": "Exact app bundle identifier"},
        "dns": {"hostname": "Explicit hostname to query", "operation": "flush (plan)"},
        "periodic": {"operation": "run", "schedule": "daily | weekly | monthly"},
        "spotlight": {"operation": "rebuild"},
        "snapshots": {"operation": "thin | delete-volume-snapshots", "bytes": "For thin only, integer 1..1099511627776; do not select snapshot IDs"},
        "background-items": {"operation": "enable | disable; select one inspected item ID"},
        "login-items": {"operation": "open-settings", "outcome": "verify: cancelled | unchanged | started | completed (user-reported)"},
        "mail-rebuild": {"account": "Named account", "mailbox": "One mailbox", "account_kind": "IMAP | Exchange | local-only | unknown", "network_state": "online | offline | unknown", "operation": "open-mail", "outcome": "verify: cancelled | unchanged | started | completed (user-reported)"},
    }.get(_spec["name"], {})

"""Application evidence and narrowly scoped Homebrew cask operations.

Bundles are inspected without launching, thinning, re-signing, or changing them.
The only mutation transport here is the installed owning Homebrew package manager;
unknown artifacts and uninstall hooks are not supported. See SOURCES for contracts.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import stat
import struct
import subprocess
import tempfile
import time
from urllib.parse import urlsplit

from . import adapters
from .common import CleanMacError, digest, new_id, utc_now

SOURCES = [
    "https://docs.brew.sh/Manpage",
    "https://docs.brew.sh/Querying-Brew",
    "https://github.com/Homebrew/brew/blob/main/Library/Homebrew/cask/tab.rb",
    "https://github.com/Homebrew/brew/blob/main/Library/Homebrew/cask/caskroom.rb",
    "https://developer.apple.com/library/archive/technotes/tn2206/",
]
_SPECS = (
    ("applications", "Application identity and compatibility", ["CM-044"], ["inspect"]),
    ("localizations", "App localization footprint", ["CM-020"], ["inspect"]),
    ("architectures", "App executable architectures", ["CM-021"], ["inspect"]),
    ("app-uninstall", "Review package-manager app removal", ["CM-045"], ["inspect", "plan", "apply"]),
    ("app-leftovers", "Review installer and component candidates", ["CM-046"], ["inspect"]),
    ("app-updates", "Review package-manager app updates", ["CM-047"], ["inspect", "plan", "apply"]),
)
TOOL_SPECS = [
    {"name": name, "title": title, "feature_ids": features, "actions": actions,
     "scope_required": name not in ("app-updates", "app-uninstall"),
     "description": ("Read-only bundle evidence; no resource or executable removal." if name in ("localizations", "architectures") else
                     "Only exact installed Homebrew casks with supported app-only artifacts can be changed; other sources need their own updater/uninstaller." if len(actions) > 1 else
                     "Selected local roots only; missing usage and ownership evidence remain unknown."),
     "requirements": ["macOS"] + (["Existing Homebrew installation for package-manager actions"] if len(actions) > 1 else []),
     "configuration": {"brew_path": "Optional explicit trusted Homebrew executable path", "application_roots": "For leftovers, app roots used as presence evidence", "preferred_languages": "Optional language identifiers for localization reports"}}
    for name, title, features, actions in _SPECS
]
_TOKEN = re.compile(r"[a-z0-9][a-z0-9@+_.-]{0,127}\Z")
_PROTECTED = {"codex", "claude", "claude-code", "chatgpt", "kimi", "kimi-code", "homebrew", "iterm2", "ghostty", "warp"}
_MAX_OUTPUT = 4 * 1024 * 1024
_MAX_ENTRIES = 20000
_MAX_FILES = 2000
_MAX_SECONDS = 12.0
_DATALESS = getattr(stat, "SF_DATALESS", 0x40000000)
_CASK_FIELDS = {"token", "name", "tap", "old_tokens", "installed", "version", "pinned", "disabled",
                "artifacts", "sha256", "url", "auto_updates", "depends_on", "url_specs"}


def _error(message, code="app_unavailable", **details):
    return CleanMacError(message, code=code, details=details or None)


def _run(argv, *, cancel=None, timeout=20, env=None):
    """No shell, stdin, terminal, unbounded pipe, or hanging child process."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                       env=env, start_new_session=True)
        except OSError as exc:
            raise _error("The requested provider could not start.", "provider_unavailable", errno=exc.errno) from exc
        deadline = time.monotonic() + timeout
        failed = None
        while process.poll() is None:
            if cancel and cancel():
                failed = "cancelled"
            elif time.monotonic() > deadline:
                failed = "provider_timeout"
            elif os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > _MAX_OUTPUT:
                failed = "provider_output_limit"
            if failed:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=1)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                raise _error("Provider operation stopped; inspect its receipt before retrying a mutation.", failed)
            time.sleep(0.04)
        if os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > _MAX_OUTPUT:
            raise _error("Provider output exceeded its bounded response limit.", "provider_output_limit")
        out.seek(0)
        err.seek(0)
        return {"returncode": process.returncode, "stdout": out.read().decode("utf-8", "replace"),
                "stderr": err.read().decode("utf-8", "replace")}


def _identity(info):
    return {"device": info.st_dev, "inode": info.st_ino, "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}


def _read_local(path, limit=_MAX_OUTPUT):
    parent = adapters._open_dir(str(Path(path).parent))
    try:
        fd = os.open(Path(path).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit or getattr(before, "st_flags", 0) & _DATALESS:
            raise _error("Metadata must be a bounded, local regular file.", "unsafe_metadata")
        data = b""
        while len(data) <= limit:
            block = os.read(fd, min(65536, limit + 1 - len(data)))
            if not block:
                break
            data += block
        if len(data) > limit or _identity(before) != _identity(os.fstat(fd)):
            raise _error("Metadata changed or exceeded its limit.", "changed_metadata")
        return data, _identity(before)
    finally:
        os.close(fd)


def _json_file(path):
    raw, identity = _read_local(path)
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise _error("Package-manager metadata could not be parsed.", "invalid_provider_metadata") from exc
    if not isinstance(value, dict):
        raise _error("Package-manager metadata is not an object.", "invalid_provider_metadata")
    return value, {"path": path, "identity": identity, "sha256": hashlib.sha256(raw).hexdigest()}


def _roots(options):
    roots = options.get("roots", [])
    if not isinstance(roots, list) or not roots or any(not isinstance(p, str) or not p or "\0" in p for p in roots):
        raise _error("Choose explicit application or component roots.", "scope_required")
    return list(dict.fromkeys(os.path.abspath(os.path.expanduser(p)) for p in roots))


def _native_architecture():
    machine = platform.machine()
    try:
        probe = _run(["/usr/sbin/sysctl", "-n", "hw.optional.arm64"], timeout=3)
        if probe["returncode"] == 0 and probe["stdout"].strip() == "1":
            return "arm64"
    except CleanMacError:
        pass
    return machine


def _macho(fd, size):
    """Parse bounded Mach-O headers; never call an app executable."""
    head = os.pread(fd, min(size, 4096), 0)
    if len(head) < 8:
        return None
    names = {7: "i386", 0x01000007: "x86_64", 12: "arm", 0x0100000C: "arm64", 18: "ppc", 0x01000012: "ppc64"}
    thin = {b"\xce\xfa\xed\xfe": "<", b"\xcf\xfa\xed\xfe": "<", b"\xfe\xed\xfa\xce": ">", b"\xfe\xed\xfa\xcf": ">"}
    if head[:4] in thin:
        cpu = struct.unpack_from(thin[head[:4]] + "I", head, 4)[0]
        return [{"architecture": names.get(cpu, f"cpu-{cpu}"), "slice_bytes": size}]
    fats = {b"\xca\xfe\xba\xbe": (">", False), b"\xbe\xba\xfe\xca": ("<", False),
            b"\xca\xfe\xba\xbf": (">", True), b"\xbf\xba\xfe\xca": ("<", True)}
    if head[:4] not in fats:
        return None
    order, wide = fats[head[:4]]
    count = struct.unpack_from(order + "I", head, 4)[0]
    width = 32 if wide else 20
    if not 1 <= count <= 64 or 8 + width * count > len(head):
        raise _error("Malformed universal-binary header.", "invalid_macho")
    slices = []
    spans = []
    for index in range(count):
        at = 8 + width * index
        cpu = struct.unpack_from(order + "I", head, at)[0]
        offset, length = struct.unpack_from(order + ("QQ" if wide else "II"), head, at + 8)
        if offset < 8 + width * count or length < 8 or offset + length > size or any(offset < end and offset + length > start for start, end in spans):
            raise _error("Invalid or overlapping universal-binary slice.", "invalid_macho")
        spans.append((offset, offset + length))
        slices.append({"architecture": names.get(cpu, f"cpu-{cpu}"), "slice_bytes": length})
    return slices


def _bundle_contents(path, *, cancel=None):
    result = {"localizations": [], "binaries": [], "logical_bytes": 0, "allocated_bytes_estimate": 0,
              "status": "complete", "errors": [], "excluded": 0}
    locales = {}
    started = time.monotonic()
    entries = 0
    seen = set()
    root = adapters._open_dir(path)
    device = os.fstat(root).st_dev

    def visit(fd, rel, depth=0):
        nonlocal entries
        if cancel and cancel():
            result["status"] = "cancelled"
            return
        if depth > 48:
            result["status"] = "partial"
            return
        with os.scandir(fd) as children:
            for child in children:
                entries += 1
                if entries > _MAX_ENTRIES or time.monotonic() - started > _MAX_SECONDS:
                    result["status"] = "partial"
                    return
                if cancel and cancel():
                    result["status"] = "cancelled"
                    return
                relative = str(Path(rel) / child.name)
                try:
                    info = child.stat(follow_symlinks=False)
                    if info.st_dev != device or stat.S_ISLNK(info.st_mode) or getattr(info, "st_flags", 0) & _DATALESS:
                        result["excluded"] += 1
                        result["status"] = "partial"
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        nested = os.open(child.name, adapters._DIR_FLAGS, dir_fd=fd)
                        try:
                            if _identity(info) != _identity(os.fstat(nested)):
                                raise _error("Bundle directory changed.", "changed_bundle")
                            visit(nested, relative, depth + 1)
                        finally:
                            os.close(nested)
                        if result["status"] == "cancelled":
                            return
                    elif stat.S_ISREG(info.st_mode):
                        identity = (info.st_dev, info.st_ino)
                        if identity not in seen:
                            seen.add(identity)
                            result["logical_bytes"] += info.st_size
                            result["allocated_bytes_estimate"] += getattr(info, "st_blocks", 0) * 512
                            locale = next((p[:-6] for p in Path(relative).parts if p.endswith(".lproj")), None)
                            if locale is not None:
                                locales[locale] = locales.get(locale, 0) + info.st_size
                        if len(result["binaries"]) >= _MAX_FILES:
                            result["status"] = "partial"
                            continue
                        opened = os.open(child.name, adapters._FILE_FLAGS, dir_fd=fd)
                        try:
                            if _identity(info) != _identity(os.fstat(opened)):
                                raise _error("Bundle file changed.", "changed_bundle")
                            slices = _macho(opened, info.st_size)
                            if _identity(info) != _identity(os.fstat(opened)):
                                raise _error("Bundle file changed during header inspection.", "changed_bundle")
                        finally:
                            os.close(opened)
                        if slices:
                            result["binaries"].append({"relative_path": relative, "slices": slices, "eligible": False})
                except (OSError, CleanMacError) as exc:
                    result["status"] = "partial"
                    result["errors"].append({"path": relative, "code": getattr(exc, "code", "metadata_denied")})
    try:
        visit(root, "")
    finally:
        os.close(root)
    result["localizations"] = [{"language": name, "logical_bytes": size, "eligible": False} for name, size in sorted(locales.items())]
    return result


def _signature(path, *, cancel=None):
    verify = _run(["/usr/bin/codesign", "--verify", "--strict", path], cancel=cancel, timeout=10)
    details = _run(["/usr/bin/codesign", "--display", "--verbose=4", path], cancel=cancel, timeout=5)
    text = details["stderr"] + details["stdout"]
    team = re.search(r"^TeamIdentifier=([A-Z0-9]+)$", text, re.M)
    ident = re.search(r"^Identifier=([^\r\n]+)$", text, re.M)
    return {"status": "valid" if verify["returncode"] == 0 else "not_verified",
            "team_id": team.group(1) if team else None, "identifier": ident.group(1) if ident else None}


def _inspect_apps(name, options, cancel):
    report = adapters.scan_applications(_roots(options), cancel=cancel)
    report.update(tool=name, sources=SOURCES, native_architecture=_native_architecture())
    report["limitations"] = [
        "Missing last-use metadata is unknown; timestamps are hints, not evidence an app is unused.",
        "Localization and architecture footprints are report-only. Modifying signed resources or executable slices can invalidate signatures, plugins, and updates.",
        "A compatible main executable does not prove compatibility of every plugin or helper. Intel apps on Apple silicon may require Rosetta.",
        "Allocated bytes are estimates; APFS sharing and compression prevent guaranteed reclaimable-byte claims.",
    ]
    for item in report["items"]:
        if cancel and cancel():
            report["status"] = "cancelled"
            break
        try:
            contents = _bundle_contents(item["path"], cancel=cancel)
            item.update(contents)
            if contents["status"] != "complete":
                report["status"] = contents["status"]
            item["signature"] = _signature(item["path"], cancel=cancel)
            item["signature_status"] = item["signature"]["status"]
            use = _run(["/usr/bin/mdls", "-raw", "-name", "kMDItemLastUsedDate", item["path"]], cancel=cancel, timeout=3)
            last = use["stdout"].strip()
            item["last_used"] = last if use["returncode"] == 0 and re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4}", last) else None
            item["unused_status"] = "usage-hint-available" if item["last_used"] else "unknown"
            names = {s["architecture"] for b in item["binaries"] for s in b["slices"]}
            item["architectures"] = sorted(names)
            main = "Contents/MacOS/" + (item.get("executable") or "")
            main_names = {s["architecture"] for b in item["binaries"] if b["relative_path"] == main for s in b["slices"]}
            item["compatibility_status"] = ("unsupported-32-bit" if main_names and main_names <= {"i386", "ppc", "arm"} else
                                            "native-main-executable" if report["native_architecture"] in main_names else
                                            "rosetta-may-be-required" if report["native_architecture"] == "arm64" and "x86_64" in main_names else "unknown")
            preferred = options["configuration"].get("preferred_languages", [])
            if not isinstance(preferred, list) or any(not isinstance(x, str) for x in preferred):
                raise _error("preferred_languages must be an array of language identifiers.", "invalid_configuration")
            for locale in item["localizations"]:
                locale["preference"] = "selected" if locale["language"] in preferred else "unknown" if not preferred else "not-selected"
            item["resource_removal"] = "unsupported-signature-and-fallback-risk"
        except (OSError, CleanMacError) as exc:
            report["status"] = "cancelled" if getattr(exc, "code", None) == "cancelled" else "partial"
            report["errors"].append({"path": item["path"], "code": getattr(exc, "code", "metadata_denied")})
    return report


def _provider(configuration):
    candidate = configuration.get("brew_path")
    if candidate is None:
        candidate = next((p for p in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew") if os.path.lexists(p)), None)
    if not isinstance(candidate, str) or not os.path.isabs(candidate) or "\0" in candidate:
        raise _error("An existing Homebrew installation is required; it is never installed automatically.", "provider_unavailable")
    # Homebrew's bin/brew may be a deliberate symlink. Pin its resolved code path,
    # then reject writable-by-others ancestors and substitution on later recheck.
    resolved = str(Path(candidate).resolve(strict=True))
    parent = adapters._open_dir(str(Path(resolved).parent))
    try:
        info = os.stat(Path(resolved).name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022 or not info.st_mode & 0o111:
            raise _error("Homebrew executable ownership or mode is not trusted.", "unsafe_provider")
    finally:
        os.close(parent)
    for directory in Path(resolved).parents:
        info_dir = directory.stat()
        if info_dir.st_uid not in (0, os.getuid()) or (info_dir.st_mode & 0o022 and not (info_dir.st_uid == 0 and info_dir.st_mode & stat.S_ISVTX)):
            raise _error("Homebrew path has an untrusted ancestor.", "unsafe_provider")
    raw, file_id = _read_local(resolved)
    return {"path": resolved, "requested_path": candidate, "identity": file_id, "sha256": hashlib.sha256(raw).hexdigest()}


def _brew(provider, args, *, cancel=None, mutation=False):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("HOMEBREW_", "DYLD_")) and key not in ("RUBYOPT", "RUBYLIB", "BASH_ENV", "ENV")}
    env.update(HOMEBREW_NO_AUTO_UPDATE="1", HOMEBREW_NO_ANALYTICS="1", HOMEBREW_NO_INSTALL_CLEANUP="1", HOMEBREW_NO_AUTOREMOVE="1",
               HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK="1", HOMEBREW_NO_ENV_HINTS="1", NONINTERACTIVE="1", SUDO_ASKPASS="/usr/bin/false")
    # Caller-controlled Homebrew options must not silently widen approved scope.
    env.pop("HOMEBREW_CASK_OPTS", None)
    env.pop("HOMEBREW_INSTALL_FROM_API", None)
    result = _run([provider["path"], *args], env=env, cancel=cancel, timeout=300 if mutation else 30)
    if result["returncode"] != 0:
        raise _error("Homebrew did not complete the requested operation. No automatic retry was made.", "provider_failed", exit_code=result["returncode"])
    return result["stdout"]


def _brew_json(provider, args, *, cancel=None):
    try:
        value = json.loads(_brew(provider, args, cancel=cancel))
    except ValueError as exc:
        raise _error("Homebrew did not return supported JSON.", "invalid_provider_metadata") from exc
    if not isinstance(value, dict):
        raise _error("Homebrew response is not an object.", "invalid_provider_metadata")
    return value


def _artifacts(artifacts):
    """Return app artifact descriptors only; hooks, pkg installers, binaries fail closed."""
    if not isinstance(artifacts, list) or not artifacts:
        raise _error("Installed artifact ownership is unavailable.", "unsupported_cask")
    apps = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise _error("Unsupported cask artifact schema.", "unsupported_cask")
        if set(artifact) == {"zap"}:
            continue  # --zap is never used, so this metadata cannot cause a mutation.
        if set(artifact) - {"app", "target"} or "app" not in artifact:
            raise _error("This cask includes hooks or non-app artifacts requiring its own workflow.", "unsupported_cask")
        args = artifact["app"]
        if not isinstance(args, list) or not 1 <= len(args) <= 2 or not isinstance(args[0], str):
            raise _error("Unsupported app artifact.", "unsupported_cask")
        source = args[0]
        if not source.endswith(".app") or source.startswith("/") or ".." in Path(source).parts or "\0" in source:
            raise _error("Unsupported app artifact path.", "unsupported_cask")
        if len(args) == 2 and (not isinstance(args[1], dict) or set(args[1]) != {"target"} or not isinstance(args[1]["target"], str)):
            raise _error("Unsupported app relocation options.", "unsupported_cask")
        apps.append(artifact)
    if not apps:
        raise _error("No owned app artifacts were found.", "unsupported_cask")
    return apps


def _installed_evidence(provider, item, *, cancel=None):
    token = item["token"]
    root = _brew(provider, ["--caskroom"], cancel=cancel).strip()
    if not os.path.isabs(root) or "\n" in root or "\0" in root:
        raise _error("Homebrew caskroom path was not recognized.", "invalid_provider_metadata")
    metadata = str(Path(root) / token / ".metadata")
    receipt, receipt_evidence = _json_file(str(Path(metadata) / "INSTALL_RECEIPT.json"))
    source = receipt.get("source", {})
    if (not isinstance(source, dict) or source.get("version") != item.get("installed") or source.get("tap") != "homebrew/cask"
            or receipt.get("uninstall_flight_blocks") is not False):
        raise _error("Installed cask source, version, or uninstall hooks are not verified.", "unsupported_cask")
    old = _artifacts(receipt.get("uninstall_artifacts"))
    new = _artifacts(item.get("artifacts"))
    # Installed JSON can override receipt artifacts; bind it and reject overrides
    # which differ. Old Ruby metadata is never accepted for execution.
    root_fd = adapters._open_dir(metadata)
    candidates = []
    try:
        with os.scandir(root_fd) as versions:
            for version in versions:
                if len(candidates) > 100:
                    raise _error("Installed metadata exceeds the supported history limit.", "unsupported_cask")
                info = version.stat(follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode):
                    continue
                version_fd = os.open(version.name, adapters._DIR_FLAGS, dir_fd=root_fd)
                try:
                    with os.scandir(version_fd) as stamps:
                        for stamp in stamps:
                            if len(candidates) >= 100:
                                raise _error("Installed metadata exceeds the supported history limit.", "unsupported_cask")
                            if not stat.S_ISDIR(stamp.stat(follow_symlinks=False).st_mode):
                                continue
                            candidates.append((stamp.name, version.name, str(Path(metadata) / version.name / stamp.name / "Casks")))
                finally:
                    os.close(version_fd)
    finally:
        os.close(root_fd)
    if not candidates:
        raise _error("Installed cask definition is missing.", "unsupported_cask")
    stamp, version, location = max(candidates)
    if version != item["installed"]:
        raise _error("Installed cask versions disagree.", "changed_provider_metadata")
    installed, definition_evidence = _json_file(str(Path(location) / (token + ".json")))
    if "artifacts" in installed and _artifacts(installed["artifacts"]) != old:
        raise _error("Installed cask definition overrides its recorded ownership.", "unsupported_cask")
    # Compare app source/target directives, excluding new JSON's resolved target.
    if [a["app"] for a in old] != [a["app"] for a in new]:
        raise _error("An update changes the app artifact layout; review through Homebrew directly.", "unsupported_cask")
    return {"receipt": receipt_evidence, "definition": definition_evidence,
            "runtime_dependencies": receipt.get("runtime_dependencies", {}), "artifacts": old}


def _cask_guard(item, operation):
    token = item.get("token")
    if not isinstance(token, str) or not _TOKEN.fullmatch(token) or token in _PROTECTED:
        raise _error("This cask token is invalid or belongs to a protected agent/runtime.", "protected_application")
    if item.get("tap") != "homebrew/cask" or item.get("old_tokens") or item.get("pinned") or item.get("disabled"):
        raise _error("Only unpinned official installed casks with unambiguous ownership are supported.", "unsupported_cask")
    if not isinstance(item.get("installed"), str) or not item["installed"]:
        raise _error("The selected cask is not installed.", "not_installed")
    # Do not turn provider credentials or signed download URLs into local history.
    # Such providers need their own authenticated updater, not this adapter.
    try:
        url = urlsplit(item.get("url") or "")
        if url.username is not None or url.password is not None or url.query or url.fragment:
            raise ValueError("credential-bearing or signed URL")
        specs = item.get("url_specs") or {}
        if not isinstance(specs, dict) or set(specs) - {"verified"}:
            raise ValueError("authenticated or unsupported download options")
        if specs and (not isinstance(specs["verified"], str) or not re.fullmatch(r"[A-Za-z0-9./_-]{1,512}", specs["verified"])):
            raise ValueError("unsupported download provenance")
    except (TypeError, ValueError) as exc:
        raise _error("Credential-bearing URLs and authenticated download options require the owning updater; sensitive metadata was omitted.", "authenticated_update_unsupported") from exc
    _artifacts(item.get("artifacts"))
    if operation == "upgrade":
        if (not isinstance(item.get("sha256"), str) or not re.fullmatch(r"[0-9a-fA-F]{64}", item["sha256"])
                or not isinstance(item.get("url"), str) or not item["url"].startswith("https://")
                or item.get("version") in (None, "latest") or item.get("auto_updates")):
            raise _error("This update requires a fixed HTTPS release with a checksum and no automatic updater.", "unsupported_update")
        deps = item.get("depends_on") or {}
        if not isinstance(deps, dict) or any(key in deps for key in ("formula", "cask")):
            raise _error("Updates with package dependencies require a separate dependency review.", "unsupported_update")


def _bundle_evidence(item, *, cancel=None):
    targets = []
    for artifact in _artifacts(item.get("artifacts")):
        target = artifact.get("target")
        if not isinstance(target, str) or not os.path.isabs(target) or not target.endswith(".app") or "\0" in target:
            raise _error("Homebrew did not supply an exact installed app target.", "unsupported_cask")
        if target.startswith(("/System/", "/usr/")) or Path(target).stem.lower() in _PROTECTED:
            raise _error("System and agent applications cannot be managed by this action.", "protected_application")
        fd = adapters._open_dir(target)
        try:
            identity = _identity(os.fstat(fd))
            metadata = adapters._read_plist(fd, target)
            bundle_id = metadata.get("CFBundleIdentifier")
        finally:
            os.close(fd)
        if not isinstance(bundle_id, str) or bundle_id.startswith(("com.apple.", "com.openai.", "com.anthropic.")):
            raise _error("Application identity is missing or protected.", "protected_application")
        signature = _signature(target, cancel=cancel)
        if signature["status"] != "valid" or not signature["team_id"] or signature["identifier"] != bundle_id:
            raise _error("A valid matching vendor signature is required for package-manager mutations.", "signature_unverified")
        targets.append({"path": target, "identity": identity, "bundle_id": bundle_id, "signature": signature})
    return targets


def _package_report(name, options, *, cancel=None):
    report = {"schema_version": 1, "id": new_id("appscan"), "tool": name, "created_at": utc_now(),
              "status": "complete", "read_only": True, "items": [], "errors": [], "sources": SOURCES,
              "limitations": ["Homebrew metadata freshness is provider-managed; discovery does not run brew update or install downloads.",
                              "App Store, macOS, Sparkle, and other vendor updates require their native updater. No app launch or relaunch is performed.",
                              "Package-manager changes are not Trash operations. Rollback is not guaranteed; receipts retain recovery guidance."]}
    provider = _provider(options["configuration"])
    report["provider"] = provider
    content = _brew_json(provider, ["info", "--json=v2", "--installed", "--cask"], cancel=cancel)
    casks = content.get("casks")
    if not isinstance(casks, list) or len(casks) > 5000:
        raise _error("Installed cask inventory was not recognized.", "invalid_provider_metadata")
    outdated = {}
    if name == "app-updates":
        data = _brew_json(provider, ["outdated", "--cask", "--json=v2"], cancel=cancel)
        if not isinstance(data.get("casks"), list):
            raise _error("Outdated cask inventory was not recognized.", "invalid_provider_metadata")
        outdated = {value.get("name"): value for value in data["casks"] if isinstance(value, dict)}
    for cask in casks:
        if cancel and cancel():
            report["status"] = "cancelled"
            break
        if not isinstance(cask, dict) or not isinstance(cask.get("token"), str):
            report["status"] = "partial"
            report["errors"].append({"code": "invalid_cask"})
            continue
        item = {"id": "cask-" + digest({"token": cask["token"], "tap": cask.get("tap")})[:24],
                "token": cask["token"], "name": cask.get("name"), "installed_version": cask.get("installed"),
                "available_version": cask.get("version"), "update_available": cask["token"] in outdated,
                "source": "homebrew", "eligible": False}
        try:
            _cask_guard(cask, "upgrade" if name == "app-updates" else "uninstall")
            if name == "app-updates" and not item["update_available"]:
                item["reason"] = "No update reported by the owning package manager."
            else:
                item["reason"] = "Candidate requires fresh installed-artifact and signed-bundle verification when preparing the plan."
                item["eligible"] = True
                item["metadata"] = {key: value for key, value in cask.items() if key in _CASK_FIELDS}
                item["metadata_digest"] = digest(cask)
        except CleanMacError as exc:
            item["reason"] = exc.message
            item["reason_code"] = exc.code
        report["items"].append(item)
    return report


def _prepare_package(name, options, store, *, cancel=None):
    from .actions import create_action_plan
    record = store.get("scans", options.get("record_id"))
    if record.get("tool") != name or record.get("status") != "complete":
        raise _error("Choose a complete report for this app tool.", "invalid_scan")
    selected = options.get("selected", [])
    if not isinstance(selected, list) or len(selected) != 1:
        raise _error("Prepare exactly one app operation per plan.", "invalid_selection")
    matches = [v for v in record.get("items", []) if v.get("id") == selected[0]]
    if len(matches) != 1 or not matches[0].get("eligible"):
        raise _error("Choose one eligible cask from the saved report.", "invalid_selection")
    old = matches[0]["metadata"]
    provider = _provider({"brew_path": record["provider"]["requested_path"]})
    if provider != record["provider"]:
        raise _error("The package-manager executable changed; inspect again.", "changed_provider")
    operation = "upgrade" if name == "app-updates" else "uninstall"
    data = _brew_json(provider, ["info", "--json=v2", "--cask", old["token"]], cancel=cancel)
    casks = data.get("casks", [])
    if len(casks) != 1 or digest(casks[0]) != matches[0].get("metadata_digest"):
        raise _error("Cask metadata changed; inspect again before approval.", "changed_provider_metadata")
    _cask_guard(old, operation)
    installed = _installed_evidence(provider, old, cancel=cancel)
    bundles = _bundle_evidence(old, cancel=cancel)
    _assert_not_running(bundles, cancel=cancel)
    payload = {"provider": provider, "token": old["token"], "operation": operation,
               "metadata": old, "metadata_digest": matches[0]["metadata_digest"], "installed": installed, "bundles": bundles}
    return create_action_plan(store, name, operation, payload,
        {"title": f"{operation.title()} Homebrew cask {old['token']}", "installed_version": old["installed"],
         "target_version": old.get("version") if operation == "upgrade" else None,
         "applications": [b["path"] for b in bundles], "owned_components": installed["artifacts"],
         "preserved": "No zap or application-support cleanup; no automatic app quit, relaunch, or dependency cleanup.",
         "recovery": "Package-manager operation; no guaranteed rollback or Trash restoration. Review Homebrew state after failure."})


def _assert_not_running(bundles, *, cancel=None):
    result = _run(["/bin/ps", "-axo", "comm="], cancel=cancel, timeout=3)
    if result["returncode"] != 0:
        raise _error("Running application state could not be verified.", "process_state_unknown")
    paths = [line.strip() for line in result["stdout"].splitlines() if line.strip()]
    for bundle in bundles:
        if any(path == bundle["path"] or path.startswith(bundle["path"] + "/") for path in paths):
            raise _error("Quit the selected application yourself before preparing or applying this plan.", "application_running")


def _apply_package(name, options, store, *, cancel=None, progress=None):
    from .actions import execute_action_plan
    operation = "upgrade" if name == "app-updates" else "uninstall"

    def validate(plan):
        data = plan["payload"]
        provider = _provider({"brew_path": data["provider"]["requested_path"]})
        if provider != data["provider"]:
            raise _error("Homebrew executable changed after approval.", "changed_provider")
        current = _brew_json(provider, ["info", "--json=v2", "--cask", data["token"]], cancel=cancel)
        casks = current.get("casks", [])
        if len(casks) != 1 or digest(casks[0]) != data["metadata_digest"]:
            raise _error("Cask metadata changed after approval.", "changed_provider_metadata")
        _cask_guard(data["metadata"], operation)
        if _installed_evidence(provider, data["metadata"], cancel=cancel) != data["installed"] or _bundle_evidence(data["metadata"], cancel=cancel) != data["bundles"]:
            raise _error("Installed ownership, bundle identity, or signature changed.", "changed_application")
        _assert_not_running(data["bundles"], cancel=cancel)

    def execute(plan):
        data = plan["payload"]
        _brew(data["provider"], [operation, "--cask", data["token"]], cancel=cancel, mutation=True)
        current = _brew_json(data["provider"], ["info", "--json=v2", "--installed", "--cask"], cancel=cancel)
        installed = [i for i in current.get("casks", []) if i.get("token") == data["token"]]
        if operation == "uninstall":
            if installed or any(os.path.lexists(b["path"]) for b in data["bundles"]):
                raise _error("Homebrew returned but removal could not be verified.", "verification_failed")
        else:
            if len(installed) != 1 or installed[0].get("installed") != data["metadata"]["version"]:
                raise _error("Homebrew returned but the approved target version was not verified.", "verification_failed")
            after = _bundle_evidence(installed[0], cancel=cancel)
            before_trust = [(v["path"], v["bundle_id"], v["signature"]["team_id"]) for v in data["bundles"]]
            if [(v["path"], v["bundle_id"], v["signature"]["team_id"]) for v in after] != before_trust:
                raise _error("Updated app signing identity changed; manual review is required.", "verification_failed")
        return {"operation": operation, "token": data["token"], "verified": True,
                "recovery_guidance": "Use the owning Homebrew package manager for reinstall or repair; automatic rollback is unavailable."}

    return execute_action_plan(store, options.get("record_id"), options.get("approve"), name, operation,
                               validate, execute, cancel=cancel, progress=progress)


def _leftovers(options, *, cancel=None, progress=None):
    from .inventory import scan_roots
    roots = _roots(options)
    inventory = scan_roots(roots, cancel=cancel, progress=progress, max_items=20000)
    app_roots = options["configuration"].get("application_roots", [])
    if not isinstance(app_roots, list) or any(not isinstance(v, str) for v in app_roots):
        raise _error("application_roots must be an array of explicitly selected roots.", "invalid_configuration")
    apps = adapters.scan_applications(app_roots, cancel=cancel) if app_roots else None
    ids = {item.get("bundle_id") for item in apps.get("items", [])} if apps else set()
    findings = []
    for item in inventory.get("items", []):
        path = Path(item["path"])
        if item["kind"] != "file":
            continue
        if path.suffix.lower() in (".dmg", ".pkg", ".mpkg"):
            findings.append({"id": item["id"], "path": item["path"], "kind": "installer", "eligible": False,
                             "logical_bytes": item["logical_size"], "ownership": "user-file", "reason": "Age and filename do not establish disposability; mounted/in-use state has not been verified. Review as a personal file."})
        elif path.suffix == ".plist" and path.stem.count(".") >= 2:
            owner = path.stem
            findings.append({"id": item["id"], "path": item["path"], "kind": "component-candidate", "eligible": False,
                             "bundle_id_hint": owner, "app_presence": "observed" if owner in ids else "unknown",
                             "ownership": "unproven", "reason": "An exact filename is a lead, not proof of ownership. Unselected, inaccessible, and offline app locations prevent an orphan claim."})
    inventory.update(tool="app-leftovers", items=findings, read_only=True, sources=SOURCES,
                     application_coverage=apps.get("coverage", []) if apps else [],
                     limitations=["No orphaned data is automatically selected or deleted. Shared groups, documents, licenses, and similarly named support paths stay protected."])
    return inventory


def dispatch(name, action, options, *, store, cancel=None, progress=None):
    specs = next((v for v in TOOL_SPECS if v["name"] == name), None)
    if specs is None or action not in specs["actions"]:
        raise _error("Unsupported application tool action.", "unsupported_action")
    if platform.system() != "Darwin":
        return {"tool": name, "status": "unavailable", "reason": "macOS is required; no provider command ran."}
    if not isinstance(options, dict) or not isinstance(options.get("configuration", {}), dict):
        raise _error("Tool options must be an object.", "invalid_configuration")
    options = dict(options, configuration=options.get("configuration", {}))
    if action == "plan":
        return _prepare_package(name, options, store, cancel=cancel)
    if action == "apply":
        return _apply_package(name, options, store, cancel=cancel, progress=progress)
    try:
        if name in ("applications", "architectures", "localizations"):
            report = _inspect_apps(name, options, cancel)
        elif name == "app-leftovers":
            report = _leftovers(options, cancel=cancel, progress=progress)
        else:
            report = _package_report(name, options, cancel=cancel)
    except (CleanMacError, OSError) as exc:
        if isinstance(exc, CleanMacError) and exc.code in ("scope_required", "invalid_configuration"):
            raise
        return {"tool": name, "status": "cancelled" if getattr(exc, "code", None) == "cancelled" else "unavailable",
                "read_only": True, "items": [], "reason": str(exc), "code": getattr(exc, "code", "metadata_denied"), "sources": SOURCES}
    return store.put("scans", report)

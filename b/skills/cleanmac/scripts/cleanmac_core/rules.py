"""Reviewed discovery rules and one narrow, content-verified cache handler.

This module never removes files or launches an owning application. It reads only
the caller's inventory, the trusted bundled catalog, process executable names,
and (for the conditional npm rule) selected-scope cache content. Unknown app
layouts and process state remain report-only. Re-run classification immediately
before planning/applying; findings are observations, not durable authorization.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Iterable

from .common import CleanMacError, digest


_CATALOG = Path(__file__).resolve().parents[2] / "references" / "cleanup-rules.json"
_MAX_FILE_HASH_BYTES = 64 * 1024 * 1024
_MAX_TOTAL_HASH_BYTES = 256 * 1024 * 1024
_WRITERS = {"node", "nodejs", "npm", "npx", "pnpm", "yarn", "corepack", "bun", "deno"}
_PROTECTED_COMPONENTS = {
    ".git", ".hg", ".svn", ".ssh", ".gnupg", ".aws", ".azure", ".kube",
    ".codex", ".claude", ".kimi", ".cursor", ".gemini", "node_modules",
}
_PROTECTED_NAMES = {
    "auth.json", "credentials", "credentials.json", "keychain", "passwords",
    "settings.json", "config.json", "preferences", ".npmrc", ".env",
    "license", "license.key", "licence", "licence.key",
}
_HARD_PACKAGE_SUFFIXES = (".app", ".photoslibrary", ".photolibrary", ".backupbundle", ".sparsebundle")
_KNOWN_MATCHERS = {"npm_sha512_content_v2", "prefix", "app_data", "electron_cache", "dropbox_cache"}


def load_rules() -> dict:
    """Read reviewed data without treating catalog contents as executable code."""
    try:
        catalog = json.loads(_CATALOG.read_text(encoding="utf-8"))
        if not isinstance(catalog, dict) or catalog.get("schema_version") != 1 or not isinstance(catalog.get("rules"), list):
            raise ValueError("Unsupported cleanup rule schema")
        seen = set()
        for rule in catalog["rules"]:
            if not isinstance(rule, dict):
                raise ValueError("Malformed cleanup rule")
            for field in ("id", "version", "app", "data_type", "mode", "matcher", "applicability", "effects"):
                if not isinstance(rule.get(field), str) or not rule[field]:
                    raise ValueError("Missing cleanup rule field: " + field)
            if rule["id"] in seen:
                raise ValueError("Duplicate cleanup rule ID")
            seen.add(rule["id"])
            if rule["matcher"] not in _KNOWN_MATCHERS or rule["mode"] not in {"conditional", "report-only"}:
                raise ValueError("Unsupported cleanup rule handler")
            if not isinstance(rule.get("roots"), list) or not rule["roots"]:
                raise ValueError("Missing cleanup rule roots")
            for root in rule["roots"]:
                if not isinstance(root, str) or Path(root).is_absolute() or ".." in Path(root).parts:
                    raise ValueError("Invalid cleanup rule root")
            if not isinstance(rule.get("provenance"), list) or not rule["provenance"]:
                raise ValueError("Missing rule provenance")
            if rule["mode"] == "conditional" and (rule["id"], rule["matcher"], rule["version"]) != (
                "npm-cacache-sha512-v2", "npm_sha512_content_v2", "1.0.0"
            ):
                raise ValueError("No implemented handler for conditional rule revision")
        if "unknown-app-data-review" not in seen:
            raise ValueError("Required unknown-data protection rule is missing")
        return catalog
    except (OSError, ValueError, TypeError) as exc:
        raise CleanMacError("Cleanup rules are unavailable or invalid", code="invalid_rules", details=str(exc)) from exc


def _home_path(home=None) -> Path:
    value = Path(home).expanduser() if home is not None else Path.home()
    if not value.is_absolute() or ".." in value.parts or value == Path("/"):
        raise CleanMacError("A specific absolute user home is required", code="invalid_home")
    # Do not resolve: a symlinked user-selected path must remain detectable.
    return value


def _no_symlink_directory(path: Path) -> bool:
    try:
        for ancestor in reversed((path, *path.parents)):
            info = os.lstat(ancestor)
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                return False
        return True
    except OSError:
        return False


def default_cache_roots(home=None) -> list[str]:
    """Return existing narrow discovery roots; choosing them still belongs to UI.

    App-data roots used only for classification are deliberately not default
    scopes. An unavailable location is not interpreted as an empty cache.
    """
    user_home = _home_path(home)
    relative = (
        "Library/Caches", ".npm/_cacache", ".gradle/caches", ".cache/pip",
        "Library/Developer/Xcode/DerivedData/ModuleCache.noindex",
        "Library/Application Support/Adobe/Common/Media Cache Files",
        "Library/Application Support/Spotify/PersistentCache/Storage",
    )
    return [str(user_home / value) for value in relative if _no_symlink_directory(user_home / value)]


def detect_active_apps() -> list[str] | None:
    """Read executable names only; never disclose argv which may contain secrets.

    None means unknown. The conservative npm handler treats any Node-family
    executable as a possible writer, including another agent using Node.
    """
    try:
        result = subprocess.run(
            ["/bin/ps", "-axo", "comm="], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            errors="replace", timeout=3, check=False,
        )
        if result.returncode or not result.stdout.strip() or len(result.stdout) > 1024 * 1024:
            return None
        return sorted({Path(line.strip()).name.casefold() for line in result.stdout.splitlines() if line.strip()})
    except (OSError, subprocess.SubprocessError):
        return None


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _app_data(path: Path, item: dict, home: Path, catalog: dict) -> bool:
    if item.get("is_app_data"):
        return True
    for rule in catalog["rules"]:
        if _matches_rule(path, home, rule):
            return True
    # User-selected external caches are still unverified, not personal files.
    return any(part.casefold() in {"cache", "caches", "_cacache"} for part in path.parts)


def _matches_rule(path: Path, home: Path, rule: dict) -> bool:
    if rule["matcher"] == "electron_cache":
        root = home / "Library" / "Application Support"
        if not _inside(path, root):
            return False
        segments = path.relative_to(root).parts
        return len(segments) >= 2 and segments[1] in {"Cache", "Code Cache", "GPUCache"}
    if rule["matcher"] == "dropbox_cache":
        if not _inside(path, home):
            return False
        segments = path.relative_to(home).parts
        if len(segments) >= 2 and "dropbox" in segments[0].casefold() and segments[1] == ".dropbox.cache":
            return True
        return len(segments) >= 4 and segments[:2] == ("Library", "Group Containers") and segments[2].endswith(".com.getdropbox.dropbox.sync") and segments[3] == "root-mount"
    return any(_inside(path, home / value) for value in rule["roots"])


def _protection(item: dict, path: Path) -> str | None:
    if item.get("protected"):
        return item.get("protection_reason") or "The inventory marks this item protected"
    if item.get("is_placeholder"):
        return "Cloud placeholders are not read or removed by cache rules"
    if item.get("kind") != "file":
        return "Only individual regular files can enter a cache cleanup plan"
    if item.get("nlink") != 1:
        return "Shared or unknown hard-link ownership is protected"
    parts = {part.casefold() for part in path.parts}
    if parts.intersection(_PROTECTED_COMPONENTS) or any(part.endswith(_HARD_PACKAGE_SUFFIXES) for part in parts):
        return "Agent, repository, credential, package, and backup internals are protected"
    name = path.name.casefold()
    if name in _PROTECTED_NAMES or name.startswith((".env.", "license.", "licence.")):
        return "Configuration, credentials, and licenses are protected"
    return None


def _same_identity(info, item: dict) -> bool:
    fields = {
        "device": info.st_dev, "inode": info.st_ino, "mode": info.st_mode,
        "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns,
        "logical_size": info.st_size, "nlink": info.st_nlink,
    }
    return all(type(item.get(name)) is int and item[name] == value for name, value in fields.items())


def _open_item(item: dict, path: Path) -> int:
    """Open from directory descriptors, refusing substituted ancestor links."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if not getattr(os, "O_NOFOLLOW", None) or not getattr(os, "O_DIRECTORY", None):
        raise OSError("No descriptor-relative no-follow support")
    parent_fd = os.open("/", flags | os.O_DIRECTORY)
    ancestors = item.get("ancestors")
    if not isinstance(ancestors, list) or not ancestors:
        os.close(parent_fd)
        raise OSError("Missing ancestor identity")
    expected = {value.get("path"): value for value in ancestors if isinstance(value, dict)}
    current = Path("/")
    try:
        for name in path.parts[1:-1]:
            next_fd = os.open(name, flags | os.O_DIRECTORY, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
            current /= name
            info = os.fstat(parent_fd)
            recorded = expected.get(str(current))
            # All ancestors at/under the inventory root must have captured identity.
            root = Path(item.get("root", str(path.parent)))
            if _inside(current, root) and recorded is None:
                raise OSError("Missing scoped ancestor identity")
            if recorded is not None and (info.st_dev, info.st_ino) != (recorded.get("device"), recorded.get("inode")):
                raise OSError("A parent directory changed since the scan")
        return os.open(path.name, flags | getattr(os, "O_NONBLOCK", 0), dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _verify_npm(item: dict, path: Path, home: Path, active_apps, budget: list[int], cancel=None) -> tuple[bool, str, dict]:
    if cancel and cancel():
        return False, "Cache verification cancelled", {"cancelled": True}
    root = home / ".npm" / "_cacache" / "content-v2" / "sha512"
    if not _inside(path, root):
        return False, "Only the verified content-v2/sha512 layout is supported; indexes and temporary data are preserved", {}
    segments = path.relative_to(root).parts
    if len(segments) != 3 or not all(re.fullmatch(pattern, value) for pattern, value in zip(
        (r"[0-9a-f]{2}", r"[0-9a-f]{2}", r"[0-9a-f]{124}"), segments
    )):
        return False, "This npm cache layout is unrecognized", {}
    if active_apps is None:
        return False, "Writer process state is unknown; cache content remains report-only", {"process_status": "unknown"}
    names = {Path(str(value)).name.casefold() for value in active_apps}
    writers = sorted(name for name in names if any(name == writer or name.startswith(writer + " ") for writer in _WRITERS))
    if writers:
        return False, "A Node/package-manager process may be using this cache; skip it or quit that process yourself", {"process_status": "active", "active_writers": writers}
    size = item.get("logical_size")
    if type(size) is not int or size < 0 or size > _MAX_FILE_HASH_BYTES or size > budget[0]:
        return False, "Content verification is outside the bounded scan budget; this item remains report-only", {}
    fd = None
    try:
        fd = _open_item(item, path)
        before = os.fstat(fd)
        dataless = getattr(stat, "SF_DATALESS", 0x40000000)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or not _same_identity(before, item):
            return False, "Ownership or file identity changed or could not be verified", {}
        if getattr(before, "st_flags", 0) & dataless:
            return False, "Cloud-only content is not read by cache rules", {}
        expected = "".join(segments)
        hasher = hashlib.sha512()
        consumed = 0
        while True:
            if cancel and cancel():
                return False, "Cache verification cancelled", {"cancelled": True}
            block = os.read(fd, min(1024 * 1024, size - consumed + 1))
            if not block:
                break
            consumed += len(block)
            budget[0] -= len(block)
            if consumed > size or budget[0] < 0:
                return False, "The cache file changed while verifying its content", {}
            hasher.update(block)
        after = os.fstat(fd)
        if consumed != size or not _same_identity(after, item):
            return False, "The cache file changed while verifying its content", {}
        if hasher.hexdigest() != expected:
            return False, "The content does not match its cache digest; no removal rule is enabled", {}
        return True, "Verified npm SHA-512 cache content; selecting it removes an offline cache copy and may require a later download", {"content_sha512": expected, "process_status": "inactive"}
    except (OSError, ValueError, TypeError) as exc:
        return False, "The cache file could not be verified without following links or losing its scanned identity", {"verification_error": type(exc).__name__}
    finally:
        if fd is not None:
            os.close(fd)


def classify_items(items: Iterable[dict], *, home=None, active_apps=None, cancel=None) -> list[dict]:
    """Return findings for cache/app-data candidates; ordinary files are omitted.

    Passing an explicit iterable of executable names supplies a current process
    snapshot (primarily for tests). None obtains a fresh snapshot. The caller
    must not substitute an empty list for a failed process query. Cancellation
    returns only completed findings; the caller owns the scan's cancelled state.
    """
    if cancel and cancel():
        return []
    catalog = load_rules()
    user_home = _home_path(home)
    active = detect_active_apps() if active_apps is None else active_apps
    if active is not None and (isinstance(active, (str, bytes, dict)) or not isinstance(active, (list, tuple, set, frozenset))):
        active = None
    findings = []
    budget = [_MAX_TOTAL_HASH_BYTES]
    fallback = next(rule for rule in catalog["rules"] if rule["id"] == "unknown-app-data-review")
    for item in items:
        if cancel and cancel():
            break
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("id"), str):
            continue
        path = Path(item["path"])
        if not path.is_absolute() or ".." in path.parts or "\x00" in str(path):
            continue
        if not _app_data(path, item, user_home, catalog):
            continue
        rule = next((candidate for candidate in catalog["rules"]
                     if candidate["matcher"] != "app_data" and _matches_rule(path, user_home, candidate)), fallback)
        eligible = False
        reason = _protection(item, path)
        extra = {}
        if reason is None and rule["mode"] == "conditional":
            eligible, reason, extra = _verify_npm(item, path, user_home, active, budget, cancel=cancel)
            if extra.get("cancelled"):
                break
        elif reason is None:
            reason = rule["effects"]
        finding = {
            "id": "finding-" + digest({"item_id": item["id"], "rule_id": rule["id"], "rule_version": rule["version"]})[:24],
            "item_id": item["id"], "category": "caches", "reason": reason,
            "confidence": "verified-format" if eligible else "report-only",
            "eligible": eligible, "rule_id": rule["id"], "rule_version": rule["version"],
            "rule_digest": digest(rule), "app": rule["app"], "data_type": rule["data_type"],
            "applicability": rule["applicability"], "provenance": rule["provenance"],
            "effects": rule["effects"], "prerequisites": ["fresh identity, ancestor, rule, and writer-state revalidation", "explicit selected-item approval"],
            **extra,
        }
        findings.append(finding)
    return findings

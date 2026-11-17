"""Explicit-account cloud metadata and single-item recoverable action plans.

Local eviction and remote deletion are different actions. No API here downloads
file contents, permanently deletes cloud files, or signs users into accounts.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from urllib.parse import quote, urlencode, urlsplit

from .cloud_http import CloudHTTP, CloudHTTPError, validate_url
from .common import CleanMacError, digest, display_path, new_id, utc_now


SOURCES = {
    "google-drive": ["https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list",
                     "https://developers.google.com/workspace/drive/api/guides/delete"],
    "onedrive": ["https://learn.microsoft.com/en-us/graph/api/driveitem-delete?view=graph-rest-1.0",
                 "https://learn.microsoft.com/en-us/graph/api/driveitem-restore?view=graph-rest-1.0"],
    "dropbox": ["https://docs.dropboxapi.com/dropbox-api/api-reference/user-endpoints/files/delete-v-2",
                "https://docs.dropboxapi.com/dropbox-api/api-reference/user-endpoints/files/restore"],
    "icloud": ["https://developer.apple.com/documentation/foundation/filemanager/evictubiquitousitem(at:)"],
}
PROVIDERS = ("google-drive", "onedrive", "dropbox")
DEFAULT_ENV = {name: "CLEANMAC_" + name.upper().replace("-", "_") + "_TOKEN" for name in PROVIDERS}
TOOL_SPECS = [
    {"name": "cloud-accounts", "title": "Cloud connection requirements", "feature_ids": ["CM-054"], "actions": ["inspect"]},
    {"name": "icloud", "title": "iCloud local storage", "feature_ids": ["CM-055"], "actions": ["inspect", "plan", "apply"], "scope_kind": "local"},
    *[{"name": name, "title": name.replace("-", " ").title() + " cloud review", "feature_ids": [fid],
       "actions": ["inspect", "plan", "apply", "restore-plan", "evict"], "scope_kind": "remote",
       "configuration": {"account_id": "Expected provider account ID; inspect without roots to identify it first.",
                         "oauth_connection": "Managed connection ID from the accounts tool; refresh keeps the same grant binding.",
                         "token_env": "Name of an environment variable containing an externally granted access token.",
                         "keychain_account": "Account name of an existing token entry in Keychain service cleanmac.<provider>.",
                         "drive_id": "Optional explicit shared Drive/OneDrive ID (shared/work files remain report-only).",
                         "namespace_id": "Optional Dropbox namespace ID.", "max_pages": "Page bound, default 20, maximum 100.",
                         "max_items": "Metadata item bound, default 2000, maximum 10000.",
                         "operation": "cloud-trash or cloud-restore; never local eviction."}}
      for name, fid in zip(PROVIDERS, ("CM-056", "CM-057", "CM-058"))],
]


def _text(value, label, *, empty=False):
    if not isinstance(value, str) or len(value) > 4096 or (not value and not empty) or any(ord(c) < 32 for c in value):
        raise CleanMacError("Provide a valid " + label + ".", "cloud_invalid_configuration")
    return value


def _mapping(value, label):
    if not isinstance(value, dict):
        raise CleanMacError("The provider returned an invalid " + label + ".", "cloud_invalid_response")
    return value


def _size(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise CleanMacError("The provider returned an invalid logical size.", "cloud_invalid_response")
    try:
        size = int(value)
    except ValueError:
        raise CleanMacError("The provider returned an invalid logical size.", "cloud_invalid_response") from None
    if not 0 <= size <= 2**63 - 1:
        raise CleanMacError("The provider returned an invalid logical size.", "cloud_invalid_response")
    return size


def _flags(raw, names):
    for name in names:
        if name in raw and type(raw[name]) is not bool:
            raise CleanMacError("The provider returned an invalid Boolean metadata field.", "cloud_invalid_response")


def _version(value):
    if not isinstance(value, str) or len(value) > 512 or any(ord(char) < 32 for char in value):
        raise CleanMacError("The provider returned an invalid item version.", "cloud_invalid_response")
    return value


def _checked_token(token):
    if not isinstance(token, str) or not token or len(token) > 32768 or any(ord(char) <= 32 for char in token):
        raise CleanMacError("The selected credential is not a valid bearer token.", "cloud_auth_required")
    return token


def _configuration(options):
    value = options.get("configuration", {})
    if not isinstance(value, dict):
        raise CleanMacError("Cloud configuration must be an object.", "cloud_invalid_configuration")
    allowed = {"oauth_connection", "token_env", "keychain_account", "account_id", "drive_id", "namespace_id", "max_pages", "max_items", "operation"}
    if set(value) - allowed:
        raise CleanMacError("Cloud configuration accepts credential references, not access tokens or arbitrary endpoints.", "cloud_invalid_configuration")
    return dict(value)


def _credential(provider, config, store=None):
    if config.get("oauth_connection"):
        if config.get("token_env") or config.get("keychain_account"):
            raise CleanMacError("Choose one credential source for this cloud account.", "cloud_invalid_configuration")
        from . import oauth
        connection = _text(config["oauth_connection"], "OAuth connection ID")
        metadata = oauth.connection_metadata(store, connection, provider=provider)
        token = _checked_token(oauth.access_token(store, connection, provider=provider))
        binding = digest({key: metadata[key] for key in ("provider", "account_id", "grant_id", "scopes")})
        return token, {"oauth_connection": connection}, binding
    if config.get("keychain_account"):
        account = _text(config["keychain_account"], "Keychain account")
        if sys.platform != "darwin":
            raise CleanMacError("Keychain credentials require macOS.", "cloud_auth_required")
        try:
            result = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", "cleanmac." + provider,
                                     "-a", account, "-w"], capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise CleanMacError("The selected Keychain credential is unavailable.", "cloud_auth_required") from None
        token = result.stdout.strip() if result.returncode == 0 else ""
        reference = {"keychain_account": account}
    else:
        env = config.get("token_env", DEFAULT_ENV[provider])
        if not isinstance(env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}", env):
            raise CleanMacError("token_env must name an environment variable, not contain a token.", "cloud_invalid_configuration")
        token = os.environ.get(env, "")
        reference = {"token_env": env}
    if not token:
        raise CleanMacError("Connect an OAuth account and provide its access token through the selected environment variable or Keychain entry.",
                            "cloud_auth_required", {"provider": provider, "credential_reference": reference,
                                                    "oauth": "Use the provider's authorization-code flow with PKCE; this skill never asks for your password."})
    token = _checked_token(token)
    # Binding changes when credentials/scopes are replaced. The bearer itself
    # stays in memory only, never in a scan, plan, receipt, or error message.
    binding = hashlib.sha256((provider + "\0" + token).encode()).hexdigest()
    return token, reference, binding


def _limit(config, key, default, upper):
    value = config.get(key, default)
    if type(value) is not int or not 1 <= value <= upper:
        raise CleanMacError(f"{key} must be between 1 and {upper}.", "cloud_invalid_configuration")
    return value


def _item_base(raw, provider, account):
    _mapping(raw, "metadata item")
    identifier = _text(raw.get("id"), "provider item ID")
    return {"id": "cloud-" + digest([provider, account, identifier])[:24], "provider_id": identifier,
            "name": str(raw.get("name", ""))[:4096], "display_name": display_path(str(raw.get("name", ""))[:4096]),
            "provider": provider, "local_presence": "unknown", "eligible_actions": [], "selected": False}


def _mutation_call(callback):
    """A definite rejection of the first mutating request has no effect.

    Never wrap post-mutation reads with this conversion: verification can fail
    after the provider has already changed the item.
    """
    from .actions import ActionNotStarted
    try:
        return callback()
    except CloudHTTPError as error:
        if error.status in (401, 403, 404, 409, 412, 429):
            raise ActionNotStarted(error.message, code=error.code, details=error.details) from None
        raise


class GoogleDrive:
    name = "google-drive"
    base = "https://www.googleapis.com/drive/v3/"
    fields = "id,name,mimeType,size,version,modifiedTime,parents,ownedByMe,shared,trashed,driveId,shortcutDetails,capabilities(canTrash,canUntrash),contentRestrictions"

    def __init__(self, http, config):
        self.http, self.config = http, config

    def account(self):
        data = self.http.request("GET", self.base + "about?fields=user(permissionId)")
        user = _mapping(_mapping(data, "account").get("user"), "account user")
        return {"id": _text(user.get("permissionId"), "Drive account ID"), "provider": self.name}

    def page(self, root, cursor=None):
        if root == "root" and self.config.get("drive_id"):
            root = self.config["drive_id"]
        root = _text(root, "Drive folder ID")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", root):
            raise CleanMacError("Use an explicit Drive folder ID, or root.", "cloud_invalid_scope")
        parameters = {"q": f"'{root}' in parents", "fields": f"nextPageToken,incompleteSearch,files({self.fields})",
                      "pageSize": 100, "corpora": "user", "spaces": "drive"}
        if self.config.get("drive_id"):
            parameters.update(corpora="drive", driveId=_text(self.config["drive_id"], "shared Drive ID"),
                              supportsAllDrives="true", includeItemsFromAllDrives="true")
        if cursor:
            parameters["pageToken"] = cursor
        data = self.http.request("GET", self.base + "files?" + urlencode(parameters))
        _mapping(data, "metadata page")
        _flags(data, ("incompleteSearch",))
        return data.get("files", []), data.get("nextPageToken"), bool(data.get("incompleteSearch"))

    def get(self, item):
        return self.http.request("GET", self.base + "files/" + quote(item["provider_id"], safe="") + "?" +
                                 urlencode({"fields": self.fields, "supportsAllDrives": "true"}))

    def normalize(self, raw, account):
        item = _item_base(raw, self.name, account)
        _flags(raw, ("shared", "trashed", "ownedByMe"))
        capabilities = _mapping(raw.get("capabilities", {}), "capabilities")
        _flags(capabilities, ("canTrash", "canUntrash"))
        version = _version(raw.get("version", ""))
        parents = raw.get("parents", [])
        if not isinstance(parents, list) or any(not isinstance(parent, str) or not parent for parent in parents):
            raise CleanMacError("The provider returned invalid parent metadata.", "cloud_invalid_response")
        mime = raw.get("mimeType", "")
        item.update(version=version, size=_size(raw.get("size", 0)), parents=parents,
                    shared=raw.get("shared") is not False or bool(raw.get("driveId")), trashed=raw.get("trashed") is True,
                    kind="folder" if mime == "application/vnd.google-apps.folder" else "shortcut" if "shortcutDetails" in raw else "file" if isinstance(mime, str) and mime else "unknown",
                    owned=raw.get("ownedByMe") is True, restricted=bool(raw.get("contentRestrictions")))
        capability = "canUntrash" if item["trashed"] else "canTrash"
        if (item["kind"] == "file" and item["owned"] and not item["shared"] and not item["restricted"]
                and item["version"].isdigit() and parents and capabilities.get(capability) is True):
            item["eligible_actions"] = ["cloud-restore" if item["trashed"] else "cloud-trash"]
        item["recovery_policy"] = "Google Drive Trash is normally retained 30 days; account policy or manual removal may shorten availability."
        item["race_limit"] = "Drive v3 has no documented version precondition for this metadata mutation; identity and version are rechecked immediately before it."
        return item

    def mutate(self, item, operation, account):
        data = _mutation_call(lambda: self.http.request("PATCH", self.base + "files/" + quote(item["provider_id"], safe="") + "?" +
                                 urlencode({"fields": self.fields, "supportsAllDrives": "true"}), {"trashed": operation == "cloud-trash"}))
        actual = self.normalize(data, account)
        if actual["provider_id"] != item["provider_id"] or actual["trashed"] != (operation == "cloud-trash"):
            raise CleanMacError("The cloud mutation response could not be verified.", "cloud_verification_failed")
        return {"item": actual, "verified": True, "restore_handle": {"provider_id": actual["provider_id"]} if operation == "cloud-trash" else None}


class OneDrive:
    name = "onedrive"
    base = "https://graph.microsoft.com/v1.0/"
    fields = "id,name,size,eTag,cTag,file,folder,deleted,remoteItem,shared,parentReference,publication"

    def __init__(self, http, config):
        self.http, self.config = http, config
        self.drive = None

    def account(self):
        user = self.http.request("GET", self.base + "me?$select=id")
        requested = self.config.get("drive_id")
        self.drive = self.http.request("GET", self.base + ("drives/" + quote(_text(requested, "drive ID"), safe="") if requested else "me/drive") + "?$select=id,driveType,owner")
        _mapping(user, "account user")
        _mapping(self.drive, "drive")
        owner = _mapping(self.drive.get("owner", {}), "drive owner")
        owner_user = _mapping(owner.get("user", {}), "drive owner user")
        return {"id": _text(user.get("id"), "Microsoft account ID"), "provider": self.name,
                "drive_id": _text(self.drive.get("id"), "OneDrive ID"), "drive_type": self.drive.get("driveType"),
                "owner_id": owner_user.get("id")}

    def _path(self):
        return self.base + "drives/" + quote(self.drive["id"], safe="") + "/"

    def page(self, root, cursor=None):
        root = _text(root, "OneDrive folder ID")
        target = self._path() + ("root" if root == "root" else "items/" + quote(root, safe="")) + "/children"
        if cursor:
            validate_url(self.name, cursor)
            # Pagination may not redirect into a different drive or API family.
            if urlsplit(cursor).path != urlsplit(target).path:
                raise CleanMacError("OneDrive pagination changed the selected drive.", "cloud_invalid_endpoint")
            url = cursor
        else:
            url = target + "?" + urlencode({"$select": self.fields, "$top": 100})
        data = self.http.request("GET", url)
        _mapping(data, "metadata page")
        return data.get("value", []), data.get("@odata.nextLink"), False

    def get(self, item):
        return self.http.request("GET", self._path() + "items/" + quote(item["provider_id"], safe="") + "?" + urlencode({"$select": self.fields}))

    def normalize(self, raw, account):
        item = _item_base(raw, self.name, account)
        publication = _mapping(raw.get("publication", {}), "publication")
        parent = _mapping(raw.get("parentReference", {}), "parent reference")
        for key in ("file", "folder", "deleted", "shared", "remoteItem"):
            if key in raw:
                _mapping(raw[key], key + " facet")
        item.update(version=_version(raw.get("eTag", "")), size=_size(raw.get("size", 0)), parent=parent,
                    shared="shared" in raw or "remoteItem" in raw, trashed="deleted" in raw,
                    kind="folder" if "folder" in raw else "file" if "file" in raw else "unknown",
                    locked=publication.get("level") == "checkout")
        personal_owned = account.get("drive_type") == "personal" and account.get("owner_id") == account["id"]
        if personal_owned and item["kind"] == "file" and not item["shared"] and not item["locked"] and item["version"] and not item["trashed"]:
            item["eligible_actions"] = ["cloud-trash"]
        item["recovery_policy"] = "OneDrive recycle-bin retention depends on account policy. Only owned personal-drive files are actionable in this adapter; work/shared drives are report-only."
        return item

    def mutate(self, item, operation, account):
        if operation == "cloud-restore":
            raise CleanMacError("Use the OneDrive recycle bin to restore this item with conflict review.", "cloud_restore_handoff")
        _mutation_call(lambda: self.http.request("DELETE", self._path() + "items/" + quote(item["provider_id"], safe=""), headers={"If-Match": item["version"]}))
        try:
            current = self.get(item)
        except CloudHTTPError as error:
            if error.status != 404:
                raise
        else:
            if "deleted" not in current:
                raise CleanMacError("The deleted OneDrive item remains visible; inspect the receipt before retrying.", "cloud_verification_failed")
        return {"item": dict(item, trashed=True), "verified": True, "verification": "Recycle-bin DELETE accepted; item no longer available at its active ID.",
                "restore_handle": {"provider_id": item["provider_id"], "drive_id": account["drive_id"]}}


class Dropbox:
    name = "dropbox"
    base = "https://api.dropboxapi.com/2/"

    def __init__(self, http, config):
        self.http, self.config = http, config
        self.headers = {}
        if config.get("namespace_id"):
            namespace = _text(config["namespace_id"], "Dropbox namespace ID")
            if not namespace.isdigit():
                raise CleanMacError("Dropbox namespace IDs must be numeric.", "cloud_invalid_configuration")
            self.headers["Dropbox-API-Path-Root"] = json.dumps({".tag": "namespace_id", "namespace_id": namespace})

    def call(self, endpoint, body=None):
        return self.http.request("POST", self.base + endpoint, body, headers=self.headers)

    def account(self):
        data = self.call("users/get_current_account")
        _mapping(data, "account")
        root_info = _mapping(data.get("root_info", {}), "namespace information")
        return {"id": _text(data.get("account_id"), "Dropbox account ID"), "provider": self.name,
                "namespace_id": self.config.get("namespace_id") or root_info.get("home_namespace_id"),
                "namespace_owned": bool(root_info.get("home_namespace_id")) and (not self.config.get("namespace_id") or self.config["namespace_id"] == root_info.get("home_namespace_id")),
                "team": bool(data.get("team"))}

    def page(self, root, cursor=None):
        if cursor:
            data = self.call("files/list_folder/continue", {"cursor": cursor})
        else:
            path = "" if root == "root" else _text(root, "Dropbox folder path")
            if path and not path.startswith(("/", "id:", "ns:")):
                raise CleanMacError("Dropbox scope must be an explicit folder path or ID.", "cloud_invalid_scope")
            data = self.call("files/list_folder", {"path": path, "recursive": False, "include_deleted": False,
                                                   "include_has_explicit_shared_members": True, "limit": 100})
        _mapping(data, "metadata page")
        if type(data.get("has_more")) is not bool:
            raise CleanMacError("Dropbox pagination omitted its completion state.", "cloud_invalid_response")
        cursor = data.get("cursor") if data.get("has_more") else None
        if data.get("has_more") and not cursor:
            raise CleanMacError("Dropbox pagination omitted its cursor.", "cloud_invalid_response")
        return data.get("entries", []), cursor, False

    def get(self, item):
        return self.call("files/get_metadata", {"path": item["provider_id"], "include_deleted": True, "include_has_explicit_shared_members": True})

    def normalize(self, raw, account):
        item = _item_base(raw, self.name, account)
        _flags(raw, ("has_explicit_shared_members",))
        item.update(version=_version(raw.get("rev", "")), size=_size(raw.get("size", 0)), path=raw.get("path_lower", ""),
                    shared=bool(raw.get("sharing_info")) or raw.get("has_explicit_shared_members") is not False,
                    locked=bool(raw.get("file_lock_info")), trashed=raw.get(".tag") == "deleted", kind=raw.get(".tag", "unknown"))
        if (item["kind"] == "file" and not item["shared"] and not item["locked"] and re.fullmatch(r"[0-9a-f]{9,}", item["version"])
                and not account.get("team") and account.get("namespace_owned", False)):
            item["eligible_actions"] = ["cloud-trash"]
        item["recovery_policy"] = "Dropbox deleted-file recovery depends on account plan and policy; no unlimited retention is promised. Team and shared files remain report-only."
        return item

    def mutate(self, item, operation, account):
        if operation == "cloud-restore":
            # restore can replace a newer file and lacks a conditional create;
            # automatic restore is deliberately unavailable until no-overwrite
            # semantics are exposed by the provider. The receipt retains rev.
            raise CleanMacError("Restore this exact revision in Dropbox Deleted files; the public restore API cannot guarantee preserving a new file at the original path.", "cloud_restore_handoff")
        data = _mutation_call(lambda: self.call("files/delete_v2", {"path": item["provider_id"], "parent_rev": item["version"]}))
        actual = self.normalize(data.get("metadata", {}), account)
        if actual["provider_id"] != item["provider_id"] or actual["version"] != item["version"]:
            raise CleanMacError("The Dropbox deletion response did not match the approved file revision.", "cloud_verification_failed")
        return {"item": dict(actual, trashed=True), "verified": True,
                "restore_handle": {"provider_id": item["provider_id"], "path": actual["path"], "revision": actual["version"]},
                "restore_handoff": "Use Dropbox Deleted files to restore this recorded revision without overwriting a newer path."}


ADAPTERS = {"google-drive": GoogleDrive, "onedrive": OneDrive, "dropbox": Dropbox}


def _connect(provider, config, *, expected=None, store=None):
    token, reference, binding = _credential(provider, config, store)
    adapter = ADAPTERS[provider](CloudHTTP(provider, token), config)
    account = adapter.account()
    if config.get("account_id") and account["id"] != config["account_id"]:
        raise CleanMacError("The credential belongs to a different account than the selected account.", "cloud_account_changed")
    context = {"account": account, "credential_reference": reference, "credential_binding": binding,
               "drive_id": config.get("drive_id"), "namespace_id": config.get("namespace_id")}
    if expected is not None and context != expected:
        raise CleanMacError("The account, drive, namespace, or credential grant changed. Make a new plan.", "cloud_account_changed")
    return adapter, context


def _safe_config(context):
    return dict(context["credential_reference"], account_id=context["account"]["id"],
                **{key: context[key] for key in ("drive_id", "namespace_id") if context.get(key)})


def _inspect(provider, options, config, store, cancel, progress):
    try:
        adapter, context = _connect(provider, config, store=store)
    except CleanMacError as error:
        return {"tool": provider, "status": "dependency-needed" if error.code == "cloud_auth_required" else "unavailable",
                "coverage": "unscanned", "error": error.as_dict(), "sources": SOURCES[provider]}
    roots = options.get("roots", [])
    if not isinstance(roots, list) or len(roots) > 20 or any(not isinstance(root, str) or not root for root in roots):
        raise CleanMacError("Choose between one and twenty explicit remote folder IDs or paths.", "cloud_invalid_scope")
    if not roots or not config.get("account_id"):
        return {"tool": provider, "status": "scope-needed", "account": context["account"], "coverage": "unscanned",
                "message": "Choose this account_id and explicit remote folder IDs/paths (root selects the account root). Listing is one level per selected folder; no file contents are read."}
    max_pages, max_items = _limit(config, "max_pages", 20, 100), _limit(config, "max_items", 2000, 10000)
    scan = {"schema_version": 1, "id": new_id("scan"), "kind": "cloud_inventory", "tool": provider,
            "created_at": utc_now(), "status": "complete", "context": context, "roots": roots,
            "items": [], "errors": [], "pages": 0, "scope_depth": "direct-children",
            "bytes_basis": "remote logical metadata; not estimated local reclaimed storage", "sources": SOURCES[provider]}
    seen_items, seen_pages = set(), set()
    for root in roots:
        cursor = None
        while True:
            if cancel and cancel():
                scan["status"] = "cancelled"
                break
            if scan["pages"] >= max_pages or len(scan["items"]) >= max_items:
                scan["status"] = "partial"
                scan["errors"].append({"code": "cloud_inventory_limit", "root": root})
                break
            try:
                rows, next_cursor, incomplete = adapter.page(root, cursor)
                scan["pages"] += 1
                if not isinstance(rows, list):
                    raise CleanMacError("Provider metadata list is invalid.", "cloud_invalid_response")
                for raw in rows:
                    if not isinstance(raw, dict):
                        raise CleanMacError("Provider metadata item is invalid.", "cloud_invalid_response")
                    try:
                        item = adapter.normalize(raw, context["account"])
                    except (ValueError, TypeError, OverflowError):
                        raise CleanMacError("Provider metadata item is invalid.", "cloud_invalid_response") from None
                    if item["id"] in seen_items:
                        continue
                    if len(scan["items"]) >= max_items:
                        scan["status"] = "partial"
                        scan["errors"].append({"code": "cloud_inventory_limit", "root": root})
                        break
                    seen_items.add(item["id"])
                    scan["items"].append(item)
                if incomplete:
                    scan["status"] = "partial"
                    scan["errors"].append({"code": "provider_incomplete_search", "root": root})
                if progress:
                    progress({"phase": "cloud_metadata", "items": len(scan["items"]), "pages": scan["pages"]})
                if not next_cursor or len(scan["items"]) >= max_items:
                    if next_cursor:
                        scan["status"] = "partial"
                    break
                _text(next_cursor, "provider pagination cursor")
                key = (root, next_cursor)
                if key in seen_pages:
                    raise CleanMacError("Provider pagination repeated a cursor.", "cloud_pagination_cycle")
                seen_pages.add(key)
                cursor = next_cursor
            except CleanMacError as error:
                scan["status"] = "partial" if scan["items"] else "unavailable"
                scan["errors"].append(dict(error.as_dict(), root=root))
                break
        if scan["status"] == "cancelled":
            break
    scan["counts"] = {"items": len(scan["items"]), "eligible": sum(bool(item["eligible_actions"]) for item in scan["items"])}
    return store.put("scans", scan)


def _current(adapter, item, account):
    try:
        actual = adapter.normalize(adapter.get(item), account)
    except (ValueError, TypeError, OverflowError):
        raise CleanMacError("Provider metadata item is invalid.", "cloud_invalid_response") from None
    if actual != item:
        raise CleanMacError("The selected cloud item, permissions, sharing, or revision changed. Scan it again.", "cloud_item_changed")
    return actual


def _remote_plan(provider, options, config, store):
    from .actions import create_action_plan
    scan = store.get("scans", options.get("record_id"))
    if scan.get("kind") != "cloud_inventory" or scan.get("tool") != provider:
        raise CleanMacError("Select a saved inventory for this cloud provider.", "cloud_invalid_record")
    selected = options.get("selected", [])
    if not isinstance(selected, list) or len(selected) != 1:
        raise CleanMacError("Choose exactly one cloud file per action plan; each receives an independent recoverable receipt.", "cloud_invalid_selection")
    item = next((row for row in scan["items"] if row["id"] == selected[0]), None)
    if item is None:
        raise CleanMacError("The selected file is not in this cloud inventory.", "cloud_invalid_selection")
    operation = config.get("operation", "cloud-restore" if item["trashed"] else "cloud-trash")
    if operation not in ("cloud-trash", "cloud-restore") or operation not in item["eligible_actions"]:
        raise CleanMacError("This file is not eligible for the selected cloud action. Shared, locked, folder, shortcut, and uncertain items remain report-only.", "cloud_action_unavailable")
    cfg = dict(_safe_config(scan["context"]), **config)
    adapter, context = _connect(provider, cfg, expected=scan["context"], store=store)
    _current(adapter, item, context["account"])
    payload = {"context": context, "item": item, "operation": operation, "source_scan_id": scan["id"]}
    return create_action_plan(store, provider, operation, payload,
                              {"action": operation, "account": context["account"], "file": item["name"],
                               "remote_item_id": item["provider_id"], "recovery": item["recovery_policy"],
                               "local_storage_effect": "This changes the cloud account and syncs to connected devices. It is not local eviction.",
                               "race_limit": item.get("race_limit")})


def _remote_apply(provider, options, config, store, cancel, progress):
    from .actions import execute_action_plan, checkpoint_action, validate_action_plan
    plan = store.get("plans", options.get("record_id"))
    operation = plan.get("action")
    if operation not in ("cloud-trash", "cloud-restore"):
        raise CleanMacError("This is not a supported cloud plan.", "cloud_invalid_record")
    validate_action_plan(plan, tool=provider, action=operation)
    cfg = dict(_safe_config(plan["payload"]["context"]), **config)
    connected = {}

    def validate(current_plan):
        adapter, context = _connect(provider, cfg, expected=current_plan["payload"]["context"], store=store)
        connected.update(adapter=adapter, context=context)
        item = current_plan["payload"]["item"]
        if operation not in item.get("eligible_actions", []):
            raise CleanMacError("The selected cloud action is no longer eligible.", "cloud_action_unavailable")
        _current(adapter, item, context["account"])

    def execute(current_plan):
        adapter, context = connected["adapter"], connected["context"]
        item = current_plan["payload"]["item"]
        # Revalidate after the receipt becomes running; changed metadata cannot
        # be confused with success, nor automatically retried after a timeout.
        _current(adapter, item, context["account"])
        result = adapter.mutate(item, operation, context["account"])
        checkpoint_action(result)
        return dict(result, action=operation, provider=provider, account=context["account"], recovery_policy=item["recovery_policy"])

    return execute_action_plan(store, plan["id"], options.get("approve"), provider, operation, validate, execute,
                               cancel=cancel, progress=progress)


def _restore_plan(provider, options, config, store):
    from .actions import create_action_plan, validate_action_receipt
    receipt = store.get("receipts", options.get("record_id"))
    original = store.get("plans", receipt.get("plan_id"))
    receipt = validate_action_receipt(receipt, original)
    if receipt.get("status") != "completed" or receipt.get("tool") != provider or receipt.get("action") != "cloud-trash":
        raise CleanMacError("Choose a completed cloud-trash receipt from this provider.", "cloud_invalid_record")
    context = original["payload"]["context"]
    item = receipt.get("result", {}).get("item")
    if not isinstance(item, dict) or item.get("provider_id") != original["payload"]["item"]["provider_id"]:
        raise CleanMacError("The receipt does not bind its recovery item.", "cloud_invalid_record")
    if provider != "google-drive":
        return {"tool": provider, "status": "manual-action-required", "action": "cloud-restore", "receipt_id": receipt["id"],
                "restore_handle": receipt["result"].get("restore_handle"),
                "message": "Restore this exact item through the provider's recycle-bin/deleted-files interface. The adapter cannot guarantee a conflict-free restore through this provider API.",
                "recovery_policy": item["recovery_policy"]}
    adapter, current_context = _connect(provider, dict(_safe_config(context), **config), expected=context, store=store)
    actual = adapter.normalize(adapter.get(item), current_context["account"])
    if "cloud-restore" not in actual["eligible_actions"]:
        raise CleanMacError("The remote item cannot currently be restored.", "cloud_action_unavailable")
    return create_action_plan(store, provider, "cloud-restore", {"context": context, "item": actual,
                              "operation": "cloud-restore", "source_receipt_id": receipt["id"]},
                              {"action": "cloud-restore", "account": context["account"], "file": actual["name"], "remote_item_id": actual["provider_id"]})


def _cloud_helper(command, value=None):
    path = Path(os.environ.get("CLEANMAC_CLOUD_PATH", str(Path(__file__).resolve().parents[2] / "native/bin/cleanmac-cloud")))
    try:
        metadata = path.lstat()
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid not in (os.getuid(), 0)
                or metadata.st_mode & 0o022 or not os.access(path, os.X_OK)):
            raise CleanMacError("The iCloud helper must be a trusted-owner executable without group/world write access.", "cloud_helper_unsafe")
        if command != "info":
            handshake = subprocess.run([str(path), "info"], capture_output=True, text=True, timeout=5, check=False)
            info = json.loads(handshake.stdout) if len(handshake.stdout) < 65536 else None
            if (handshake.returncode or not isinstance(info, dict) or info.get("protocol_version") != 1
                    or info.get("privileged") is not False or "local-evict" not in info.get("capabilities", [])):
                raise CleanMacError("The native cloud helper has an incompatible protocol.", "cloud_helper_incompatible")
        result = subprocess.run([str(path), command], input=json.dumps(value or {}), capture_output=True,
                                text=True, timeout=30, check=False)
        if len(result.stdout) > 8 * 1024 * 1024:
            raise CleanMacError("The native cloud response exceeded its limit.", "cloud_response_limit")
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            raise ValueError()
        if result.returncode or data.get("error"):
            native_error = data.get("error") if isinstance(data.get("error"), dict) else {}
            raise CleanMacError("The native iCloud operation could not complete. Review the selected scope and permission state.",
                                native_error.get("code", "cloud_native_failed"))
        return data
    except FileNotFoundError:
        raise CleanMacError("Build the optional iCloud helper with sh native/build-cloud.sh; no local file was changed.", "cloud_helper_required") from None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise CleanMacError("The iCloud helper did not return a valid result. Do not retry an interrupted eviction without checking the receipt.", "cloud_native_failed") from None


def _icloud(action, options, store, cancel, progress):
    from .actions import create_action_plan, execute_action_plan, validate_action_plan
    if action == "inspect":
        roots = options.get("roots", [])
        if not roots:
            return {"tool": "icloud", "status": "scope-needed", "coverage": "unscanned", "message": "Choose an explicit local iCloud folder. No account or filesystem probe was made."}
        if len(roots) != 1:
            raise CleanMacError("Choose one iCloud folder per inventory.", "cloud_invalid_scope")
        try:
            data = _cloud_helper("inspect", {"root": os.path.abspath(os.path.expanduser(roots[0]))})
        except CleanMacError as error:
            return {"tool": "icloud", "status": "dependency-needed" if error.code == "cloud_helper_required" else "unavailable", "coverage": "unscanned", "error": error.as_dict()}
        return store.put("scans", dict(data, schema_version=1, id=new_id("scan"), tool="icloud", kind="icloud_inventory", created_at=utc_now()))
    if action == "plan":
        scan = store.get("scans", options.get("record_id"))
        selected = options.get("selected", [])
        if scan.get("kind") != "icloud_inventory" or scan.get("tool") != "icloud" or len(selected) != 1:
            raise CleanMacError("Choose one item from a saved iCloud inventory.", "cloud_invalid_selection")
        item = next((item for item in scan["items"] if item["id"] == selected[0]), None)
        if item is None or not item.get("eligible"):
            raise CleanMacError("Only an uploaded, current, conflict-free regular iCloud file can be evicted.", "cloud_action_unavailable")
        request = {"root": scan["root"], "account": scan["account"], "item": item}
        _cloud_helper("validate", request)
        return create_action_plan(store, "icloud", "local-evict", request,
                                  {"action": "local-evict", "file": item["path"], "effect": "Remove the local download while keeping the remote iCloud file. Finder can download it again."})
    if action == "apply":
        plan = store.get("plans", options.get("record_id"))
        validate_action_plan(plan, tool="icloud", action="local-evict")
        return execute_action_plan(store, plan["id"], options.get("approve"), "icloud", "local-evict",
                                   lambda record: _cloud_helper("validate", record["payload"]),
                                   lambda record: _cloud_helper("evict", record["payload"]), cancel=cancel, progress=progress)
    raise CleanMacError("Unsupported iCloud action.", "cloud_action_unavailable")


def dispatch(name, action, options, *, store, cancel=None, progress=None):
    if name == "cloud-accounts" and action == "inspect":
        return {"tool": name, "status": "complete", "network_access": False, "accounts_probed": False,
                "providers": [{"provider": provider, "token_env": DEFAULT_ENV[provider], "keychain_service": "cleanmac." + provider,
                               "credential_present": bool(os.environ.get(DEFAULT_ENV[provider])), "access": "unverified",
                               "sources": SOURCES[provider]} for provider in PROVIDERS],
                "connection": "Use the accounts tool for a managed OAuth connection, or supply an externally authorized access token through an environment reference or existing Keychain entry. Passwords and raw tokens are never accepted in command options. Inspect with no roots to identify the account, then choose account_id and remote roots.",
                "disconnect": "Remove the environment credential or Keychain entry and revoke this app's grant in the provider account settings. This skill stores no access/refresh tokens.",
                "limitations": "Provider app registration and user consent are required. External environment/Keychain access tokens need renewal by their issuer; managed connections own their grant and refresh lifecycle."}
    if name == "icloud":
        return _icloud(action, options, store, cancel, progress)
    if name not in ADAPTERS:
        raise CleanMacError("Unknown cloud tool.", "cloud_invalid_tool")
    if action == "evict":
        return {"tool": name, "status": "manual-action-required", "action": "local-evict", "network_access": False,
                "message": "Use the sync client's supported online-only/free-up-space action. Cloud API deletion would remove the remote file and is never substituted for local eviction."}
    config = _configuration(options)
    if action == "inspect":
        return _inspect(name, options, config, store, cancel, progress)
    if action == "plan":
        return _remote_plan(name, options, config, store)
    if action == "apply":
        return _remote_apply(name, options, config, store, cancel, progress)
    if action == "restore-plan":
        return _restore_plan(name, options, config, store)
    raise CleanMacError("Unsupported cloud action.", "cloud_action_unavailable")

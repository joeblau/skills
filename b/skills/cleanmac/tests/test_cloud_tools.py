"""Observable cloud contracts using private local state and fake providers only."""
from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess
import types
import unittest
from unittest.mock import patch
import urllib.error
from urllib.parse import urlsplit, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cleanmac_core import cloud_tools as cloud
from cleanmac_core.cloud_http import CloudHTTP, CloudHTTPError, _NoRedirect, validate_url
from cleanmac_core.common import CleanMacError
from cleanmac_core.state import StateStore


def google_file(**changes):
    return dict({"id": "file1", "name": "example.txt", "size": "14", "version": "1",
                 "mimeType": "text/plain", "parents": ["root"], "ownedByMe": True, "shared": False,
                 "trashed": False, "capabilities": {"canTrash": True, "canUntrash": True}}, **changes)


class FakeHTTP:
    def __init__(self):
        self.calls = []
        self.items = {"file1": google_file()}
        self.account_id = "account1"
        self.page_failure = None
        self.next_cursor = None
        self.patch_failure = None

    def request(self, method, url, body=None, headers=None):
        self.calls.append((method, url, copy.deepcopy(body), copy.deepcopy(headers)))
        path = urlsplit(url).path
        if path.endswith("/about"):
            return {"user": {"permissionId": self.account_id}}
        if path.endswith("/files"):
            if "pageToken" in parse_qs(urlsplit(url).query) and self.page_failure:
                raise self.page_failure
            data = {"files": list(copy.deepcopy(self.items).values())}
            if self.next_cursor:
                data["nextPageToken"] = self.next_cursor
            return data
        identifier = path.split("/")[-1]
        if method == "PATCH":
            if self.patch_failure:
                raise self.patch_failure
            self.items[identifier]["trashed"] = body["trashed"]
            self.items[identifier]["version"] = str(int(self.items[identifier]["version"]) + 1)
        if identifier not in self.items:
            raise CloudHTTPError(404)
        return copy.deepcopy(self.items[identifier])


class CloudWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = StateStore(Path(self.temporary.name).resolve() / "state")
        self.http = FakeHTTP()
        self.factory = patch.object(cloud, "CloudHTTP", return_value=self.http).start()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"CLEANMAC_GOOGLE_DRIVE_TOKEN": "secret-token-fixture"}).start()

    def run_tool(self, action, **options):
        options.setdefault("configuration", {"account_id": "account1"})
        return cloud.dispatch("google-drive", action, options, store=self.store)

    def scan(self, **config):
        return self.run_tool("inspect", roots=["root"], configuration=dict(account_id="account1", **config))

    def plan(self):
        scan = self.scan()
        return self.run_tool("plan", record_id=scan["id"], selected=[scan["items"][0]["id"]])

    def test_missing_credentials_are_unscanned_not_empty(self):
        with patch.dict(os.environ, {}, clear=True):
            result = self.scan()
        self.assertEqual(result["status"], "dependency-needed")
        self.assertEqual(result["coverage"], "unscanned")
        self.assertEqual(self.http.calls, [])

    def test_no_roots_only_identifies_account(self):
        result = self.run_tool("inspect", configuration={})
        self.assertEqual(result["account"]["id"], "account1")
        self.assertEqual(result["status"], "scope-needed")
        self.assertEqual(len(self.http.calls), 1)

    def test_account_mismatch_prevents_list(self):
        result = self.run_tool("inspect", roots=["root"], configuration={"account_id": "another"})
        self.assertEqual(result["error"]["code"], "cloud_account_changed")
        self.assertEqual(len(self.http.calls), 1)

    def test_expired_second_page_is_partial(self):
        self.http.next_cursor = "next"
        self.http.page_failure = CloudHTTPError(401)
        result = self.scan()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["errors"][0]["code"], "cloud_auth_required")

    def test_cursor_cycle_is_partial_and_bounded(self):
        self.http.next_cursor = "same"
        result = self.scan()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["pages"], 2)

    def test_item_limit_is_partial(self):
        self.http.items["file2"] = google_file(id="file2")
        result = self.scan(max_items=1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["items"]), 1)

    def test_shared_shortcut_folder_and_missing_ownership_are_not_eligible(self):
        for changes in ({"shared": True}, {"ownedByMe": False}, {"mimeType": "application/vnd.google-apps.folder"},
                        {"shortcutDetails": {"targetId": "target"}}, {"contentRestrictions": [{"readOnly": True}]}):
            with self.subTest(changes=changes):
                self.http.items["file1"] = google_file(**changes)
                scan = self.scan()
                self.assertEqual(scan["items"][0]["eligible_actions"], [])
                with self.assertRaises(CleanMacError):
                    self.run_tool("plan", record_id=scan["id"], selected=[scan["items"][0]["id"]])

    def test_malformed_metadata_is_unscanned_not_actionable(self):
        for changes in ({"shared": None}, {"capabilities": None}, {"capabilities": []}, {"size": -1}, {"version": {}}, {"parents": {}}):
            with self.subTest(changes=changes):
                self.http.items["file1"] = google_file(**changes)
                scan = self.scan()
                self.assertEqual(scan["status"], "unavailable")
                self.assertEqual(scan["items"], [])
                self.assertEqual(scan["errors"][0]["code"], "cloud_invalid_response")

    def test_token_not_saved_or_returned(self):
        plan = self.plan()
        receipt = self.run_tool("apply", record_id=plan["id"], approve=plan["digest"])
        records = self.store.list("scans") + self.store.list("plans") + self.store.list("receipts")
        self.assertNotIn("secret-token-fixture", json.dumps(records))
        self.assertEqual(receipt["status"], "completed")

    def test_wrong_approval_has_no_network_or_mutation(self):
        plan = self.plan()
        self.http.calls.clear()
        with self.assertRaises(CleanMacError):
            self.run_tool("apply", record_id=plan["id"], approve="wrong")
        self.assertEqual(self.http.calls, [])

    def test_changed_version_refuses_mutation(self):
        plan = self.plan()
        self.http.items["file1"]["version"] = "42"
        with self.assertRaises(CleanMacError):
            self.run_tool("apply", record_id=plan["id"], approve=plan["digest"])
        self.assertFalse(any(call[0] == "PATCH" for call in self.http.calls))

    def test_changed_credentials_require_new_plan(self):
        plan = self.plan()
        with patch.dict(os.environ, {"CLEANMAC_GOOGLE_DRIVE_TOKEN": "new-scopes-token"}):
            with self.assertRaises(CleanMacError):
                self.run_tool("apply", record_id=plan["id"], approve=plan["digest"])
        self.assertFalse(any(call[0] == "PATCH" for call in self.http.calls))

    def test_changed_account_require_new_plan(self):
        plan = self.plan()
        self.http.account_id = "other-account"
        with self.assertRaises(CleanMacError):
            self.run_tool("apply", record_id=plan["id"], approve=plan["digest"])
        self.assertFalse(any(call[0] == "PATCH" for call in self.http.calls))

    def test_successful_trash_replay_and_restore(self):
        plan = self.plan()
        receipt = self.run_tool("apply", record_id=plan["id"], approve=plan["digest"])
        self.assertTrue(self.http.items["file1"]["trashed"])
        self.http.calls.clear()
        with patch.dict(os.environ, {}, clear=True):
            replay = self.run_tool("apply", record_id=plan["id"], approve=plan["digest"])
        self.assertEqual(replay["id"], receipt["id"])
        self.assertEqual(self.http.calls, [])
        restore = self.run_tool("restore-plan", record_id=receipt["id"])
        self.run_tool("apply", record_id=restore["id"], approve=restore["digest"])
        self.assertFalse(self.http.items["file1"]["trashed"])

    def test_timeout_receipt_prevents_automatic_repeat(self):
        plan = self.plan()
        self.http.patch_failure = CleanMacError("timed out", "cloud_network_error")
        receipt = self.run_tool("apply", record_id=plan["id"], approve=plan["digest"])
        self.assertEqual(receipt["status"], "recovery_needed")
        mutation_count = sum(call[0] == "PATCH" for call in self.http.calls)
        replay = self.run_tool("apply", record_id=plan["id"], approve=plan["digest"])
        self.assertEqual(replay["status"], "recovery_needed")
        self.assertEqual(sum(call[0] == "PATCH" for call in self.http.calls), mutation_count)

    def test_local_eviction_never_reaches_cloud_api(self):
        result = self.run_tool("evict")
        self.assertEqual(result["action"], "local-evict")
        self.assertEqual(self.http.calls, [])

    def test_raw_token_config_rejected(self):
        with self.assertRaises(CleanMacError):
            self.run_tool("inspect", configuration={"token": "do-not-store"})
        self.assertEqual(self.http.calls, [])

    def test_cancelled_scan_is_explicit(self):
        result = cloud.dispatch("google-drive", "inspect", {"roots": ["root"], "configuration": {"account_id": "account1"}},
                                store=self.store, cancel=lambda: True)
        self.assertEqual(result["status"], "cancelled")

    def test_cloud_catalog_is_read_only_and_no_probe(self):
        result = cloud.dispatch("cloud-accounts", "inspect", {}, store=self.store)
        self.assertFalse(result["accounts_probed"])
        self.assertEqual(self.http.calls, [])

    def test_managed_refresh_preserves_approval_but_scope_upgrade_does_not(self):
        metadata = {"id": "connection1", "provider": "google-drive", "account_id": "account1", "grant_id": "grant1", "scopes": ["drive.file"]}
        tokens = iter(("original-access", "refreshed-access", "refreshed-access"))
        module = types.ModuleType("cleanmac_core.oauth")
        module.connection_metadata = lambda *args, **kwargs: copy.deepcopy(metadata)
        module.access_token = lambda *args, **kwargs: next(tokens)
        cfg = {"oauth_connection": "connection1", "account_id": "account1"}
        with patch.dict(sys.modules, {"cleanmac_core.oauth": module}), patch("cleanmac_core.oauth", module, create=True):
            scan = self.run_tool("inspect", roots=["root"], configuration=cfg)
            plan = self.run_tool("plan", record_id=scan["id"], selected=[scan["items"][0]["id"]], configuration=cfg)
            result = self.run_tool("apply", record_id=plan["id"], approve=plan["digest"], configuration=cfg)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(plan["payload"]["context"]["credential_reference"], {"oauth_connection": "connection1"})
        self.assertNotIn("refreshed-access", json.dumps(result))

    def test_managed_grant_upgrade_rejects_prior_inventory(self):
        metadata = {"provider": "google-drive", "account_id": "account1", "grant_id": "grant1", "scopes": ["drive.file"]}
        module = types.ModuleType("cleanmac_core.oauth")
        module.connection_metadata = lambda *args, **kwargs: copy.deepcopy(metadata)
        module.access_token = lambda *args, **kwargs: "managed-access-token"
        cfg = {"oauth_connection": "connection1", "account_id": "account1"}
        with patch.dict(sys.modules, {"cleanmac_core.oauth": module}), patch("cleanmac_core.oauth", module, create=True):
            scan = self.run_tool("inspect", roots=["root"], configuration=cfg)
            metadata["scopes"].append("drive")
            with self.assertRaises(CleanMacError) as error:
                self.run_tool("plan", record_id=scan["id"], selected=[scan["items"][0]["id"]], configuration=cfg)
        self.assertEqual(error.exception.code, "cloud_account_changed")


class ProviderContracts(unittest.TestCase):
    def test_onedrive_uses_if_match_and_refuses_work_shared_or_locked_items(self):
        http = FakeHTTP()
        provider = cloud.OneDrive(http, {})
        provider.drive = {"id": "drive1"}
        account = {"id": "user1", "owner_id": "user1", "drive_id": "drive1", "drive_type": "personal"}
        raw = {"id": "file1", "name": "x", "file": {}, "eTag": '"version"', "parentReference": {"id": "root"}}
        for changed in ({"shared": {}}, {"remoteItem": {}}, {"publication": {"level": "checkout"}}):
            self.assertEqual(provider.normalize(dict(raw, **changed), account)["eligible_actions"], [])
        self.assertEqual(provider.normalize(raw, dict(account, drive_type="business"))["eligible_actions"], [])
        item = provider.normalize(raw, account)
        calls = []
        def request(method, url, body=None, headers=None):
            calls.append((method, headers))
            if method == "GET":
                raise CloudHTTPError(404)
            return {}
        http.request = request
        result = provider.mutate(item, "cloud-trash", account)
        self.assertTrue(result["verified"])
        self.assertEqual(calls[0], ("DELETE", {"If-Match": '"version"'}))

    def test_post_delete_permission_error_is_not_no_effect(self):
        from cleanmac_core.actions import ActionNotStarted
        http = FakeHTTP()
        provider = cloud.OneDrive(http, {})
        provider.drive = {"id": "drive1"}
        def request(method, url, body=None, headers=None):
            if method == "GET":
                raise CloudHTTPError(403)
            return {}
        http.request = request
        with self.assertRaises(CloudHTTPError) as error:
            provider.mutate({"provider_id": "file1", "version": "v1"}, "cloud-trash", {"drive_id": "drive1"})
        self.assertNotIsInstance(error.exception, ActionNotStarted)

    def test_onedrive_pagination_cannot_switch_host_drive_folder_or_download(self):
        provider = cloud.OneDrive(FakeHTTP(), {})
        provider.drive = {"id": "drive1"}
        for link in ("https://evil.example/v1.0/drives/drive1/root/children", "https://graph.microsoft.com/v1.0/drives/drive2/root/children",
                     "https://graph.microsoft.com/v1.0/drives/drive1/items/other/children", "https://graph.microsoft.com/v1.0/drives/drive1/root/content"):
            with self.subTest(link=link), self.assertRaises(CleanMacError):
                provider.page("root", link)

    def test_dropbox_delete_has_revision_precondition_and_stable_id(self):
        http = FakeHTTP()
        provider = cloud.Dropbox(http, {})
        raw = {"id": "id:xyz", "name": "x", ".tag": "file", "rev": "123456789", "path_lower": "/folder/x", "has_explicit_shared_members": False}
        account = {"id": "dbid:x", "team": False, "namespace_owned": True}
        item = provider.normalize(raw, account)
        calls = []
        def request(method, url, body=None, headers=None):
            calls.append((url, body))
            return {"metadata": raw}
        http.request = request
        result = provider.mutate(item, "cloud-trash", account)
        self.assertTrue(result["verified"])
        self.assertEqual(calls[0][1], {"path": "id:xyz", "parent_rev": "123456789"})
        self.assertEqual(result["restore_handle"]["revision"], "123456789")

    def test_dropbox_shared_locked_team_and_unknown_sharing_report_only(self):
        provider = cloud.Dropbox(FakeHTTP(), {})
        raw = {"id": "id:x", "name": "x", ".tag": "file", "rev": "123456789", "has_explicit_shared_members": False}
        for changed in ({"sharing_info": {"read_only": True}}, {"file_lock_info": {"is_lockholder": True}}):
            self.assertEqual(provider.normalize(dict(raw, **changed), {"team": False})["eligible_actions"], [])
        with self.assertRaises(CleanMacError):
            provider.normalize(dict(raw, has_explicit_shared_members=None), {"team": False})
        self.assertEqual(provider.normalize(raw, {"team": True})["eligible_actions"], [])

    def test_dropbox_cursor_continuation_does_not_relist_root(self):
        http = FakeHTTP()
        provider = cloud.Dropbox(http, {})
        calls = []
        def request(method, url, body=None, headers=None):
            calls.append((url, body))
            return {"entries": [], "has_more": False, "cursor": "done"}
        http.request = request
        rows, cursor, incomplete = provider.page("/folder", "saved-cursor")
        self.assertTrue(calls[0][0].endswith("/files/list_folder/continue"))
        self.assertEqual(calls[0][1], {"cursor": "saved-cursor"})
        self.assertIsNone(cursor)


class TransportTests(unittest.TestCase):
    def test_only_exact_https_provider_endpoints(self):
        for url in ("http://www.googleapis.com/drive/v3/files", "https://www.googleapis.com.evil/drive/v3/files",
                    "https://user@www.googleapis.com/drive/v3/files", "https://www.googleapis.com/other", "https://www.googleapis.com:8443/drive/v3/files"):
            with self.subTest(url=url), self.assertRaises(CleanMacError):
                validate_url("google-drive", url)

    def test_redirect_never_forwards_bearer(self):
        with self.assertRaises(CleanMacError):
            _NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://evil.example")

    def test_provider_error_body_is_not_exposed(self):
        transport = CloudHTTP("google-drive", "a-secret")
        error = urllib.error.HTTPError("https://www.googleapis.com/drive/v3/files", 403, "secret error text", {}, io.BytesIO(b"a-secret"))
        with patch.object(transport._opener, "open", side_effect=error):
            with self.assertRaises(CloudHTTPError) as raised:
                transport.request("GET", "https://www.googleapis.com/drive/v3/files")
        self.assertNotIn("a-secret", json.dumps(raised.exception.as_dict()))
        self.assertNotIn("secret error text", str(raised.exception))

    def test_icloud_without_scope_does_not_invoke_helper(self):
        with patch.object(cloud, "_cloud_helper") as helper:
            result = cloud.dispatch("icloud", "inspect", {"roots": []}, store=None)
        self.assertEqual(result["status"], "scope-needed")
        helper.assert_not_called()


_NATIVE = Path(__file__).resolve().parents[1] / "native/bin/cleanmac-cloud"


@unittest.skipUnless(sys.platform == "darwin" and _NATIVE.is_file(), "Optional iCloud helper must be explicitly built.")
class NativeCloudFixtures(unittest.TestCase):
    def invoke(self, command, request):
        result = subprocess.run([str(_NATIVE), command], input=json.dumps(request), text=True, capture_output=True, timeout=5)
        return result.returncode, json.loads(result.stdout)

    def test_info_does_not_probe_account(self):
        code, result = self.invoke("info", {})
        self.assertEqual(code, 0)
        self.assertFalse(result["privileged"])
        self.assertIn("local-evict", result["capabilities"])

    def test_symlink_root_rejected_before_account_or_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            data = root / "real"
            data.mkdir()
            original = data / "keep.txt"
            original.write_bytes(b"keep fixture")
            link = root / "link"
            link.symlink_to(data, target_is_directory=True)
            code, result = self.invoke("inspect", {"root": str(link)})
            self.assertEqual(code, 1)
            self.assertIn(result["error"]["code"], {"cloud_path_unavailable", "cloud_invalid_scope"})
            self.assertEqual(original.read_bytes(), b"keep fixture")

    def test_relative_scope_rejected(self):
        code, result = self.invoke("inspect", {"root": "relative"})
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "cloud_invalid_scope")

    def test_unknown_command_has_no_account_or_filesystem_operation(self):
        code, result = self.invoke("permanent-delete", {})
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "cloud_invalid_command")

    def test_input_bound_rejects_large_payload(self):
        code, result = self.invoke("inspect", {"root": "x" * 70000})
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "cloud_invalid_request")


if __name__ == "__main__":
    unittest.main()

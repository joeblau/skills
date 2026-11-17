"""Independent cloud forward tests through CLI JSON and the HTTPS transport.

The opener is replaced with an in-memory provider. No account, socket, browser,
credential store, or live file content is accessed.
"""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cleanmac_core import cli, cloud_tools
from cleanmac_core.cloud_http import CloudHTTP, validate_url
from cleanmac_core.common import CleanMacError


class FakeHTTPS:
    def __init__(self):
        self.requests = []
        self.account = "fixture-account"
        self.file = {"id": "fixture-file", "name": "accounting notes.txt", "size": "20", "version": "1", "mimeType": "text/plain",
                     "parents": ["fixture-folder"], "ownedByMe": True, "shared": False, "trashed": False,
                     "capabilities": {"canTrash": True, "canUntrash": True}}
        self.fail_after_mutation = False
        self.next_link = None

    def open(self, request, timeout=None):
        method, url = request.get_method(), request.full_url
        self.requests.append((method, url, request.data, dict(request.headers)))
        parsed = urlsplit(url)
        if parsed.hostname == "graph.microsoft.com":
            if parsed.path == "/v1.0/me":
                value = {"id": self.account}
            elif parsed.path == "/v1.0/me/drive":
                value = {"id": "fixture-drive", "driveType": "personal", "owner": {"user": {"id": self.account}}}
            elif parsed.path == "/v1.0/drives/fixture-drive/root/children":
                value = {"value": [{"id": "file1", "name": "file1", "size": 10, "file": {}, "eTag": '"version1"', "parentReference": {"id": "root"}}],
                         "@odata.nextLink": self.next_link}
            else:
                raise AssertionError("Unexpected Graph request " + url)
        elif parsed.hostname == "www.googleapis.com":
            if parsed.path.endswith("/about"):
                value = {"user": {"permissionId": self.account}}
            elif parsed.path == "/drive/v3/files":
                self.assertion_query = parse_qs(parsed.query)["q"][0]
                value = {"files": [copy.deepcopy(self.file)]}
            elif parsed.path == "/drive/v3/files/fixture-file":
                if method == "PATCH":
                    self.file["trashed"] = json.loads(request.data)["trashed"]
                    self.file["version"] = str(int(self.file["version"]) + 1)
                    if self.fail_after_mutation:
                        raise urllib.error.URLError("provider timeout after mutation; secret-fixture-token")
                value = copy.deepcopy(self.file)
            else:
                raise AssertionError("Unexpected Drive request " + url)
        else:
            raise AssertionError("Cross-provider request reached transport: " + url)
        return io.BytesIO(json.dumps(value).encode())


class CloudCLIForwardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name).resolve() / "state"
        self.http = FakeHTTPS()
        self.addCleanup(patch.stopall)
        patch("cleanmac_core.cloud_http.urllib.request.build_opener", return_value=self.http).start()
        patch.dict(os.environ, {"CLEANMAC_GOOGLE_DRIVE_TOKEN": "secret-fixture-token", "CLEANMAC_ONEDRIVE_TOKEN": "secret-graph-token"}).start()
        patch.object(cli.sys, "platform", "darwin").start()

    def command(self, *arguments, expected_code=0):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = cli.main(["--json", "--data-dir", str(self.state), *arguments])
        self.assertEqual(code, expected_code, stream.getvalue())
        result = json.loads(stream.getvalue())
        self.assertNotIn("secret-fixture-token", stream.getvalue())
        self.assertNotIn("secret-graph-token", stream.getvalue())
        return result

    def scan(self):
        return self.command("tool", "google-drive", "--root", "fixture-folder", "--config", '{"account_id":"fixture-account"}')

    def plan(self, scan):
        return self.command("tool", "google-drive", "--action", "plan", "--record", scan["id"], "--select", scan["items"][0]["id"])

    def apply(self, plan, expected_code=0):
        return self.command("tool", "google-drive", "--action", "apply", "--record", plan["id"], "--approve", plan["digest"], expected_code=expected_code)

    def test_real_cli_scan_review_plan_trash_restore_uses_opaque_remote_scope(self):
        scan = self.scan()
        self.assertEqual(scan["roots"], ["fixture-folder"])
        self.assertEqual(self.http.assertion_query, "'fixture-folder' in parents")
        plan = self.plan(scan)
        receipt = self.apply(plan)
        self.assertEqual(receipt["status"], "completed")
        self.assertTrue(self.http.file["trashed"])
        restored = self.command("tool", "google-drive", "--action", "restore-plan", "--record", receipt["id"])
        result = self.apply(restored)
        self.assertEqual(result["status"], "completed")
        self.assertFalse(self.http.file["trashed"])
        self.assertEqual(sum(method == "PATCH" for method, _, _, _ in self.http.requests), 2)
        for path in self.state.rglob("*.json"):
            self.assertNotIn("secret-fixture-token", path.read_text())

    def test_real_cli_account_change_rejects_before_any_mutation(self):
        plan = self.plan(self.scan())
        self.http.account = "different-account"
        result = self.apply(plan, expected_code=2)
        self.assertEqual(result["error"]["code"], "cloud_account_changed")
        self.assertFalse(any(method == "PATCH" for method, _, _, _ in self.http.requests))

    def test_real_cli_move_outside_selected_folder_invalidates_plan(self):
        plan = self.plan(self.scan())
        self.http.file["parents"] = ["unselected-folder"]
        result = self.apply(plan, expected_code=2)
        self.assertEqual(result["error"]["code"], "cloud_item_changed")
        self.assertFalse(any(method == "PATCH" for method, _, _, _ in self.http.requests))

    def test_ambiguous_http_timeout_preserves_receipt_and_never_repeats(self):
        plan = self.plan(self.scan())
        self.http.fail_after_mutation = True
        receipt = self.apply(plan)
        self.assertEqual(receipt["status"], "recovery_needed")
        self.assertTrue(self.http.file["trashed"])
        self.apply(plan)
        self.assertEqual(sum(method == "PATCH" for method, _, _, _ in self.http.requests), 1)

    def test_saved_receipt_cannot_be_retargeted_for_restore(self):
        receipt = self.apply(self.plan(self.scan()))
        path = self.state / "receipts" / (receipt["id"] + ".json")
        changed = json.loads(path.read_text())
        changed["result"]["item"]["provider_id"] = "unselected-file"
        path.write_text(json.dumps(changed))
        result = self.command("tool", "google-drive", "--action", "restore-plan", "--record", receipt["id"], expected_code=2)
        self.assertIn(result["error"]["code"], ("cloud_invalid_record", "invalid_action_receipt"))
        self.assertEqual(sum(method == "PATCH" for method, _, _, _ in self.http.requests), 1)

    def test_graph_malicious_nextlink_never_leaves_selected_drive_path(self):
        self.http.next_link = "https://graph.microsoft.com/v1.0/drives/other-drive/root/children"
        report = self.command("tool", "onedrive", "--root", "root", "--config", '{"account_id":"fixture-account"}')
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["errors"][0]["code"], "cloud_invalid_endpoint")
        self.assertFalse(any("other-drive" in url for _, url, _, _ in self.http.requests))

    def test_unknown_nested_metadata_stays_structured_and_unactionable(self):
        self.http.file["capabilities"] = None
        result = self.scan()
        self.assertIn(result["status"], ("partial", "unavailable"))
        self.assertEqual(result["counts"]["eligible"], 0)

    def test_wrong_digest_never_reads_account_or_sends_request(self):
        plan = self.plan(self.scan())
        self.http.requests.clear()
        result = self.command("tool", "google-drive", "--action", "apply", "--record", plan["id"], "--approve", "invalid", expected_code=2)
        self.assertIn(result["error"]["code"], ("approval_required", "approval_mismatch"))
        self.assertEqual(self.http.requests, [])

    def test_provider_eviction_remains_separate_from_remote_delete(self):
        result = self.command("tool", "google-drive", "--action", "evict")
        self.assertEqual(result["action"], "local-evict")
        self.assertEqual(self.http.requests, [])

    def test_exact_host_and_api_prefix_prevent_credential_exfiltration(self):
        for url in ("https://www.googleapis.com.evil.test/drive/v3/files", "https://www.googleapis.com@evil.test/drive/v3/files",
                    "https://www.googleapis.com/drive/v3/files#https://evil.test", "https://www.googleapis.com:444/drive/v3/files",
                    "http://www.googleapis.com/drive/v3/files", "https://graph.microsoft.com/drive/v3/files"):
            with self.subTest(url=url), self.assertRaises(CleanMacError):
                CloudHTTP("google-drive", "secret-fixture-token").request("GET", url)
        self.assertEqual(self.http.requests, [])


if __name__ == "__main__":
    unittest.main()

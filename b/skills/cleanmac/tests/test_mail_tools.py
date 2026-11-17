"""Fixture-only Gmail transport, exact mail mutations, and preservation tests."""
from __future__ import annotations

import copy
import datetime as dt
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from urllib import error, parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cleanmac_core import mail_http, mail_tools
from cleanmac_core.common import CleanMacError, digest
from cleanmac_core.state import StateStore


ACCOUNT = "fixture@example.com"
CONFIG = {"account": ACCOUNT, "scopes": [mail_http.MODIFY_SCOPE]}


def message(identifier="a1", labels=None, *, date="2025-01-01T00:00:00+00:00", headers=None):
    values = [{"name": "From", "value": "Example <sender@example.net>"},
              {"name": "Subject", "value": "Fixture subject"}]
    values.extend(headers or [])
    return {"id": identifier, "threadId": "same-thread", "historyId": "10",
            "internalDate": str(int(dt.datetime.fromisoformat(date).timestamp() * 1000)),
            "labelIds": list(labels if labels is not None else ["INBOX"]), "sizeEstimate": 1234,
            "payload": {"headers": values, "body": {"data": "BODY_MUST_NOT_PERSIST"}},
            "snippet": "BODY_SNIPPET_MUST_NOT_PERSIST"}


class FakeGmail:
    def __init__(self, messages=None, configuration=None):
        self.reference = mail_http.credential_reference(configuration or CONFIG)
        self.account = self.reference["account"]
        self.messages = {item["id"]: copy.deepcopy(item) for item in (messages or [message()])}
        self.changes = []
        self.reads = []
        self.pages = []
        self.failure = None
        self.metadata_hook = None
        self.change_hook = None

    def check_account(self):
        return {"account": self.account, "messages_total_estimate": len(self.messages), "history_id": "100"}

    def list_messages(self, **kwargs):
        self.pages.append(kwargs)
        values = list(self.messages.values())
        if kwargs.get("labels"):
            values = [item for item in values if set(kwargs["labels"]) <= set(item["labelIds"])]
        offset = int(kwargs.get("page_token") or 0)
        ids = [{"id": item["id"]} for item in values[offset:offset + kwargs["limit"]]]
        result = {"messages": ids}
        if offset + kwargs["limit"] < len(values):
            result["nextPageToken"] = str(offset + kwargs["limit"])
        return result

    def metadata(self, identifier):
        self.reads.append(identifier)
        if self.metadata_hook:
            self.metadata_hook(self, identifier)
        if self.failure:
            raise self.failure
        if identifier not in self.messages:
            raise CleanMacError("Gone", code="mail_message_missing")
        return copy.deepcopy(self.messages[identifier])

    def change(self, identifier, operation):
        self.changes.append((identifier, operation))
        if self.change_hook:
            self.change_hook(self, identifier, operation)
        labels = set(self.messages[identifier]["labelIds"])
        if operation == "archive":
            labels.discard("INBOX")
        elif operation == "restore-archive":
            labels.add("INBOX")
        elif operation == "trash":
            labels.discard("INBOX")
            labels.add("TRASH")
        elif operation == "restore-trash":
            labels.discard("TRASH")
        self.messages[identifier]["labelIds"] = sorted(labels)
        self.messages[identifier]["historyId"] = str(int(self.messages[identifier]["historyId"]) + 1)
        return {"id": identifier, "labelIds": sorted(labels)}


class MailWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cleanmac-mail-")
        self.base = Path(self.temp.name).resolve()
        self.store = StateStore(self.base / "state")
        self.client = FakeGmail()
        self.patch = mock.patch.object(mail_tools, "_client", side_effect=lambda config, **kwargs: self.client)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def run_tool(self, action, **options):
        options.setdefault("configuration", copy.deepcopy(CONFIG))
        return mail_tools.dispatch("gmail", action, options, store=self.store)

    def plan(self, operation="archive", *, scan=None, selected=None):
        scan = scan or self.run_tool("inspect")
        selected = selected or [scan["items"][0]["id"]]
        return self.run_tool("plan", record_id=scan["id"], selected=selected,
                             configuration=dict(CONFIG, operation=operation))

    def apply(self, plan):
        return self.run_tool("apply", record_id=plan["id"], approve=plan["digest"])

    def test_metadata_inventory_never_retains_bodies_or_token_values(self):
        result = self.run_tool("inspect")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["counts"]["messages_inspected"], 1)
        encoded = json.dumps(result)
        self.assertNotIn("BODY_", encoded)
        self.assertFalse(result["bodies_fetched"])
        self.assertFalse(result["query_sent"])
        self.assertEqual(self.client.changes, [])

    def test_no_account_is_actionable_unscanned_dependency(self):
        result = self.run_tool("inspect", configuration={})
        self.assertEqual(result["status"], "dependency-needed")
        self.assertEqual(result["coverage"], "unscanned")
        self.assertEqual(self.client.reads, [])

    def test_expired_token_keeps_prior_message_metadata(self):
        self.client = FakeGmail([message("a1"), message("b2")])
        def expiry(client, identifier):
            if identifier == "b2":
                raise CleanMacError("Expired", code="mail_auth_expired")
        self.client.metadata_hook = expiry
        result = self.run_tool("inspect")
        self.assertEqual(result["status"], "partial")
        self.assertEqual([item["message_id"] for item in result["items"]], ["a1"])
        self.assertEqual(self.store.get("scans", result["id"])["errors"][0]["code"], "mail_auth_expired")

    def test_pagination_bound_reports_partial_not_empty_or_complete(self):
        self.client = FakeGmail([message("a1"), message("b2")])
        result = self.run_tool("inspect", configuration=dict(CONFIG, max_messages=1))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["items"]), 1)

    def test_cancel_keeps_completed_metadata(self):
        self.client = FakeGmail([message("a1"), message("b2")])
        result = mail_tools.dispatch("gmail", "inspect", {"configuration": CONFIG}, store=self.store,
                                     cancel=lambda: len(self.client.reads) >= 1)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(len(result["items"]), 1)

    def test_spam_scope_uses_provider_label_and_local_sender_filter(self):
        self.client = FakeGmail([message("a1", ["SPAM"]), message("b2")])
        result = self.run_tool("inspect", configuration=dict(CONFIG, category="spam", sender="sender@example.net"))
        self.assertEqual(self.client.pages[0]["labels"], ["SPAM"])
        self.assertEqual(result["counts"]["matching_review"], 1)

    def test_protected_messages_cannot_be_selected(self):
        for label in ("DRAFT", "STARRED", "IMPORTANT", "TRASH"):
            with self.subTest(label=label):
                self.client = FakeGmail([message(labels=["INBOX", label])])
                with self.assertRaises(CleanMacError) as ctx:
                    self.plan("trash")
                self.assertEqual(ctx.exception.code, "protected_mail")
                self.assertFalse(self.client.changes)

    def test_draft_delete_and_unsubscribe_are_not_silent_trash_aliases(self):
        for action in ("delete-draft", "unsubscribe"):
            with self.assertRaises(CleanMacError) as ctx:
                self.plan(action)
            self.assertEqual(ctx.exception.code, "unsupported_mail_action")

    def test_exact_message_archive_keeps_other_message_in_thread(self):
        self.client = FakeGmail([message("a1"), message("b2")])
        result = self.apply(self.plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.client.changes, [("a1", "archive")])
        self.assertIn("INBOX", self.client.messages["b2"]["labelIds"])
        journal = self.store.get("receipts", result["result"]["journal_id"])
        self.assertEqual(journal["operations"][0]["status"], "completed")
        self.assertEqual(result["checkpoint"]["journal_digest"], digest(journal))

    def test_wrong_approval_sends_no_mutation(self):
        plan = self.plan()
        with self.assertRaises(CleanMacError):
            self.run_tool("apply", record_id=plan["id"], approve="0" * 64)
        self.assertEqual(self.client.changes, [])

    def test_rescued_spam_is_not_trashed_from_stale_plan(self):
        self.client = FakeGmail([message(labels=["SPAM"])])
        plan = self.plan("trash")
        self.client.messages["a1"]["labelIds"] = ["INBOX"]
        self.client.messages["a1"]["historyId"] = "11"
        with self.assertRaises(CleanMacError) as ctx:
            self.apply(plan)
        self.assertEqual(ctx.exception.code, "mail_state_changed")
        self.assertEqual(self.client.changes, [])

    def test_second_recheck_skips_change_between_preflight_and_execution(self):
        plan = self.plan("trash")
        self.client.reads.clear()
        def concurrent(client, identifier):
            if len(client.reads) == 2:
                client.messages[identifier]["labelIds"].append("IMPORTANT")
                client.messages[identifier]["historyId"] = "11"
        self.client.metadata_hook = concurrent
        result = self.apply(plan)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["outcome"], "partial")
        self.assertEqual(result["result"]["skipped"], 1)
        self.assertEqual(self.client.changes, [])

    def test_completed_plan_replay_never_mutates_again(self):
        plan = self.plan()
        first = self.apply(plan)
        reads = len(self.client.reads)
        second = self.apply(plan)
        self.assertEqual(first, second)
        self.assertEqual(len(self.client.changes), 1)
        self.assertEqual(len(self.client.reads), reads)

    def test_completed_plan_replay_needs_no_token_or_keychain_access(self):
        plan = self.plan()
        first = self.apply(plan)
        with mock.patch.object(mail_tools, "_client", side_effect=AssertionError("credential access on replay")):
            self.assertEqual(first, self.apply(plan))

    def test_timeout_keeps_ambiguous_journal_and_does_not_retry(self):
        plan = self.plan("trash")
        def fail(client, identifier, operation):
            raise CleanMacError("Network timed out", code="mail_network_error")
        self.client.change_hook = fail
        result = self.apply(plan)
        self.assertEqual(result["status"], "recovery_needed")
        journal = self.store.get("receipts", result["checkpoint"]["journal_id"])
        self.assertEqual(journal["operations"][0]["status"], "running")
        self.apply(plan)
        self.assertEqual(len(self.client.changes), 1)
        with self.assertRaises(CleanMacError):
            self.run_tool("restore", record_id=journal["id"], selected=[journal["operations"][0]["item_id"]])

    def test_partial_batch_journals_each_message_and_restores_only_verified_items(self):
        self.client = FakeGmail([message("a1"), message("b2"), message("c3")])
        scan = self.run_tool("inspect")
        plan = self.plan("trash", scan=scan, selected=[item["id"] for item in scan["items"]])
        def stop(client, identifier, operation):
            if identifier == "b2":
                raise CleanMacError("Timeout", code="mail_network_error")
        self.client.change_hook = stop
        result = self.apply(plan)
        self.assertEqual(result["status"], "recovery_needed")
        journal = self.store.get("receipts", result["checkpoint"]["journal_id"])
        self.assertEqual([entry["status"] for entry in journal["operations"]], ["completed", "running", "not_started"])
        restore = self.run_tool("restore", record_id=journal["id"], selected=[journal["operations"][0]["item_id"]])
        self.client.change_hook = None
        self.assertEqual(self.apply(restore)["status"], "completed")
        self.assertNotIn(("c3", "trash"), self.client.changes)

    def test_restore_archive_is_new_plan_and_preserves_unrelated_labels(self):
        self.client = FakeGmail([message(labels=["INBOX", "Label_1"])])
        result = self.apply(self.plan())
        journal_id = result["result"]["journal_id"]
        journal = self.store.get("receipts", journal_id)
        restore = self.run_tool("restore", record_id=journal_id, selected=[journal["operations"][0]["item_id"]])
        self.assertEqual(len(self.client.changes), 1)
        outcome = self.apply(restore)
        self.assertEqual(outcome["status"], "completed")
        self.assertEqual(self.client.messages["a1"]["labelIds"], ["INBOX", "Label_1"])

    def test_restore_conflict_preserves_newer_user_label_state(self):
        result = self.apply(self.plan())
        journal = self.store.get("receipts", result["result"]["journal_id"])
        restore = self.run_tool("restore", record_id=journal["id"], selected=[journal["operations"][0]["item_id"]])
        self.client.messages["a1"]["labelIds"] = ["Label_new"]
        self.client.messages["a1"]["historyId"] = "99"
        with self.assertRaises(CleanMacError) as ctx:
            self.apply(restore)
        self.assertEqual(ctx.exception.code, "mail_state_changed")
        self.assertEqual(len(self.client.changes), 1)

    def test_untrash_does_not_claim_exact_original_inbox_restoration(self):
        result = self.apply(self.plan("trash"))
        journal = self.store.get("receipts", result["result"]["journal_id"])
        restore = self.run_tool("restore", record_id=journal["id"], selected=[journal["operations"][0]["item_id"]])
        restored = self.apply(restore)
        recovery = self.store.get("receipts", restored["result"]["journal_id"])
        self.assertFalse(recovery["operations"][0]["original_labels_restored_exactly"])
        self.assertNotIn("TRASH", self.client.messages["a1"]["labelIds"])

    def test_other_account_same_message_id_cannot_reuse_scan(self):
        scan = self.run_tool("inspect")
        other = dict(CONFIG, account="other@example.com", operation="trash")
        with self.assertRaises(CleanMacError) as ctx:
            self.run_tool("plan", record_id=scan["id"], selected=[scan["items"][0]["id"]], configuration=other)
        self.assertEqual(ctx.exception.code, "mail_account_mismatch")
        self.assertNotEqual(mail_tools.normalize_message(message(), ACCOUNT)["id"],
                            mail_tools.normalize_message(message(), "other@example.com")["id"])

    def test_modified_journal_cannot_expand_restore_scope(self):
        result = self.apply(self.plan())
        journal = self.store.get("receipts", result["result"]["journal_id"])
        journal["operations"][0]["after"]["message_id"] = "b2"
        self.store.put("receipts", journal)
        with self.assertRaises(CleanMacError) as ctx:
            self.run_tool("restore", record_id=journal["id"], selected=[journal["operations"][0]["item_id"]])
        self.assertEqual(ctx.exception.code, "mail_recovery_invalid")

    def test_forget_preserves_recovery_journal(self):
        result = self.apply(self.plan())
        with self.assertRaises(CleanMacError) as ctx:
            self.store.delete("receipts", result["result"]["journal_id"])
        self.assertEqual(ctx.exception.code, "recovery_retention")

    def test_apple_mail_inventory_never_recommends_eviction(self):
        root = self.base / "Mail Downloads"
        root.mkdir()
        (root / "edited.txt").write_text("authored changes")
        result = mail_tools.dispatch("mail-attachments", "inspect", {"roots": [str(root)]}, store=self.store)
        self.assertFalse(result["cleanup_available"])
        self.assertEqual(result["remote_recoverability"], "unverified")
        self.assertTrue(all(not item["eligible"] for item in result["items"]))
        self.assertEqual((root / "edited.txt").read_text(), "authored changes")


class MailRulesTests(unittest.TestCase):
    def test_calendar_month_thresholds_overlap_and_trash_exclusion(self):
        now = dt.datetime(2026, 11, 13, 12, tzinfo=dt.timezone.utc)
        items = [mail_tools.normalize_message(message("a1", ["UNREAD", "CATEGORY_PROMOTIONS"],
                    headers=[{"name": "Auto-Submitted", "value": "auto-generated"}]), ACCOUNT),
                 mail_tools.normalize_message(message("b2", ["TRASH", "UNREAD"]), ACCOUNT),
                 mail_tools.normalize_message(message("c3", ["UNREAD"], date="2026-08-13T12:00:00+00:00"), ACCOUNT)]
        groups = mail_tools.suggestions(items, now=now)
        data = {entry["category"]: entry["item_ids"] for entry in groups["groups"]}
        self.assertEqual(data["unread"], [items[0]["id"]])
        self.assertIn(items[0]["id"], data["automated"])
        self.assertIn(items[0]["id"], data["promotions"])
        # Both surviving messages are archived; repeated suggestion membership is deduplicated.
        self.assertEqual(groups["unique_suggested_count"], 2)

    def test_end_of_month_cutoff_clamps_days(self):
        cutoff = mail_tools._months_before(dt.datetime(2024, 3, 31, tzinfo=dt.timezone.utc), 1)
        self.assertEqual((cutoff.month, cutoff.day), (2, 29))

    def test_unsubscribe_headers_are_data_no_requests_and_no_urls_persist(self):
        result = mail_tools.unsubscribe_metadata({"list-unsubscribe": "<https://127.0.0.1/private>, <https://example.org/unsubscribe?secret=abc>, <mailto:list@example.org>",
                                                   "list-unsubscribe-post": "List-Unsubscribe=One-Click"})
        self.assertTrue(result["unsafe_or_unsupported_target_present"])
        self.assertTrue(result["https_header_present"])
        self.assertFalse(result["automatic_action_available"])
        self.assertNotIn("secret", json.dumps(result))


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"CLEANMAC_GMAIL_ACCESS_TOKEN": "private-token-value"})
        self.env.start()
        self.client = mail_http.GmailClient(CONFIG)

    def tearDown(self):
        self.env.stop()

    def response(self, payload):
        result = mock.MagicMock()
        result.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        return result

    def test_metadata_request_excludes_body_and_attachment_fields(self):
        self.client._opener = mock.Mock()
        self.client._opener.open.return_value = self.response(message())
        self.client.metadata("a1")
        req = self.client._opener.open.call_args.args[0]
        query = parse.parse_qs(parse.urlsplit(req.full_url).query)
        self.assertEqual(query["format"], ["metadata"])
        self.assertNotIn("body", query["fields"][0])
        self.assertNotIn("snippet", query["fields"][0])
        self.assertEqual(req.get_header("Authorization"), "Bearer private-token-value")
        self.assertIsNone(req.data)

    def test_metadata_list_never_uses_search_query(self):
        self.client._opener = mock.Mock()
        self.client._opener.open.return_value = self.response({})
        self.client.list_messages(labels=["SPAM"])
        req = self.client._opener.open.call_args.args[0]
        query = parse.parse_qs(parse.urlsplit(req.full_url).query)
        self.assertNotIn("q", query)
        self.assertEqual(query["labelIds"], ["SPAM"])

    def test_fixed_endpoint_allowlist_rejects_message_send_and_arbitrary_hosts(self):
        for method, path in (("POST", "messages/send"), ("GET", "https://evil.example/"),
                             ("DELETE", "messages/a1"), ("GET", "messages/a1/attachments/x")):
            with self.assertRaises(CleanMacError):
                self.client._request(method, path)

    def test_no_redirect_policy_never_forwards_token(self):
        self.assertIsNone(mail_http._NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example"))

    def test_profile_account_mismatch_blocks_actions(self):
        self.client._opener = mock.Mock()
        self.client._opener.open.return_value = self.response({"emailAddress": "different@example.com"})
        with self.assertRaises(CleanMacError) as ctx:
            self.client.check_account()
        self.assertEqual(ctx.exception.code, "mail_account_mismatch")

    def test_scope_must_be_separately_declared_for_mutation(self):
        client = mail_http.GmailClient({"account": ACCOUNT})
        with self.assertRaises(CleanMacError) as ctx:
            client.change("a1", "trash")
        self.assertEqual(ctx.exception.code, "mail_modify_scope_required")

    def test_error_does_not_leak_provider_body_or_credentials(self):
        self.client._opener = mock.Mock()
        self.client._opener.open.side_effect = error.HTTPError("https://gmail.googleapis.com/", 429, "private-token-value", {"Retry-After": "30"}, io.BytesIO(b"SENSITIVE"))
        with self.assertRaises(CleanMacError) as ctx:
            self.client.metadata("a1")
        self.assertEqual(ctx.exception.code, "mail_rate_limited")
        self.assertEqual(ctx.exception.details["retry_after_seconds"], 30)
        self.assertNotIn("private-token-value", str(ctx.exception))
        self.assertNotIn("SENSITIVE", str(ctx.exception))

    def test_invalid_token_options_cannot_enter_a_plan(self):
        with self.assertRaises(CleanMacError) as ctx:
            mail_http.credential_reference(dict(CONFIG, access_token="secret"))
        self.assertEqual(ctx.exception.code, "mail_credential_reference_required")

    def test_managed_oauth_credential_only_uses_in_memory_resolver(self):
        config = dict(CONFIG, credential={"source": "managed-oauth", "connection_id": "connection-fixture"})
        with mock.patch.object(mail_http, "_read_token", side_effect=AssertionError("legacy credential access")):
            client = mail_http.GmailClient(config, token_provider=lambda: "fixture-refreshed-token")
        self.assertEqual(client._token, "fixture-refreshed-token")
        self.assertNotIn("fixture-refreshed-token", json.dumps(client.reference))

    def test_keychain_query_contains_reference_but_no_token_argument(self):
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(mail_http.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = b"fixture-keychain-token\n"
            client = mail_http.GmailClient(dict(CONFIG, credential={"source": "keychain"}))
            self.assertEqual(client._token, "fixture-keychain-token")
            self.assertNotIn("fixture-keychain-token", repr(run.call_args))
            self.assertIn(ACCOUNT, run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()

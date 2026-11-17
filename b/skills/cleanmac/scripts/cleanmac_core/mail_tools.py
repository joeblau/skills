"""Account-bound Gmail metadata review and individually journaled mail actions.

Provider changes are exact message operations, never thread-wide operations.
Apple Mail attachments remain report-only until remote recoverability and local
edits can be proven through a supported API.
"""
from __future__ import annotations

import calendar
import datetime as dt
from email.utils import parseaddr
import ipaddress
import re
from urllib.parse import urlsplit

from .common import CleanMacError, digest, display_path, new_id, utc_now
from .mail_http import GmailClient, MODIFY_SCOPE, METADATA_SCOPE, credential_reference, message_id

SOURCES = [
    "https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list",
    "https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/get",
    "https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/modify",
    "https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/trash",
    "https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/untrash",
    "https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.drafts/delete",
    "https://developers.google.com/workspace/gmail/api/auth/scopes",
    "https://developers.google.com/identity/protocols/oauth2/native-app",
    "https://www.rfc-editor.org/rfc/rfc8058",
]
TOOL_SPECS = [
    {"name": "gmail", "title": "Gmail metadata review and recoverable mail actions",
     "feature_ids": ["CM-063", "CM-064", "CM-065", "CM-066", "CM-067", "CM-068"],
     "actions": ["inspect", "plan", "apply", "restore"], "status": "conditional",
     "description": "Review metadata and explicit messages; archive/trash requires a separate modify grant. Restore prepares a new approval plan.",
     "requirements": ["Explicit Gmail account or saved OAuth connection", "Your own Google desktop OAuth client, or an external access-token reference", "gmail.metadata for review; gmail.modify for changes"],
     "configuration": {"account": "Required exact account email address.",
                       "oauth_connection": "Saved Gmail connection from the accounts tool; resolves account/scopes and refreshes through Keychain without exposing tokens.",
                       "credential": "{source:environment,name:CLEANMAC_GMAIL_ACCESS_TOKEN} or {source:keychain,service:b-skills.cleanmac.gmail.access-token}; never pass the token itself.",
                       "scopes": "Actual authorized scope URIs; defaults to gmail.metadata. A declared scope does not grant it.",
                       "mode": "inventory (default) or connection (profile-only check).",
                       "category": "all, promotions, automated, unread, archived, newsletters, spam, drafts, or sender.",
                       "sender": "Exact sender email filter, applied locally.",
                       "max_messages": "1–2000; defaults to 200. Partial coverage is retained.",
                       "max_pages": "1–100; defaults to 10.",
                       "operation": "For plan: archive or trash. Draft-delete and unsubscribe are not enabled."},
     "examples": ["tool gmail --action inspect --config '{\"account\":\"you@gmail.com\",\"max_messages\":100}'",
                  "tool gmail --action plan --record SCAN_ID --select ITEM_ID --config '{\"account\":\"you@gmail.com\",\"scopes\":[\"https://www.googleapis.com/auth/gmail.modify\"],\"operation\":\"archive\"}'",
                  "tool gmail --action restore --record JOURNAL_RECEIPT_ID --select ITEM_ID --config '{\"account\":\"you@gmail.com\",\"scopes\":[\"https://www.googleapis.com/auth/gmail.modify\"]}'"]},
    {"name": "mail-attachments", "title": "Apple Mail attachment preservation review",
     "feature_ids": ["CM-025"], "actions": ["inspect"], "status": "report-only",
     "description": "Metadata inventory of explicitly selected folders. No eviction recommendation without verifiable remote recovery and edit provenance.",
     "configuration": {"max_items": "1–100000 metadata entries; defaults to 10000."},
     "requirements": ["Explicit local folder for inventory; no roots returns guidance only"]},
]
_CATEGORIES = {"all", "promotions", "automated", "unread", "archived", "newsletters", "spam", "drafts", "sender"}
_MUTATIONS = {"archive", "trash", "restore-archive", "restore-trash"}
_LIMITATIONS = [
    "Metadata and modification scopes are Google restricted scopes; use your own development OAuth client or complete Google's applicable verification before distributing an OAuth app.",
    "Use a saved Gmail connection from the accounts tool, or an externally obtained access-token reference. A new scope grant requires a new explicit authorization; it is never upgraded automatically.",
    "Gmail modify grants broader access than this adapter uses. This adapter never requests message bodies, attachments, send endpoints, or permanent deletion.",
    "Unsubscribe remains a Gmail/native-client handoff because metadata alone does not verify the DKIM coverage required for a safe RFC 8058 POST.",
    "Draft review is metadata-only. Permanent draft deletion is disabled until validated with a disposable account.",
    "Provider calls do not offer a documented atomic label precondition here. Rechecks narrow the concurrency window; mutations only change the intended labels and never replace all labels.",
]


def _integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise CleanMacError(f"{label} must be between {low} and {high}.", code="invalid_mail_limit")
    return value


def _headers(raw):
    result = {}
    payload = raw.get("payload", {})
    values = payload.get("headers", []) if isinstance(payload, dict) else []
    if not isinstance(values, list) or len(values) > 128:
        raise CleanMacError("Gmail returned invalid header metadata.", code="mail_invalid_response")
    allowed = {"from", "subject", "list-id", "list-unsubscribe", "list-unsubscribe-post", "auto-submitted", "precedence", "date"}
    for header in values:
        if not isinstance(header, dict):
            continue
        name, value = header.get("name"), header.get("value")
        if isinstance(name, str) and name.casefold() in allowed and isinstance(value, str):
            # Untrusted header text is bounded data, never an executable command.
            if name.casefold() in result:
                result[name.casefold()] = (result[name.casefold()] + "\n" + value)[:4096]
            else:
                result[name.casefold()] = value[:4096]
    return result


def unsubscribe_metadata(headers):
    """Do not resolve, open, or retain token-bearing header URLs during scan."""
    value = headers.get("list-unsubscribe", "")
    https = mailto = rejected = False
    for candidate in re.findall(r"<([^<>]+)>", value)[:10]:
        try:
            parsed = urlsplit(candidate)
            if parsed.scheme == "mailto":
                mailto = True
                continue
            host = parsed.hostname or ""
            if (parsed.scheme != "https" or not host or parsed.username or parsed.password
                    or parsed.port not in (None, 443) or parsed.fragment
                    or any(ord(c) <= 32 for c in candidate) or "." not in host
                    or host.casefold().rstrip(".").endswith((".localhost", ".local", ".internal"))):
                rejected = True
                continue
            try:
                ipaddress.ip_address(host)
                rejected = True  # Never hand off an IP-literal unsubscribe link.
                continue
            except ValueError:
                pass
            https = True
        except ValueError:
            rejected = True
    return {"header_present": bool(value), "https_header_present": https,
            "mailto_header_present": mailto, "unsafe_or_unsupported_target_present": rejected,
            "one_click_header_present": headers.get("list-unsubscribe-post", "").strip().casefold() == "list-unsubscribe=one-click",
            "automatic_action_available": False, "authentication_verified": False,
            "handoff": "Review this sender in Gmail and use its native unsubscribe control. No unsubscribe request or email has been sent."}


def normalize_message(raw, account, expected_id=None):
    if not isinstance(raw, dict):
        raise CleanMacError("Gmail returned invalid message metadata.", code="mail_invalid_response")
    identifier = message_id(raw.get("id"))
    if expected_id is not None and identifier != expected_id:
        raise CleanMacError("Gmail returned a different message identity.", code="mail_identity_changed")
    labels = raw.get("labelIds", [])
    if (not isinstance(labels, list) or len(labels) > 1000
            or any(not isinstance(label, str) or len(label) > 256 for label in labels)):
        raise CleanMacError("Gmail returned invalid label metadata.", code="mail_invalid_response")
    try:
        millis = int(raw["internalDate"])
        size = int(raw.get("sizeEstimate", 0))
        timestamp = dt.datetime.fromtimestamp(millis / 1000, dt.timezone.utc)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        raise CleanMacError("The message has no usable provider timestamp.", code="mail_invalid_response") from None
    if millis < 0 or size < 0 or size > 2**63 - 1:
        raise CleanMacError("Gmail returned invalid message sizes or dates.", code="mail_invalid_response")
    headers = _headers(raw)
    sender = parseaddr(headers.get("from", ""))[1].casefold()
    thread = raw.get("threadId")
    history = raw.get("historyId")
    if (not isinstance(thread, str) or len(thread) > 256
            or not isinstance(history, str) or not history or len(history) > 100):
        raise CleanMacError("Gmail message identity/history metadata is missing.", code="mail_invalid_response")
    result = {"id": "gmail-" + digest({"account": account, "message_id": identifier})[:32],
              "provider": "gmail", "account": account, "message_id": identifier,
              "thread_id": thread, "history_id": history, "labels": sorted(set(labels)),
              "internal_date_ms": millis, "received_at": timestamp.isoformat(),
              "size_estimate": size, "size_basis": "provider estimate; not local disk space",
              "sender": sender, "display_sender": display_path(headers.get("from", "")),
              "display_subject": display_path(headers.get("subject", "")),
              "list_id": headers.get("list-id", ""),
              "automated": bool(headers.get("auto-submitted", "").casefold() not in ("", "no")
                                or headers.get("precedence", "").casefold() in ("bulk", "list", "junk")),
              "unsubscribe": unsubscribe_metadata(headers),
              "selected": False}
    result["fingerprint"] = digest({key: result[key] for key in ("account", "message_id", "thread_id", "history_id", "labels", "internal_date_ms", "size_estimate")})
    return result


def _months_before(now, months):
    serial = now.year * 12 + now.month - 1 - months
    year, month0 = divmod(serial, 12)
    month = month0 + 1
    return now.replace(year=year, month=month, day=min(now.day, calendar.monthrange(year, month)[1]))


def suggestions(items, *, now=None, months=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        raise CleanMacError("Suggestion time must include a timezone.", code="invalid_mail_date")
    settings = {"promotions": 6, "automated": 1, "unread": 3}
    if months is not None:
        if not isinstance(months, dict) or any(key not in settings for key in months):
            raise CleanMacError("Age rules support promotions, automated, and unread month thresholds.", code="invalid_mail_filter")
        for key, value in months.items():
            settings[key] = _integer(value, 1, 120, key + " months")
    cutoffs = {key: int(_months_before(now, value).timestamp() * 1000) for key, value in settings.items()}
    groups = {key: [] for key in ("promotions", "automated", "unread", "archived", "newsletters", "spam", "drafts")}
    senders = {}
    for item in items:
        labels = set(item["labels"])
        if "TRASH" in labels:
            continue
        senders.setdefault(item["sender"], []).append(item["id"])
        if "DRAFT" in labels:
            groups["drafts"].append(item["id"])
            continue  # Authored draft content is never a generic cleanup suggestion.
        if "SPAM" in labels:
            groups["spam"].append(item["id"])
        if item["list_id"] or item["unsubscribe"]["header_present"]:
            groups["newsletters"].append(item["id"])
        if "INBOX" not in labels and "SPAM" not in labels:
            groups["archived"].append(item["id"])
        if "CATEGORY_PROMOTIONS" in labels and item["internal_date_ms"] < cutoffs["promotions"]:
            groups["promotions"].append(item["id"])
        if item["automated"] and item["internal_date_ms"] < cutoffs["automated"]:
            groups["automated"].append(item["id"])
        if "UNREAD" in labels and item["internal_date_ms"] < cutoffs["unread"]:
            groups["unread"].append(item["id"])
    return {"groups": [{"category": key, "item_ids": values, "count": len(values),
                        "selection_default": False, "confidence": "header heuristic" if key in ("automated", "newsletters") else "provider metadata"}
                       for key, values in groups.items()],
            "unique_suggested_count": len(set(value for values in groups.values() for value in values)),
            "top_senders": [{"sender": sender, "count": len(ids), "item_ids": ids}
                            for sender, ids in sorted(senders.items(), key=lambda pair: (-len(pair[1]), pair[0]))[:50]],
            "age_months": settings, "cutoff_comparison": "strictly older than UTC calendar-month cutoff",
            "note": "These groups are review suggestions. Archived, unread, and authored mail are not automatically disposable."}


def _client(configuration, *, store=None):
    credential = configuration.get("credential", {})
    if isinstance(credential, dict) and credential.get("source") == "managed-oauth":
        from .oauth import access_token
        return GmailClient(configuration, token_provider=lambda: access_token(store, credential["connection_id"], provider="gmail"))
    return GmailClient(configuration)


def _configuration(configuration, store):
    if "oauth_connection" not in configuration:
        return configuration
    from .oauth import adapter_configuration
    managed = adapter_configuration(store, configuration["oauth_connection"], provider="gmail")
    for field in ("account", "scopes", "credential"):
        if field in configuration and configuration[field] != managed[field]:
            raise CleanMacError("The explicit account or grant differs from the saved OAuth connection.", code="mail_account_mismatch")
    resolved = dict(configuration)
    resolved.update(managed)
    return resolved


def _selected(options):
    selected = options.get("selected", [])
    if (not isinstance(selected, list) or not selected or len(selected) > 500
            or any(not isinstance(value, str) or not re.fullmatch(r"gmail-[0-9a-f]{32}", value) for value in selected)):
        raise CleanMacError("Select 1–500 exact Gmail item IDs from this account's saved review.", code="invalid_mail_selection")
    if len(selected) != len(set(selected)):
        raise CleanMacError("Mail selections contain duplicate IDs.", code="invalid_mail_selection")
    return selected


def _unavailable(reason, message):
    return {"schema_version": 1, "tool": "gmail", "status": "dependency-needed", "reason": reason,
            "message": message, "items": [], "coverage": "unscanned", "limitations": _LIMITATIONS, "sources": SOURCES}


def _inventory(configuration, store, *, cancel=None, progress=None):
    if not configuration.get("account"):
        return _unavailable("mail_account_required", "Choose a Gmail account and authorize its metadata scope with your own OAuth client. Provide only a credential reference; never paste access tokens into the conversation or CLI options.")
    limit = _integer(configuration.get("max_messages", 200), 1, 2000, "max_messages")
    pages_limit = _integer(configuration.get("max_pages", 10), 1, 100, "max_pages")
    category = configuration.get("category", "all")
    if category not in _CATEGORIES:
        raise CleanMacError("Choose a supported Gmail review category.", code="invalid_mail_filter")
    sender = configuration.get("sender")
    if sender is not None and (not isinstance(sender, str) or len(sender) > 254):
        raise CleanMacError("sender must be an email string.", code="invalid_mail_filter")
    if category == "sender" and not sender:
        raise CleanMacError("The sender review requires an exact sender email.", code="invalid_mail_filter")
    # Validate configuration before a provider call.
    suggestions([], months=configuration.get("age_months"))
    try:
        client = _client(configuration, store=store)
        profile = client.check_account()
    except CleanMacError as exc:
        if exc.code in {"mail_auth_required", "mail_auth_expired", "mail_scope_or_policy_denied", "mail_credential_unavailable"}:
            return _unavailable(exc.code, exc.message)
        raise
    if configuration.get("mode", "inventory") == "connection":
        return {"schema_version": 1, "tool": "gmail", "status": "available", "profile": profile,
                "credential_reference": client.reference, "coverage": "profile-only", "limitations": _LIMITATIONS, "sources": SOURCES}
    if configuration.get("mode", "inventory") != "inventory":
        raise CleanMacError("Gmail mode must be inventory or connection.", code="invalid_mail_filter")
    record = {"schema_version": 1, "id": new_id("mail-scan"), "created_at": utc_now(), "kind": "gmail-inventory",
              "tool": "gmail", "account": client.account, "credential_reference": client.reference,
              "status": "running", "items": [], "errors": [], "profile": profile,
              "max_messages": limit, "max_pages": pages_limit, "pages": 0, "coverage": "metadata-only",
              "limitations": _LIMITATIONS, "sources": SOURCES, "requested_category": category,
              "requested_sender": sender, "query_sent": False, "bodies_fetched": False}
    store.put("scans", record)
    page_token = None
    seen_tokens, seen_ids = set(), set()
    finished = False
    try:
        while record["pages"] < pages_limit and len(record["items"]) < limit:
            if cancel and cancel():
                record["status"] = "cancelled"
                break
            # Provider labels are a scope bound; other suggestion filters remain local.
            labels = ["SPAM"] if category == "spam" else (["DRAFT"] if category == "drafts" else None)
            page_limit = min(100, limit - len(record["items"]))
            page = client.list_messages(limit=page_limit, page_token=page_token, labels=labels)
            record["pages"] += 1
            listed = page.get("messages", [])
            if not isinstance(listed, list) or len(listed) > page_limit:
                raise CleanMacError("Gmail returned an invalid message page.", code="mail_invalid_response")
            for entry in listed:
                if cancel and cancel():
                    record["status"] = "cancelled"
                    break
                identifier = message_id(entry.get("id") if isinstance(entry, dict) else None)
                if identifier in seen_ids:
                    continue
                seen_ids.add(identifier)
                try:
                    item = normalize_message(client.metadata(identifier), client.account, identifier)
                except CleanMacError as exc:
                    if exc.code == "mail_message_missing":
                        record["errors"].append({"message_id": identifier, "code": exc.code, "coverage": "unscanned"})
                        continue
                    raise
                record["items"].append(item)
                if progress:
                    progress({"phase": "gmail_metadata", "messages": len(record["items"]), "pages": record["pages"]})
                if len(record["items"]) >= limit:
                    break
            if record["status"] == "cancelled":
                break
            token = page.get("nextPageToken")
            if token is None or token == "":
                finished = True
                break
            if not isinstance(token, str) or len(token) > 4096 or token in seen_tokens:
                raise CleanMacError("Gmail pagination did not advance safely.", code="mail_pagination_stalled")
            seen_tokens.add(token)
            page_token = token
            store.put("scans", record)
    except CleanMacError as exc:
        record["errors"].append({"code": exc.code, "message": exc.message, "details": exc.details})
    if record["status"] != "cancelled":
        record["status"] = "complete" if finished and not record["errors"] else "partial"
    record["suggestions"] = suggestions(record["items"], months=configuration.get("age_months"))
    visible_ids = {item["id"] for item in record["items"]}
    if category not in ("all", "sender"):
        visible_ids &= set(next(group["item_ids"] for group in record["suggestions"]["groups"] if group["category"] == category))
    if sender:
        visible_ids &= {item["id"] for item in record["items"] if item["sender"] == sender.casefold()}
    record["review_item_ids"] = sorted(visible_ids)
    record["counts"] = {"messages_inspected": len(record["items"]), "matching_review": len(visible_ids),
                        "suggested_unique": record["suggestions"]["unique_suggested_count"]}
    record["continuation"] = "Rescan with a larger explicit bound to broaden coverage; each scan records its own observations."
    return store.put("scans", record)


def _plan(options, configuration, store):
    from .actions import create_action_plan
    operation = configuration.get("operation")
    if operation not in ("archive", "trash"):
        raise CleanMacError("Choose archive or trash. Permanent draft deletion and automatic unsubscribe are unavailable.", code="unsupported_mail_action")
    reference = credential_reference(configuration)
    if MODIFY_SCOPE not in reference["scopes"]:
        raise CleanMacError("Authorize a separate gmail.modify grant before preparing a mail change.", code="mail_modify_scope_required")
    scan = store.get("scans", options.get("record_id"))
    if scan.get("kind") != "gmail-inventory" or scan.get("account") != reference["account"]:
        raise CleanMacError("The saved scan does not belong to this Gmail account.", code="mail_account_mismatch")
    items = {item["id"]: item for item in scan.get("items", [])}
    selected = _selected(options)
    picked = []
    for identifier in selected:
        if identifier not in items:
            raise CleanMacError("A selected message is outside this saved inventory.", code="invalid_mail_selection")
        item = items[identifier]
        labels = set(item["labels"])
        if labels & {"DRAFT", "TRASH", "STARRED", "IMPORTANT"}:
            raise CleanMacError("Draft, trashed, starred, and important messages are protected. Review them in Gmail.", code="protected_mail")
        if operation == "archive" and "INBOX" not in labels:
            raise CleanMacError("An archive selection is already outside the Inbox; rescan and choose an applicable action.", code="mail_action_not_applicable")
        picked.append(item)
    payload = {"account": reference["account"], "credential_reference": reference, "operation": operation,
               "scan_id": scan["id"], "items": picked, "journal_id": new_id("mail-journal"),
               "selection_digest": digest(picked)}
    return create_action_plan(store, "gmail", operation, payload,
                              {"account": reference["account"], "action": operation, "message_count": len(picked),
                               "selected_messages": [{"id": item["id"], "sender": item["display_sender"], "subject": item["display_subject"], "prior_labels": item["labels"]} for item in picked],
                               "effect": "Changes server-side Gmail messages; this does not measure reclaimed local disk space.",
                               "recovery": "Archive can restore Inbox; Trash can be reversed while the provider retains the message. Independently changed messages are preserved.",
                               "concurrency": _LIMITATIONS[-1]},
                              preconditions={"account": reference["account"], "message_fingerprints": {item["message_id"]: item["fingerprint"] for item in picked}})


def _journal_from_plan(plan):
    payload = plan["payload"]
    return {"schema_version": 1, "id": payload["journal_id"], "kind": "gmail-action-journal",
            "tool": "gmail", "created_at": utc_now(), "plan_id": plan["id"], "plan_digest": plan["digest"],
            "account": payload["account"], "operation": payload["operation"], "status": "not_started",
            "restore_handle": payload["journal_id"],
            "operations": [{"item_id": item["id"], "message_id": item["message_id"], "before": item,
                            "status": "not_started"} for item in payload["items"]]}


def _validate_payload(plan):
    payload = plan.get("payload", {})
    if (payload.get("operation") not in _MUTATIONS or not isinstance(payload.get("items"), list)
            or not payload["items"] or len(payload["items"]) > 500
            or payload.get("selection_digest") != digest(payload["items"])):
        raise CleanMacError("The Gmail plan selection is invalid.", code="mail_plan_invalid")
    if len({item.get("id") for item in payload["items"]}) != len(payload["items"]):
        raise CleanMacError("The Gmail plan repeats a message.", code="mail_plan_invalid")
    for item in payload["items"]:
        if item.get("account") != payload.get("account") or item.get("id") != "gmail-" + digest({"account": payload["account"], "message_id": message_id(item.get("message_id"))})[:32]:
            raise CleanMacError("The Gmail plan has an inconsistent account/message identity.", code="mail_plan_invalid")
        fingerprint = digest({key: item.get(key) for key in ("account", "message_id", "thread_id", "history_id", "labels", "internal_date_ms", "size_estimate")})
        if item.get("fingerprint") != fingerprint or not isinstance(item.get("labels"), list):
            raise CleanMacError("The Gmail plan has inconsistent metadata fingerprints.", code="mail_plan_invalid")
        labels = set(item["labels"])
        if payload["operation"] in ("archive", "trash") and labels & {"DRAFT", "TRASH", "STARRED", "IMPORTANT"}:
            raise CleanMacError("The Gmail plan contains protected mail.", code="protected_mail")
        if payload["operation"] == "archive" and "INBOX" not in labels:
            raise CleanMacError("An archive plan includes a message outside the Inbox.", code="mail_plan_invalid")
    return payload


def _apply(options, configuration, store, *, cancel=None, progress=None):
    from .actions import checkpoint_action, execute_action_plan
    plan = store.get("plans", options.get("record_id"))
    payload = _validate_payload(plan)
    reference = credential_reference(configuration)
    if reference != payload["credential_reference"]:
        raise CleanMacError("Use the exact account, credential reference, and declared grant reviewed in the plan.", code="mail_account_mismatch")
    client = None

    def validate(current_plan):
        nonlocal client
        current_payload = _validate_payload(current_plan)
        client = _client(reference, store=store)
        client.check_account()
        if MODIFY_SCOPE not in reference["scopes"]:
            raise CleanMacError("Gmail changes require gmail.modify.", code="mail_modify_scope_required")
        for item in current_payload["items"]:
            if cancel and cancel():
                raise CleanMacError("Cancelled before any mail action.", code="cancelled")
            current = normalize_message(client.metadata(item["message_id"]), reference["account"], item["message_id"])
            if current["fingerprint"] != item["fingerprint"]:
                raise CleanMacError("A selected message changed after review. Rescan or inspect recovery before making a new plan.", code="mail_state_changed", details={"item_id": item["id"]})
        return {"account_checked": True, "message_count": len(current_payload["items"])}

    def execute(current_plan):
        journal = _journal_from_plan(current_plan)

        def save_journal():
            store.put("receipts", journal)
            checkpoint_action({"journal_id": journal["id"], "journal_digest": digest(journal),
                               "completed_item_ids": [entry["item_id"] for entry in journal["operations"] if entry["status"] == "completed"]})
        # Existing journals are never overwritten, including a crash before the outer receipt update.
        try:
            store.get("receipts", journal["id"])
        except (FileNotFoundError, CleanMacError) as exc:
            if isinstance(exc, CleanMacError) and exc.code != "state_not_found":
                raise
        else:
            raise CleanMacError("A journal already exists for this mail action; inspect it before recovery.", code="mail_recovery_required", details={"journal_id": journal["id"]})
        save_journal()
        try:
            for entry in journal["operations"]:
                if cancel and cancel():
                    raise CleanMacError("Stopped before the next selected message; earlier outcomes remain recorded.", code="cancelled")
                before = entry["before"]
                current = normalize_message(client.metadata(entry["message_id"]), reference["account"], entry["message_id"])
                if current["fingerprint"] != before["fingerprint"]:
                    entry["status"] = "skipped_changed"
                    save_journal()
                    continue
                # Persist running before transmitting the request. A timeout or crash is ambiguous.
                entry["status"] = "running"
                journal["status"] = "running"
                save_journal()
                response = client.change(entry["message_id"], payload["operation"])
                if not isinstance(response, dict) or response.get("id") != entry["message_id"]:
                    raise CleanMacError("The provider did not confirm the selected message identity.", code="mail_unverified_mutation")
                after = normalize_message(client.metadata(entry["message_id"]), reference["account"], entry["message_id"])
                labels = set(after["labels"])
                desired = {"archive": "INBOX" not in labels, "trash": "TRASH" in labels,
                           "restore-archive": "INBOX" in labels, "restore-trash": "TRASH" not in labels}[payload["operation"]]
                if not desired:
                    raise CleanMacError("The message action could not be verified; inspect it in Gmail before continuing.", code="mail_unverified_mutation")
                entry.update(status="completed", after=after, completed_at=utc_now())
                if payload["operation"].startswith("restore-"):
                    original = payload.get("original_labels", {}).get(entry["item_id"], [])
                    entry["original_labels_restored_exactly"] = after["labels"] == original
                save_journal()
                if progress:
                    progress({"phase": "gmail_action", "completed": sum(item["status"] == "completed" for item in journal["operations"]), "total": len(journal["operations"])})
        except BaseException as exc:
            journal["status"] = "recovery_needed"
            journal["error"] = {"code": exc.code if isinstance(exc, CleanMacError) else "mail_interrupted",
                                "message": "Mail processing stopped. Completed, skipped, and ambiguous messages are recorded separately."}
            save_journal()
            raise CleanMacError("Mail processing stopped; do not repeat the plan. Review the durable per-message journal.",
                                code="mail_recovery_required", details={"journal_id": journal["id"], "cause": journal["error"]["code"]}) from None
        skipped = sum(item["status"] != "completed" for item in journal["operations"])
        journal["status"] = "partial" if skipped else "completed"
        save_journal()
        return {"outcome": journal["status"], "journal_id": journal["id"], "restore_handle": journal["id"],
                "completed": len(journal["operations"]) - skipped, "skipped": skipped,
                "outcomes": [{"item_id": item["item_id"], "status": item["status"]} for item in journal["operations"]],
                "note": "Untrash removes the Trash label. It does not overwrite all labels or guarantee the exact original Inbox state."}

    return execute_action_plan(store, plan["id"], options.get("approve"), "gmail", payload["operation"], validate, execute, cancel=cancel, progress=progress)


def _restore_plan(options, configuration, store):
    from .actions import create_action_plan
    reference = credential_reference(configuration)
    if MODIFY_SCOPE not in reference["scopes"]:
        raise CleanMacError("Mail restoration requires a separately authorized gmail.modify grant.", code="mail_modify_scope_required")
    journal = store.get("receipts", options.get("record_id"))
    if (journal.get("kind") != "gmail-action-journal" or journal.get("account") != reference["account"]
            or journal.get("operation") not in ("archive", "trash")):
        raise CleanMacError("Choose this account's original archive or Trash journal.", code="mail_recovery_invalid")
    original_plan = store.get("plans", journal.get("plan_id"))
    # Validate persisted authority, not only the selected receipt's claimed scope.
    from .actions import validate_action_plan, validate_action_receipt
    validate_action_plan(original_plan, tool="gmail", action=journal["operation"])
    original = _validate_payload(original_plan)
    if journal.get("plan_digest") != original_plan.get("digest") or original.get("journal_id") != journal.get("id"):
        raise CleanMacError("The mail journal does not match its approved plan.", code="mail_recovery_invalid")
    outer_receipt = store.get("receipts", original_plan["id"].replace("plan-action-", "receipt-action-", 1))
    validate_action_receipt(outer_receipt, original_plan)
    checkpoint = outer_receipt.get("checkpoint") or {}
    if (outer_receipt.get("status") not in ("completed", "recovery_needed")
            or checkpoint.get("journal_id") != journal["id"] or checkpoint.get("journal_digest") != digest(journal)):
        raise CleanMacError("The mail journal is not bound to a finished or recovery-needed action receipt. Inspect its outcomes before restoration.", code="mail_recovery_invalid")
    allowed = {item["id"]: item for item in original["items"]}
    outcomes = {entry.get("item_id"): entry for entry in journal.get("operations", [])}
    picked, original_labels = [], {}
    for identifier in _selected(options):
        entry = outcomes.get(identifier)
        if (identifier not in allowed or not entry or entry.get("status") != "completed"
                or entry.get("before") != allowed[identifier] or not isinstance(entry.get("after"), dict)
                or entry["after"].get("message_id") != allowed[identifier]["message_id"]
                or entry["after"].get("account") != reference["account"]):
            raise CleanMacError("Only verified completed messages from this journal can be restored automatically. Inspect ambiguous outcomes in Gmail.", code="mail_recovery_invalid")
        picked.append(entry["after"])
        original_labels[identifier] = entry["before"]["labels"]
    operation = "restore-" + journal["operation"]
    payload = {"account": reference["account"], "credential_reference": reference, "operation": operation,
               "items": picked, "selection_digest": digest(picked), "journal_id": new_id("mail-journal"),
               "source_journal_id": journal["id"], "original_labels": original_labels}
    return create_action_plan(store, "gmail", operation, payload,
                              {"account": reference["account"], "action": operation, "message_count": len(picked),
                               "effect": "Restore Inbox for archived messages, or remove Trash for trashed messages; preserve unrelated labels.",
                               "conflicts": "Messages changed since the verified original action are preserved and need manual review.",
                               "selected_message_ids": [item["id"] for item in picked]},
                              preconditions={"account": reference["account"], "source_journal_id": journal["id"]})


def _attachments(options, store, *, cancel=None, progress=None):
    from .inventory import scan_roots
    roots = options.get("roots", [])
    guidance = {"tool": "mail-attachments", "status": "report-only",
                "remote_recoverability": "unverified", "local_edit_provenance": "unverified",
                "cleanup_available": False,
                "message": "Folder names, file ages, and cache locations do not prove an attachment can be recovered. Preserve local-only, POP, disconnected-account, and edited attachments. Review messages and save wanted attachments in Apple Mail.",
                "handoff": "Use Apple Mail's message and attachment controls after verifying your own saved copy. Removing attachments from a message may affect its server copy; it is not equivalent to local cache eviction.",
                "sources": ["https://support.apple.com/guide/mail/view-save-or-delete-email-attachments-mlhlp1123/mac"]}
    if not roots:
        return dict(guidance, coverage="unscanned", items=[])
    maximum = _integer(options.get("configuration", {}).get("max_items", 10000), 1, 100000, "max_items")
    exclusions = [str(store.root), *store.get_settings().get("ignore_paths", [])]
    scan = scan_roots(roots, max_items=maximum, exclude_paths=exclusions, cancel=cancel, progress=progress)
    inventory_status = scan.get("status", "partial")
    for item in scan.get("items", []):
        item.update(eligible=False, cleanup_available=False, recoverability="unverified", local_edit_provenance="unverified")
    scan.update(guidance)
    scan["inventory_status"] = inventory_status
    scan["id"] = new_id("mail-attachments")
    scan["kind"] = "mail-attachment-inventory"
    scan.setdefault("schema_version", 1)
    scan.setdefault("created_at", utc_now())
    scan["coverage"] = "metadata-only; no attachment contents or mailbox database read"
    return store.put("scans", scan)


def dispatch(name, action, options, *, store, cancel=None, progress=None):
    if not isinstance(options, dict) or not isinstance(options.get("configuration", {}), dict):
        raise CleanMacError("Tool options and configuration must be JSON objects.", code="invalid_mail_configuration")
    configuration = options.get("configuration", {})
    if name == "mail-attachments" and action == "inspect":
        return _attachments(options, store, cancel=cancel, progress=progress)
    if name != "gmail":
        raise CleanMacError("The requested Mail tool/action is unavailable.", code="unsupported_mail_action")
    configuration = _configuration(configuration, store)
    if action == "inspect":
        return _inventory(configuration, store, cancel=cancel, progress=progress)
    if action == "plan":
        return _plan(options, configuration, store)
    if action == "apply":
        return _apply(options, configuration, store, cancel=cancel, progress=progress)
    if action == "restore":
        return _restore_plan(options, configuration, store)
    raise CleanMacError("Choose inspect, plan, apply, or restore. Restore prepares a new plan for review.", code="unsupported_mail_action")

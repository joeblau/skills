"""Bounded Gmail metadata transport. Credentials never enter state records.

Only the documented Gmail endpoints below are reachable. In particular, there
is no message body, attachment, send, draft-delete, or arbitrary-URL operation.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import sys
from urllib import error, parse, request

from .common import CleanMacError

API = "https://gmail.googleapis.com/gmail/v1/users/me/"
METADATA_SCOPE = "https://www.googleapis.com/auth/gmail.metadata"
MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
HEADERS = ("From", "Subject", "List-ID", "List-Unsubscribe", "List-Unsubscribe-Post",
           "Auto-Submitted", "Precedence", "Date")
MESSAGE_FIELDS = "id,threadId,labelIds,historyId,internalDate,sizeEstimate,payload(headers)"
ID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
_ENV = re.compile(r"CLEANMAC_GMAIL_[A-Z0-9_]{1,100}\Z")
_MAX_RESPONSE = 2 * 1024 * 1024


def message_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise CleanMacError("The Gmail message ID is invalid.", code="invalid_mail_id")
    return value


def account_name(value):
    if (not isinstance(value, str) or len(value) > 254
            or not re.fullmatch(r"[^\s@<>\x00-\x1f]+@[^\s@<>\x00-\x1f]+\.[^\s@<>\x00-\x1f]+", value)):
        raise CleanMacError("Specify the Gmail account email address explicitly.", code="mail_account_required")
    return value.casefold()


def credential_reference(configuration):
    """Allow references only, never token material in CLI options or plans."""
    if any(key in configuration for key in ("token", "access_token", "refresh_token", "client_secret")):
        raise CleanMacError("Provide a credential reference, not a token in command options.", code="mail_credential_reference_required")
    account = account_name(configuration.get("account"))
    supplied = configuration.get("credential", {})
    if not isinstance(supplied, dict):
        raise CleanMacError("credential must be an environment or Keychain reference.", code="invalid_mail_credential")
    source = supplied.get("source", "environment")
    if source == "environment":
        name = supplied.get("name", "CLEANMAC_GMAIL_ACCESS_TOKEN")
        if not isinstance(name, str) or not _ENV.fullmatch(name):
            raise CleanMacError("Use a CLEANMAC_GMAIL_ environment variable name for the access token.", code="invalid_mail_credential")
        reference = {"source": source, "name": name}
    elif source == "keychain":
        service = supplied.get("service", "b-skills.cleanmac.gmail.access-token")
        if (not isinstance(service, str) or not service.startswith("b-skills.cleanmac.gmail")
                or len(service) > 200 or any(ord(c) < 32 for c in service)):
            raise CleanMacError("Use a b-skills.cleanmac.gmail Keychain service reference.", code="invalid_mail_credential")
        reference = {"source": source, "service": service, "account": account}
    elif source == "managed-oauth":
        identifier = supplied.get("connection_id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", identifier):
            raise CleanMacError("Choose a saved Gmail OAuth connection ID.", code="invalid_mail_credential")
        reference = {"source": source, "connection_id": identifier}
    else:
        raise CleanMacError("Supported credential sources are environment, keychain, and a managed OAuth connection.", code="invalid_mail_credential")
    if set(supplied) - (set(reference) | {"account"}):
        raise CleanMacError("Credential references must not contain extra fields or token values.", code="invalid_mail_credential")
    scopes = configuration.get("scopes", [METADATA_SCOPE])
    if (not isinstance(scopes, list) or not scopes or len(scopes) > 3
            or any(scope not in (METADATA_SCOPE, MODIFY_SCOPE, READONLY_SCOPE) for scope in scopes)):
        raise CleanMacError("Declare the metadata, readonly, or modify Gmail scope used by this token.", code="invalid_mail_scope")
    return {"account": account, "credential": reference, "scopes": sorted(set(scopes))}


def _read_token(reference):
    credential = reference["credential"]
    if credential["source"] == "environment":
        token = os.environ.get(credential["name"], "")
    else:
        if sys.platform != "darwin":
            raise CleanMacError("Keychain credentials require macOS.", code="mail_credential_unavailable")
        try:
            result = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-s", credential["service"],
                 "-a", credential["account"], "-w"], capture_output=True, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise CleanMacError("Keychain did not return the selected credential. Resolve its access prompt or skip Gmail.", code="mail_credential_unavailable") from None
        token = result.stdout.decode("utf-8", errors="replace").rstrip("\r\n") if result.returncode == 0 else ""
    if not token:
        raise CleanMacError("The selected Gmail access-token reference is empty or unavailable. Authorize the account with your own OAuth client, then reconnect.", code="mail_auth_required")
    if len(token) > 16384 or any(ord(c) <= 32 or ord(c) >= 127 for c in token):
        raise CleanMacError("The selected credential is not a valid access-token value.", code="invalid_mail_credential")
    return token


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GmailClient:
    def __init__(self, configuration, *, token_provider=None):
        self.reference = credential_reference(configuration)
        self.account = self.reference["account"]
        if self.reference["credential"]["source"] == "managed-oauth":
            if not callable(token_provider):
                raise CleanMacError("A managed OAuth connection needs the local account manager.", code="mail_auth_required")
            self._token = token_provider()
            if (not isinstance(self._token, str) or not self._token or len(self._token) > 16384
                    or any(ord(c) <= 32 or ord(c) >= 127 for c in self._token)):
                raise CleanMacError("The account manager did not return a valid access token.", code="mail_auth_required")
        else:
            self._token = _read_token(self.reference)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        tls.load_default_certs()
        self._opener = request.build_opener(request.ProxyHandler({}), _NoRedirect(),
                                            request.HTTPSHandler(context=tls))

    def _request(self, method, path, *, query=None, body=None):
        allowed_get = path in ("profile", "messages") or re.fullmatch(r"messages/[A-Za-z0-9_-]{1,256}", path)
        allowed_post = re.fullmatch(r"messages/[A-Za-z0-9_-]{1,256}/(modify|trash|untrash)", path)
        if not ((method == "GET" and allowed_get) or (method == "POST" and allowed_post)):
            raise CleanMacError("This Gmail endpoint is outside the metadata and recovery adapter.", code="unsupported_mail_endpoint")
        url = API + path
        if query:
            url += "?" + parse.urlencode(query, doseq=True)
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=30) as response:
                payload = response.read(_MAX_RESPONSE + 1)
                if len(payload) > _MAX_RESPONSE:
                    raise CleanMacError("Gmail returned more metadata than the request limit permits.", code="mail_response_limit")
                parsed = json.loads(payload or b"{}")
                if not isinstance(parsed, dict):
                    raise ValueError("object required")
                return parsed
        except error.HTTPError as exc:
            codes = {401: "mail_auth_expired", 403: "mail_scope_or_policy_denied", 404: "mail_message_missing",
                     429: "mail_rate_limited"}
            code = codes.get(exc.code, "mail_provider_error")
            retry = exc.headers.get("Retry-After", "") if exc.headers else ""
            details = {"http_status": exc.code}
            if retry.isdigit():
                details["retry_after_seconds"] = min(int(retry), 86400)
            raise CleanMacError("Gmail could not complete this request. Review the account grant or provider status before retrying; mutation outcomes may need recovery review.", code=code, details=details) from None
        except (error.URLError, OSError, TimeoutError):
            raise CleanMacError("The Gmail request did not complete. No mutation will be retried automatically.", code="mail_network_error") from None
        except (ValueError, UnicodeDecodeError):
            raise CleanMacError("Gmail returned invalid metadata.", code="mail_invalid_response") from None

    def check_account(self):
        profile = self._request("GET", "profile", query={"fields": "emailAddress,historyId,messagesTotal"})
        if account_name(profile.get("emailAddress")) != self.account:
            raise CleanMacError("This token belongs to a different Gmail account. No message action was attempted.", code="mail_account_mismatch")
        return {"account": self.account, "messages_total_estimate": profile.get("messagesTotal"),
                "history_id": str(profile.get("historyId", ""))}

    def list_messages(self, *, limit=100, page_token=None, labels=None):
        query = {"maxResults": limit, "includeSpamTrash": "true", "fields": "messages(id,threadId),nextPageToken,resultSizeEstimate"}
        if page_token:
            query["pageToken"] = page_token
        if labels:
            query["labelIds"] = labels
        # gmail.metadata deliberately does not support the q search parameter.
        return self._request("GET", "messages", query=query)

    def metadata(self, identifier):
        return self._request("GET", "messages/" + message_id(identifier),
                             query={"format": "metadata", "metadataHeaders": HEADERS, "fields": MESSAGE_FIELDS})

    def change(self, identifier, operation):
        if MODIFY_SCOPE not in self.reference["scopes"]:
            raise CleanMacError("This action needs a separately authorized gmail.modify grant; metadata access is read-only.", code="mail_modify_scope_required")
        prefix = "messages/" + message_id(identifier)
        if operation == "archive":
            return self._request("POST", prefix + "/modify", body={"removeLabelIds": ["INBOX"]}, query={"fields": "id,labelIds,historyId"})
        if operation == "restore-archive":
            return self._request("POST", prefix + "/modify", body={"addLabelIds": ["INBOX"]}, query={"fields": "id,labelIds,historyId"})
        if operation in ("trash", "restore-trash"):
            suffix = "trash" if operation == "trash" else "untrash"
            return self._request("POST", prefix + "/" + suffix, query={"fields": "id,labelIds,historyId"})
        raise CleanMacError("Gmail supports only archive, trash, and conflict-aware restoration here.", code="unsupported_mail_action")

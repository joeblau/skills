"""Explicit, bounded OAuth consent and private macOS Keychain token lifecycle.

Provider authorization uses a system-browser URL; the user handles sign-in and
consent. Google/Dropbox use S256 PKCE and a loopback-only callback. Microsoft uses
its public-client device grant. A worker is started only by accounts connect,
expires within 15 minutes, and never persists codes, verifiers or bearer tokens.
The project supplies no shared OAuth application: an operator registers a public
client with each provider and enables the scopes shown by accounts inspect.
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import signal
from socketserver import TCPServer
import ssl
import subprocess
import sys
import time
from urllib import error, parse, request

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "cleanmac_core"

from .common import CleanMacError, canonical_json, digest, new_id, utc_now
from .state import StateStore

PROVIDERS = ("gmail", "google-drive", "onedrive", "dropbox")
SCOPES = {
    "gmail": {"readonly": ["https://www.googleapis.com/auth/gmail.metadata"],
              "modify": ["https://www.googleapis.com/auth/gmail.modify"]},
    "google-drive": {"readonly": ["https://www.googleapis.com/auth/drive.metadata.readonly"],
                     "modify": ["https://www.googleapis.com/auth/drive"]},
    "onedrive": {"readonly": ["Files.Read", "User.Read", "offline_access"],
                 "modify": ["Files.ReadWrite", "User.Read", "offline_access"]},
    "dropbox": {"readonly": ["account_info.read", "files.metadata.read"],
                "modify": ["account_info.read", "files.metadata.read", "files.content.write"]},
}
SOURCES = [
    "https://developers.google.com/identity/protocols/oauth2/native-app",
    "https://docs.dropboxapi.com/dropbox-api/docs/get-started/authorization",
    "https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code",
    "https://developer.apple.com/documentation/security/keychain-services",
]
REVOCATION_URLS = {
    "gmail": "https://myaccount.google.com/connections",
    "google-drive": "https://myaccount.google.com/connections",
    "onedrive": "https://account.live.com/consent/Manage",
    "dropbox": "https://www.dropbox.com/account/connected_apps",
}
_SESSION = re.compile(r"oauth-session-[0-9a-f]{32}\Z")
_CONNECTION = re.compile(r"oauth-connection-[0-9a-f]{32}\Z")
_KEYCHAIN_SERVICE = "b-skills.cleanmac.oauth"
_MAX_RESPONSE = 256 * 1024
_AUTH_HOSTS = {"oauth2.googleapis.com", "api.dropboxapi.com", "login.microsoftonline.com",
               "gmail.googleapis.com", "www.googleapis.com", "graph.microsoft.com"}


def _fail(message, code="oauth_invalid_configuration", **details):
    raise CleanMacError(message, code=code, details=details or None)


def configuration(value):
    if not isinstance(value, dict):
        _fail("OAuth configuration must be an object.")
    allowed = {"provider", "client_id", "grant", "account", "tenant", "timeout", "port", "client_secret_env"}
    if set(value) - allowed:
        _fail("OAuth accepts public client configuration and credential references only; never provide tokens or client secrets.")
    provider = value.get("provider")
    if provider not in PROVIDERS:
        _fail("Choose gmail, google-drive, onedrive, or dropbox.")
    client = value.get("client_id")
    if not isinstance(client, str) or not re.fullmatch(r"[A-Za-z0-9._-]{3,256}", client):
        _fail("Provide the public application/client ID from your registered desktop app.", "oauth_client_required")
    if provider in {"gmail", "google-drive"} and not client.endswith(".apps.googleusercontent.com"):
        _fail("Google requires a registered Desktop app client ID ending in .apps.googleusercontent.com.", "oauth_client_required")
    grant = value.get("grant", "readonly")
    if grant not in ("readonly", "modify"):
        _fail("Choose readonly or modify; scope upgrades require a separate consent connection.")
    timeout = value.get("timeout", 300)
    if type(timeout) is not int or not 30 <= timeout <= 900:
        _fail("The explicit sign-in window must be 30–900 seconds.")
    port = value.get("port", 53682 if provider == "dropbox" else 0)
    if type(port) is not int or (port != 0 and not 1024 <= port <= 65535):
        _fail("The callback port must be zero (Google random port) or 1024–65535.")
    if provider == "dropbox" and port == 0:
        _fail("Dropbox requires a fixed registered loopback redirect port.")
    tenant = value.get("tenant", "consumers")
    if not isinstance(tenant, str) or not (tenant in {"consumers", "common", "organizations"}
            or re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", tenant)):
        _fail("Use a Microsoft tenant GUID, consumers, common, or organizations.")
    account = value.get("account")
    if account is not None and (not isinstance(account, str) or len(account) > 254
            or not re.fullmatch(r"[^\s@<>\x00-\x1f]+@[^\s@<>\x00-\x1f]+\.[^\s@<>\x00-\x1f]+", account)):
        _fail("The optional expected account must be an email address.")
    secret_env = value.get("client_secret_env")
    if secret_env is not None and (provider not in {"gmail", "google-drive"}
            or not isinstance(secret_env, str) or not re.fullmatch(r"CLEANMAC_[A-Z0-9_]{1,100}", secret_env)):
        _fail("Google's optional desktop client secret must be a CLEANMAC_ environment reference.")
    return {"provider": provider, "client_id": client, "grant": grant,
            "scopes": list(SCOPES[provider][grant]), "timeout": timeout, "port": port,
            "tenant": tenant, "account": account.casefold() if account else None,
            "client_secret_env": secret_env}


def _configuration_from_record(value):
    if not isinstance(value, dict):
        _fail("The saved OAuth configuration is invalid.", "oauth_invalid_record")
    normalized = configuration({key: value[key] for key in value if key != "scopes"})
    if normalized != value:
        _fail("The saved OAuth scopes or configuration changed.", "oauth_invalid_record")
    return normalized


def _safe_token(value, *, required=True):
    if value is None and not required:
        return None
    if (not isinstance(value, str) or not value or len(value) > 32768
            or any(ord(char) <= 32 or ord(char) >= 127 for char in value)):
        _fail("The provider returned an invalid credential value.", "oauth_invalid_response")
    return value


class Keychain:
    """Security.framework calls keep secrets out of process arguments and files."""
    def __init__(self):
        if sys.platform != "darwin":
            _fail("Managed OAuth credentials require macOS Keychain.", "oauth_keychain_unavailable")
        try:
            self.cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            self.sec = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        except OSError:
            _fail("macOS Keychain services are unavailable.", "oauth_keychain_unavailable")
        ptr, count = ctypes.c_void_p, ctypes.c_long
        for name, args, result in (
            ("CFStringCreateWithCString", [ptr, ctypes.c_char_p, ctypes.c_uint32], ptr),
            ("CFDictionaryCreateMutable", [ptr, count, ptr, ptr], ptr),
            ("CFDictionarySetValue", [ptr, ptr, ptr], None),
            ("CFDataCreate", [ptr, ptr, count], ptr),
            ("CFDataGetLength", [ptr], count), ("CFDataGetBytePtr", [ptr], ptr),
            ("CFRelease", [ptr], None),
        ):
            function = getattr(self.cf, name)
            function.argtypes, function.restype = args, result
        self.sec.SecItemCopyMatching.argtypes = [ptr, ctypes.POINTER(ptr)]
        self.sec.SecItemAdd.argtypes = [ptr, ctypes.POINTER(ptr)]
        self.sec.SecItemUpdate.argtypes = [ptr, ptr]
        self.sec.SecItemDelete.argtypes = [ptr]
        for name in ("SecItemCopyMatching", "SecItemAdd", "SecItemUpdate", "SecItemDelete"):
            getattr(self.sec, name).restype = ctypes.c_int32

    def _constant(self, key):
        library = self.cf if key.startswith("kCF") else self.sec
        return ctypes.c_void_p.in_dll(library, key).value

    def _dictionary(self, values):
        refs = []
        dictionary = self.cf.CFDictionaryCreateMutable(None, 0, None, None)
        if not dictionary:
            _fail("Keychain request allocation failed.", "oauth_keychain_unavailable")
        refs.append(dictionary)
        for key, value in values.items():
            if isinstance(value, str):
                converted = self.cf.CFStringCreateWithCString(None, value.encode(), 0x08000100)
                refs.append(converted)
            elif isinstance(value, bytes):
                converted = self.cf.CFDataCreate(None, ctypes.cast(ctypes.c_char_p(value), ctypes.c_void_p), len(value))
                refs.append(converted)
            else:
                converted = value
            if not converted:
                for ref in reversed(refs):
                    if ref:
                        self.cf.CFRelease(ref)
                _fail("Keychain request allocation failed.", "oauth_keychain_unavailable")
            self.cf.CFDictionarySetValue(dictionary, self._constant(key), converted)
        return dictionary, refs

    def _query(self, identifier, extra=None):
        if not isinstance(identifier, str) or not _CONNECTION.fullmatch(identifier):
            _fail("Choose a managed OAuth connection.", "oauth_invalid_record")
        return self._dictionary({"kSecClass": self._constant("kSecClassGenericPassword"),
                                 "kSecAttrService": _KEYCHAIN_SERVICE,
                                 "kSecAttrAccount": identifier, **(extra or {})})

    def _release(self, refs):
        for ref in reversed(refs):
            self.cf.CFRelease(ref)

    def _check(self, status):
        if status:
            _fail("Keychain did not permit the credential operation. Handle its prompt yourself, or skip this account.",
                  "oauth_keychain_denied", os_status=status)

    def contains(self, identifier):
        """Check this exact entry's existence without secret data or UI prompts.

        Only errSecItemNotFound proves absence. A locked keychain, authentication
        requirement, unsupported query, or other failure leaves existence unknown
        and raises so callers preserve the connection/recovery record.
        """
        try:
            extra = {"kSecMatchLimit": self._constant("kSecMatchLimitOne"),
                     "kSecUseAuthenticationUI": self._constant("kSecUseAuthenticationUIFail")}
        except (ValueError, OSError, AttributeError):
            _fail("A noninteractive Keychain existence check is unavailable; preserve the connection record.",
                  "oauth_keychain_unavailable")
        query, refs = self._query(identifier, extra)
        try:
            # No return-data/attributes/reference flag and no result buffer: this
            # checks existence only, without decrypting or copying token bytes.
            status = self.sec.SecItemCopyMatching(query, None)
            if status == -25300:
                return False
            self._check(status)
            return True
        finally:
            self._release(refs)

    def get(self, identifier):
        query, refs = self._query(identifier, {"kSecReturnData": self._constant("kCFBooleanTrue"),
                                             "kSecMatchLimit": self._constant("kSecMatchLimitOne")})
        result = ctypes.c_void_p()
        try:
            status = self.sec.SecItemCopyMatching(query, ctypes.byref(result))
            if status == -25300:
                _fail("This connection's stored credentials are unavailable; reconnect it.", "oauth_auth_required")
            self._check(status)
            length = self.cf.CFDataGetLength(result)
            if not 1 <= length <= _MAX_RESPONSE:
                _fail("Stored OAuth credentials are invalid.", "oauth_invalid_credential")
            raw = ctypes.string_at(self.cf.CFDataGetBytePtr(result), length)
            try:
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError()
                return value
            except (ValueError, UnicodeError):
                _fail("Stored OAuth credentials are invalid.", "oauth_invalid_credential")
        finally:
            if result.value:
                self.cf.CFRelease(result)
            self._release(refs)

    def put(self, identifier, value):
        data = canonical_json(value).encode()
        if len(data) > _MAX_RESPONSE:
            _fail("Stored OAuth credential exceeds its size limit.", "oauth_invalid_credential")
        query, refs = self._query(identifier, {"kSecValueData": data})
        try:
            status = self.sec.SecItemAdd(query, None)
            if status == -25299:
                match, match_refs = self._query(identifier)
                update, update_refs = self._dictionary({"kSecValueData": data})
                try:
                    status = self.sec.SecItemUpdate(match, update)
                finally:
                    self._release(update_refs)
                    self._release(match_refs)
            self._check(status)
        finally:
            self._release(refs)

    def delete(self, identifier):
        query, refs = self._query(identifier)
        try:
            status = self.sec.SecItemDelete(query)
            if status != -25300:
                self._check(status)
        finally:
            self._release(refs)


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _fail("OAuth and credential requests never follow redirects.", "oauth_redirect_rejected")


class OAuthResponseError(CleanMacError):
    def __init__(self, status, code):
        self.oauth_error = code if code in {"authorization_pending", "slow_down", "access_denied", "authorization_declined", "expired_token", "invalid_grant", "invalid_client", "invalid_scope"} else "provider_rejected"
        super().__init__("The provider did not authorize this credential request. Check the registered app and grant, or reconnect.",
                         code="oauth_provider_rejected", details={"http_status": status, "oauth_error": self.oauth_error})


def _http(method, url, *, form=None, token=None, body=None):
    parsed = parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in _AUTH_HOSTS or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.fragment or method not in {"GET", "POST"}):
        _fail("OAuth request endpoint is not supported.", "oauth_invalid_endpoint")
    headers = {"Accept": "application/json"}
    data = None
    if form is not None:
        data = parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if body is not None:
        data = canonical_json(body).encode()
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = "Bearer " + _safe_token(token)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls.load_default_certs()
    # Unlike create_default_context, this does not opt into SSLKEYLOGFILE.
    opener = request.build_opener(request.ProxyHandler({}), _NoRedirect(), request.HTTPSHandler(context=tls))
    try:
        with opener.open(request.Request(url, data=data, headers=headers, method=method), timeout=15) as response:
            raw = response.read(_MAX_RESPONSE + 1)
            if len(raw) > _MAX_RESPONSE:
                _fail("OAuth response exceeded its size limit.", "oauth_invalid_response")
            value = json.loads(raw or b"{}", parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(value, dict):
                raise ValueError()
            return value
    except error.HTTPError as exc:
        try:
            raw = exc.read(_MAX_RESPONSE + 1)
            parsed_error = json.loads(raw) if len(raw) <= _MAX_RESPONSE else {}
            code = parsed_error.get("error") if isinstance(parsed_error, dict) else None
            if not isinstance(code, str):
                code = "provider_rejected"
        except (ValueError, UnicodeError, OSError):
            code = "provider_rejected"
        finally:
            exc.close()
        raise OAuthResponseError(exc.code, code) from None
    except (error.URLError, OSError, TimeoutError):
        _fail("The OAuth request did not complete. No credentials were printed; inspect connection status before retrying.", "oauth_network_error")
    except (ValueError, UnicodeError):
        _fail("The provider returned invalid OAuth metadata.", "oauth_invalid_response")


def _token_endpoint(config):
    provider = config["provider"]
    if provider in {"gmail", "google-drive"}:
        return "https://oauth2.googleapis.com/token"
    if provider == "dropbox":
        return "https://api.dropboxapi.com/oauth2/token"
    return "https://login.microsoftonline.com/" + config["tenant"] + "/oauth2/v2.0/token"


def _client_secret(config):
    reference = config.get("client_secret_env")
    if reference is None:
        return {}
    value = os.environ.get(reference)
    if not value:
        _fail("The configured desktop-client secret reference is unavailable.", "oauth_client_secret_required")
    return {"client_secret": _safe_token(value)}


def pkce():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
    return verifier, challenge


def authorization_url(config, redirect_uri, state, challenge):
    provider = config["provider"]
    query = {"client_id": config["client_id"], "response_type": "code", "redirect_uri": redirect_uri,
             "scope": " ".join(config["scopes"]), "state": state, "code_challenge": challenge,
             "code_challenge_method": "S256"}
    if provider in {"gmail", "google-drive"}:
        endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
        query.update(access_type="offline", prompt="consent")
        if config["account"]:
            query["login_hint"] = config["account"]
    elif provider == "dropbox":
        endpoint = "https://www.dropbox.com/oauth2/authorize"
        query.update(token_access_type="offline", force_reapprove="true", require_role="personal")
    else:
        _fail("This provider uses the device consent flow.")
    return endpoint + "?" + parse.urlencode(query)


def parse_callback(target, expected_state):
    if not isinstance(target, str) or len(target) > 16384:
        _fail("Invalid OAuth callback.", "oauth_callback_rejected")
    parts = parse.urlsplit(target)
    if parts.path != "/callback" or parts.scheme or parts.netloc or parts.fragment:
        _fail("Invalid OAuth callback path.", "oauth_callback_rejected")
    pairs = parse.parse_qs(parts.query, keep_blank_values=True, max_num_fields=16)
    if any(len(values) != 1 for values in pairs.values()):
        _fail("Ambiguous OAuth callback parameters.", "oauth_callback_rejected")
    state = pairs.get("state", [""])[0]
    if (not state.isascii() or not isinstance(expected_state, str) or not expected_state.isascii()
            or not hmac.compare_digest(state, expected_state)):
        _fail("The OAuth callback did not match this consent session.", "oauth_callback_rejected")
    if "error" in pairs:
        return {"denied": True}
    code = pairs.get("code", [""])[0]
    if not code or len(code) > 8192 or any(ord(char) < 32 for char in code):
        _fail("The OAuth authorization code is invalid.", "oauth_callback_rejected")
    return {"code": code}


def _tokens(response, config, *, previous=None):
    if not isinstance(response, dict) or str(response.get("token_type", "")).casefold() != "bearer":
        _fail("The provider did not return a supported bearer grant.", "oauth_invalid_response")
    access = _safe_token(response.get("access_token"))
    refresh = _safe_token(response.get("refresh_token", (previous or {}).get("refresh_token")), required=False)
    expires = response.get("expires_in")
    if type(expires) is not int or not 1 <= expires <= 366 * 86400:
        _fail("The provider did not return a bounded token expiry.", "oauth_invalid_response")
    scopes = response.get("scope")
    if scopes is None:
        scopes = (previous or {}).get("scopes", config["scopes"])
    elif isinstance(scopes, str) and len(scopes) <= 16384:
        scopes = scopes.split()
    if not isinstance(scopes, list) or any(not isinstance(scope, str) or len(scope) > 512 for scope in scopes):
        _fail("The provider returned invalid grant scopes.", "oauth_invalid_response")
    required = set(config["scopes"]) - {"offline_access"}
    actual = set(scopes)
    if not required.issubset(actual):
        _fail("The account did not grant all requested capabilities; reconnect with the intended grant.", "oauth_scope_denied")
    if actual - set(config["scopes"]):
        _fail("The provider returned scopes beyond the explicitly requested grant. Use a separate client/grant with the intended permissions.", "oauth_grant_changed")
    if previous is not None and set(previous["scopes"]) != actual:
        _fail("The granted scopes changed during refresh; prepare a new connection.", "oauth_grant_changed")
    return {"access_token": access, "refresh_token": refresh, "expires_at": time.time() + expires,
            "scopes": sorted(actual)}


def _identity(provider, token):
    if provider == "gmail":
        result = _http("GET", "https://gmail.googleapis.com/gmail/v1/users/me/profile?fields=emailAddress", token=token)
        identifier = email = result.get("emailAddress")
    elif provider == "google-drive":
        result = _http("GET", "https://www.googleapis.com/drive/v3/about?fields=user(permissionId,emailAddress)", token=token)
        user = result.get("user", {})
        identifier, email = user.get("permissionId"), user.get("emailAddress")
    elif provider == "onedrive":
        result = _http("GET", "https://graph.microsoft.com/v1.0/me?$select=id,mail,userPrincipalName", token=token)
        identifier, email = result.get("id"), result.get("mail") or result.get("userPrincipalName")
    else:
        result = _http("POST", "https://api.dropboxapi.com/2/users/get_current_account", token=token)
        identifier, email = result.get("account_id"), result.get("email")
    if (not isinstance(identifier, str) or not identifier or len(identifier) > 512
            or any(ord(char) < 32 for char in identifier) or not isinstance(email, str)
            or len(email) > 254 or "@" not in email or any(ord(char) < 32 for char in email)):
        _fail("The provider account identity could not be verified.", "oauth_identity_unverified")
    return {"account_id": identifier.casefold() if provider == "gmail" else identifier,
            "account_email": email.casefold()}


def _session(store, identifier):
    if not isinstance(identifier, str) or not _SESSION.fullmatch(identifier):
        _fail("Choose a saved OAuth consent session.", "oauth_invalid_record")
    value = store.get("scans", identifier)
    if value.get("record_type") != "oauth_session" or value.get("tool") != "accounts":
        _fail("This is not an OAuth consent session.", "oauth_invalid_record")
    _configuration_from_record(value.get("configuration"))
    return value


def _session_update(store, identifier, **updates):
    with store.lock(identifier):
        current = _session(store, identifier)
        if (current["status"] in {"cancelled", "expired", "denied", "connected", "failed"}
                and updates.get("status", current["status"]) != current["status"]):
            return current
        current.update(updates, updated_at=utc_now())
        return store.put("scans", current)


def session_status(store, identifier):
    value = _session(store, identifier)
    if value.get("status") in {"starting", "waiting_for_consent", "exchanging"} and time.time() > value.get("expires_at", 0):
        value = _session_update(store, identifier, status="expired", authorization_url=None, user_code=None)
    return value


def cancel_session(store, identifier):
    value = _session(store, identifier)
    if value.get("status") not in {"starting", "waiting_for_consent", "exchanging"}:
        return value
    return _session_update(store, identifier, status="cancelled", authorization_url=None, user_code=None,
                           note="The bounded worker will stop; no code or credential is retained by this session. A provider grant already approved may need revocation in account settings.")


def start_connection(store, config):
    config = configuration(config)
    Keychain()  # Check native availability, without reading or changing any grant.
    _client_secret(config)
    identifier = new_id("oauth-session")
    value = {"schema_version": 1, "record_type": "oauth_session", "tool": "accounts", "id": identifier,
             "created_at": utc_now(), "updated_at": utc_now(), "status": "starting",
             "configuration": config, "expires_at": time.time() + config["timeout"],
             "connection_id": new_id("oauth-connection"), "authorization_url": None,
             "note": "Open only the returned provider consent URL in your system browser. You handle sign-in, consent, and any Keychain prompts."}
    store.put("scans", value)
    environment = {key: os.environ[key] for key in ("HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "__CF_USER_TEXT_ENCODING") if key in os.environ}
    if config.get("client_secret_env"):
        environment[config["client_secret_env"]] = os.environ[config["client_secret_env"]]
    try:
        subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), "--worker", str(store.root), identifier],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True, env=environment)
    except OSError:
        return _session_update(store, identifier, status="failed", error={"code": "oauth_worker_unavailable", "message": "The bounded consent worker could not start."})
    # Give the worker a short setup window, not a blocking sign-in prompt.
    for _ in range(20):
        value = _session(store, identifier)
        if value["status"] != "starting":
            break
        time.sleep(0.05)
    return value


def _is_pending(store, identifier):
    value = _session(store, identifier)
    return value["status"] in {"starting", "waiting_for_consent", "exchanging"} and time.time() < value["expires_at"]


def _loopback_code(store, session):
    config, identifier = session["configuration"], session["id"]
    verifier, challenge = pkce()
    state = secrets.token_urlsafe(32)
    outcome = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            try:
                if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                    _fail("Invalid callback host.", "oauth_callback_rejected")
                value = parse_callback(self.path, state)
                outcome.update(value)
                status, message = 200, "Authorization response received. You may close this tab and return to CleanMac."
            except (CleanMacError, ValueError):
                status, message = 400, "This callback does not match the active CleanMac authorization."
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'")
            self.end_headers()
            try:
                self.wfile.write(message.encode())
            except OSError:
                pass

    class LoopbackServer(HTTPServer):
        def server_bind(self):
            # HTTPServer resolves the hostname here, which can block on DNS.
            # The callback uses a fixed loopback address and needs no lookup.
            TCPServer.server_bind(self)
            self.server_name, self.server_port = self.server_address[:2]

        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(1)
            return connection, address

    server = LoopbackServer(("127.0.0.1", config["port"]), Handler)
    server.timeout = 1
    try:
        redirect = f"http://127.0.0.1:{server.server_port}/callback"
        _session_update(store, identifier, status="waiting_for_consent",
                        authorization_url=authorization_url(config, redirect, state, challenge),
                        redirect_uri=redirect)
        while not outcome and _is_pending(store, identifier):
            server.handle_request()
        if not _is_pending(store, identifier):
            return None
        if outcome.get("denied"):
            _session_update(store, identifier, status="denied", authorization_url=None)
            return None
        _session_update(store, identifier, status="exchanging", authorization_url=None)
        form = {"grant_type": "authorization_code", "client_id": config["client_id"],
                "code": outcome["code"], "code_verifier": verifier, "redirect_uri": redirect,
                **_client_secret(config)}
        return _http("POST", _token_endpoint(config), form=form)
    finally:
        server.server_close()


def _device_code(store, session):
    config, identifier = session["configuration"], session["id"]
    url = "https://login.microsoftonline.com/" + config["tenant"] + "/oauth2/v2.0/devicecode"
    response = _http("POST", url, form={"client_id": config["client_id"], "scope": " ".join(config["scopes"])})
    device = _safe_token(response.get("device_code"))
    user_code = response.get("user_code")
    verification = response.get("verification_uri")
    try:
        parsed = parse.urlsplit(verification or "")
        port = parsed.port
    except (ValueError, TypeError):
        _fail("The provider returned an invalid device consent response.", "oauth_invalid_response")
    if (not isinstance(user_code, str) or not re.fullmatch(r"[A-Z0-9-]{4,32}", user_code)
            or parsed.scheme != "https" or parsed.hostname not in {"microsoft.com", "www.microsoft.com", "login.microsoftonline.com"}
            or port not in (None, 443) or parsed.username or parsed.password or parsed.fragment):
        _fail("The provider returned an invalid device consent response.", "oauth_invalid_response")
    interval, expires = response.get("interval", 5), response.get("expires_in")
    if type(interval) is not int or not 1 <= interval <= 60 or type(expires) is not int or not 1 <= expires <= 1800:
        _fail("The provider returned invalid consent timing.", "oauth_invalid_response")
    deadline = min(session["expires_at"], time.time() + expires)
    _session_update(store, identifier, status="waiting_for_consent", authorization_url=verification,
                    user_code=user_code, expires_at=deadline)
    next_poll = time.monotonic() + interval
    while _is_pending(store, identifier):
        if time.monotonic() < next_poll:
            time.sleep(min(1, next_poll - time.monotonic()))
            continue
        try:
            return _http("POST", _token_endpoint(config), form={"client_id": config["client_id"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": device})
        except OAuthResponseError as exc:
            if exc.oauth_error == "slow_down":
                interval = min(60, interval + 5)
            elif exc.oauth_error != "authorization_pending":
                raise
        next_poll = time.monotonic() + interval
    return None


def run_worker(store, identifier):
    session = _session(store, identifier)
    config = session["configuration"]
    try:
        with store.lock("worker-" + identifier.removeprefix("oauth-session-"), timeout=0):
            if not _is_pending(store, identifier) or session["status"] != "starting":
                return
            response = _device_code(store, session) if config["provider"] == "onedrive" else _loopback_code(store, session)
            if response is None or not _is_pending(store, identifier):
                session_status(store, identifier)
                return
            tokens = _tokens(response, config)
            identity = _identity(config["provider"], tokens["access_token"])
            if config["account"] and identity["account_email"] != config["account"]:
                _fail("The authorized account differs from the account selected before consent.", "oauth_account_mismatch")
            connection = {"schema_version": 1, "record_type": "oauth_connection", "tool": "accounts",
                          "id": session["connection_id"], "created_at": utc_now(), "updated_at": utc_now(),
                          "provider": config["provider"], "configuration": config, "status": "connecting",
                          "grant_id": secrets.token_hex(32), "scopes": tokens["scopes"], **identity,
                          "expires_at": tokens["expires_at"], "refresh_available": bool(tokens["refresh_token"]),
                          "revocation_url": REVOCATION_URLS[config["provider"]]}
            with store.lock("oauth-" + connection["id"].removeprefix("oauth-connection-")):
                if not _is_pending(store, identifier):
                    return
                store.put("scans", connection)
                tokens.update(grant_id=connection["grant_id"], account_id=identity["account_id"], provider=config["provider"])
                Keychain().put(connection["id"], tokens)
                with store.lock(identifier):
                    if not _is_pending(store, identifier):
                        Keychain().delete(connection["id"])
                        connection.update(status="disconnected", expires_at=None, refresh_available=False)
                        store.put("scans", connection)
                        return
                    connection["status"] = "connected"
                    store.put("scans", connection)
                    current = _session(store, identifier)
                    current.update(status="connected", authorization_url=None, user_code=None, updated_at=utc_now())
                    store.put("scans", current)
    except BaseException as exc:
        try:
            current = _session(store, identifier)
            if current["status"] not in {"cancelled", "expired", "denied"}:
                code = exc.code if isinstance(exc, CleanMacError) else "oauth_connection_failed"
                _session_update(store, identifier, status="failed", authorization_url=None, user_code=None,
                                error={"code": code, "message": "Account connection did not finish. Check registered client settings, consent, and Keychain access. No credential values are included."})
        except Exception:
            pass
        if not isinstance(exc, Exception):
            raise


def connection_metadata(store, identifier, provider=None):
    if not isinstance(identifier, str) or not _CONNECTION.fullmatch(identifier):
        _fail("Choose a saved managed OAuth connection.", "oauth_invalid_record")
    value = store.get("scans", identifier)
    if (value.get("record_type") != "oauth_connection" or value.get("tool") != "accounts"
            or value.get("provider") not in PROVIDERS or (provider is not None and value.get("provider") != provider)):
        _fail("The OAuth connection belongs to a different provider.", "oauth_invalid_record")
    _configuration_from_record(value.get("configuration"))
    if (value["configuration"]["provider"] != value["provider"] or not isinstance(value.get("grant_id"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["grant_id"]) or not isinstance(value.get("scopes"), list)
            or any(not isinstance(scope, str) for scope in value["scopes"])
            or not isinstance(value.get("account_id"), str) or not isinstance(value.get("account_email"), str)):
        _fail("The OAuth connection identity is invalid.", "oauth_invalid_record")
    requested, granted = set(value["configuration"]["scopes"]), set(value["scopes"])
    if granted - requested or not (requested - {"offline_access"}).issubset(granted):
        _fail("The OAuth connection scopes differ from its explicitly requested grant.", "oauth_invalid_record")
    return value


def _credential_matches(tokens, connection):
    if (not isinstance(tokens, dict) or tokens.get("grant_id") != connection["grant_id"]
            or tokens.get("account_id") != connection["account_id"] or tokens.get("provider") != connection["provider"]
            or tokens.get("scopes") != connection["scopes"]):
        _fail("Stored credentials do not match this reviewed connection.", "oauth_grant_changed")


def access_token(store, identifier, provider=None, *, force_refresh=False):
    with store.lock("oauth-" + str(identifier).removeprefix("oauth-connection-")):
        connection = connection_metadata(store, identifier, provider)
        if connection["status"] not in {"connected", "refreshing"}:
            _fail("This OAuth connection is not active; reconnect or finish sign-in.", "oauth_auth_required")
        keychain = Keychain()
        tokens = keychain.get(identifier)
        _credential_matches(tokens, connection)
        if (connection["status"] == "connected" and not force_refresh and "pending_refresh" not in tokens
                and type(tokens.get("expires_at")) in (int, float)
                and tokens["expires_at"] > time.time() + 60):
            return _safe_token(tokens.get("access_token"))
        config = connection["configuration"]
        if connection["status"] == "refreshing":
            attempt = connection.get("refresh_attempt_id")
            updated = tokens.get("pending_refresh", tokens)
            if not isinstance(updated, dict) or not attempt or updated.get("rotation_id") != attempt:
                _fail("A refresh was interrupted before its new credential was stored. Reconnect this account; the old refresh token will not be reused.", "oauth_refresh_interrupted")
            _credential_matches(updated, connection)
            _safe_token(updated.get("access_token"))
        else:
            refresh = _safe_token(tokens.get("refresh_token"), required=False)
            if refresh is None:
                _fail("This access grant cannot refresh; reconnect through consent.", "oauth_reconnect_required")
            form = {"grant_type": "refresh_token", "client_id": config["client_id"], "refresh_token": refresh,
                    **_client_secret(config)}
            if config["provider"] == "onedrive":
                form["scope"] = " ".join(config["scopes"])
            connection.update(status="refreshing", refresh_attempt_id=secrets.token_hex(32), updated_at=utc_now())
            store.put("scans", connection)
            response = _http("POST", _token_endpoint(config), form=form)
            updated = _tokens(response, config, previous=tokens)
            updated.update(grant_id=connection["grant_id"], account_id=connection["account_id"], provider=connection["provider"],
                           rotation_id=connection["refresh_attempt_id"])
            # Preserve a rotated refresh token before a separate account request
            # can time out. A retry verifies this pending grant, never repeats
            # the previous refresh with a potentially invalidated token.
            keychain.put(identifier, dict(tokens, pending_refresh=updated))
        identity = _identity(config["provider"], updated["access_token"])
        if identity["account_id"] != connection["account_id"] or identity["account_email"] != connection["account_email"]:
            connection.update(status="reauthentication_required", updated_at=utc_now())
            store.put("scans", connection)
            _fail("A refreshed grant returned a different account identity.", "oauth_account_mismatch")
        updated.update(grant_id=connection["grant_id"], account_id=connection["account_id"], provider=connection["provider"])
        keychain.put(identifier, updated)
        connection.update(status="connected", expires_at=updated["expires_at"], refresh_available=bool(updated["refresh_token"]), updated_at=utc_now())
        store.put("scans", connection)
        return updated["access_token"]


def adapter_configuration(store, identifier, provider="gmail"):
    connection = connection_metadata(store, identifier, provider)
    if connection["status"] not in {"connected", "refreshing"}:
        _fail("Complete this account connection before using its adapter.", "oauth_auth_required")
    if provider == "gmail":
        return {"account": connection["account_email"], "scopes": connection["scopes"],
                "credential": {"source": "managed-oauth", "connection_id": identifier}}
    return {"oauth_connection": identifier, "account_id": connection["account_id"]}


def disconnect(store, identifier, provider=None):
    with store.lock("oauth-" + str(identifier).removeprefix("oauth-connection-")):
        connection = connection_metadata(store, identifier, provider)
        connection.update(status="disconnecting", updated_at=utc_now())
        store.put("scans", connection)  # Fail closed before touching credentials.
        Keychain().delete(identifier)
        connection.update(status="disconnected", expires_at=None, refresh_available=False, updated_at=utc_now())
        store.put("scans", connection)
        return {"connection_id": identifier, "local_credentials_removed": True,
                "provider_grant_revoked": False, "revocation_url": connection["revocation_url"],
                "note": "This skill can no longer use the account. Revoke the application in provider settings to remove the provider-side grant."}


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "--worker":
        raise SystemExit(2)
    try:
        worker_store = StateStore(sys.argv[2])
        deadline = _session(worker_store, sys.argv[3])["expires_at"]
        # A slow local HTTP client or native prompt cannot leave a detached
        # worker indefinitely. Public status derives expiry without trusting PID.
        signal.signal(signal.SIGALRM, lambda *_: os._exit(3))
        signal.setitimer(signal.ITIMER_REAL, max(1, deadline - time.time() + 5))
        run_worker(worker_store, sys.argv[3])
    except Exception:
        # Never print OAuth callback codes, request bodies or bearer credentials.
        raise SystemExit(1)

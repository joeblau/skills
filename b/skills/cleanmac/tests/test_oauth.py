"""OAuth fixtures: no live account, browser consent, or Keychain entries used."""
from __future__ import annotations

import base64
import copy
import hashlib
import http.client
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib import error, parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cleanmac_core import accounts_tools, oauth
from cleanmac_core.common import CleanMacError, new_id, utc_now
from cleanmac_core.state import StateStore

_NativeKeychain = oauth.Keychain


class FakeKeychain:
    values = {}

    def get(self, identifier):
        if identifier not in self.values:
            raise CleanMacError("Fixture key missing", code="oauth_auth_required")
        return copy.deepcopy(self.values[identifier])

    def put(self, identifier, value):
        self.values[identifier] = copy.deepcopy(value)

    def delete(self, identifier):
        self.values.pop(identifier, None)

    def contains(self, identifier):
        return identifier in self.values


class KeychainExistenceTests(unittest.TestCase):
    def keychain(self, status):
        # No frameworks loaded or actual entries queried: exercise the real
        # Python method against an injected Security.framework function table.
        keychain = object.__new__(_NativeKeychain)
        keychain.sec = mock.Mock()
        keychain.sec.SecItemCopyMatching.return_value = status
        keychain._constant = mock.Mock(side_effect=lambda name: name)
        keychain._query = mock.Mock(return_value=("fixture-query", ["fixture-ref"]))
        keychain._release = mock.Mock()
        return keychain

    def test_exists_without_return_data_result_or_authentication_ui(self):
        keychain = self.keychain(0)
        identifier = "oauth-connection-" + "0" * 32
        self.assertIs(keychain.contains(identifier), True)
        keychain._query.assert_called_once_with(identifier, {
            "kSecMatchLimit": "kSecMatchLimitOne", "kSecUseAuthenticationUI": "kSecUseAuthenticationUIFail"})
        keychain.sec.SecItemCopyMatching.assert_called_once_with("fixture-query", None)
        keychain._release.assert_called_once_with(["fixture-ref"])
        keychain.sec.SecItemAdd.assert_not_called()
        keychain.sec.SecItemUpdate.assert_not_called()
        keychain.sec.SecItemDelete.assert_not_called()

    def test_only_not_found_proves_absence(self):
        keychain = self.keychain(-25300)
        self.assertIs(keychain.contains("oauth-connection-" + "0" * 32), False)
        keychain._release.assert_called_once_with(["fixture-ref"])

    def test_denial_or_invalid_query_is_unknown_and_preserves_resources(self):
        for status in (-25308, -25293, -50, -25291):
            with self.subTest(status=status):
                keychain = self.keychain(status)
                with self.assertRaises(CleanMacError) as raised:
                    keychain.contains("oauth-connection-" + "0" * 32)
                self.assertEqual(raised.exception.code, "oauth_keychain_denied")
                self.assertEqual(raised.exception.details["os_status"], status)
                keychain._release.assert_called_once_with(["fixture-ref"])

    def test_missing_noninteractive_flag_fails_before_query(self):
        keychain = self.keychain(0)
        keychain._constant.side_effect = ValueError("fixture symbol missing")
        with self.assertRaises(CleanMacError) as raised:
            keychain.contains("oauth-connection-" + "0" * 32)
        self.assertEqual(raised.exception.code, "oauth_keychain_unavailable")
        keychain.sec.SecItemCopyMatching.assert_not_called()


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp.name).resolve() / "state")
        FakeKeychain.values = {}
        self.keychain_patch = mock.patch.object(oauth, "Keychain", FakeKeychain)
        self.keychain_patch.start()
        self.config = oauth.configuration({"provider": "gmail", "client_id": "fixture.apps.googleusercontent.com", "account": "person@example.com"})

    def tearDown(self):
        self.keychain_patch.stop()
        self.temp.cleanup()

    def assert_error(self, code, function, *args, **kwargs):
        with self.assertRaises(CleanMacError) as result:
            function(*args, **kwargs)
        self.assertEqual(result.exception.code, code)

    def session(self, config=None):
        record = {"schema_version": 1, "record_type": "oauth_session", "tool": "accounts",
                  "id": new_id("oauth-session"), "created_at": utc_now(), "status": "starting",
                  "configuration": config or self.config, "expires_at": time.time() + 100,
                  "connection_id": new_id("oauth-connection"), "authorization_url": None}
        return self.store.put("scans", record)

    def response(self, **changes):
        return {"access_token": "fixture-access-secret", "refresh_token": "fixture-refresh-secret",
                "expires_in": 3600, "token_type": "Bearer", "scope": " ".join(self.config["scopes"]), **changes}

    def connect(self):
        session = self.session()
        with mock.patch.object(oauth, "_loopback_code", return_value=self.response()), \
             mock.patch.object(oauth, "_identity", return_value={"account_id": "person@example.com", "account_email": "person@example.com"}):
            oauth.run_worker(self.store, session["id"])
        return oauth.connection_metadata(self.store, session["connection_id"])

    def test_pkce_uses_unpadded_s256_and_random_verifiers(self):
        verifier, challenge = oauth.pkce()
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        self.assertEqual(challenge, expected)
        self.assertTrue(43 <= len(verifier) <= 128)
        self.assertNotEqual(verifier, oauth.pkce()[0])

    def test_configuration_rejects_raw_credentials_and_custom_endpoints(self):
        for extra in ({"token": "secret"}, {"client_secret": "secret"}, {"token_endpoint": "https://evil.test"}, {"scope": "all"}):
            self.assert_error("oauth_invalid_configuration", oauth.configuration,
                              {"provider": "gmail", "client_id": "a.apps.googleusercontent.com", **extra})
        for timeout in (True, 29, 901):
            self.assert_error("oauth_invalid_configuration", oauth.configuration,
                              {"provider": "dropbox", "client_id": "fixture", "timeout": timeout})
        self.assert_error("oauth_invalid_configuration", oauth.configuration,
                          {"provider": "onedrive", "client_id": "fixture", "tenant": "common/../../evil"})

    def test_readonly_default_and_modify_are_distinct_grants(self):
        self.assertEqual(self.config["grant"], "readonly")
        altered = oauth.configuration({"provider": "gmail", "client_id": self.config["client_id"], "grant": "modify"})
        self.assertNotEqual(altered["scopes"], self.config["scopes"])
        self.assertEqual(altered["scopes"], ["https://www.googleapis.com/auth/gmail.modify"])

    def test_authorization_url_has_pkce_state_and_exact_scopes(self):
        url = oauth.authorization_url(self.config, "http://127.0.0.1:54321/callback", "nonce", "challenge")
        parsed = parse.urlsplit(url)
        params = parse.parse_qs(parsed.query)
        self.assertEqual(parsed.hostname, "accounts.google.com")
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertEqual(params["state"], ["nonce"])
        self.assertEqual(params["scope"], self.config["scopes"])
        self.assertNotIn("code_verifier", params)
        self.assertNotIn("access_token", params)
        self.assertEqual(params["access_type"], ["offline"])

    def test_dropbox_fixed_registered_redirect_and_pkce(self):
        config = oauth.configuration({"provider": "dropbox", "client_id": "fixture"})
        self.assertEqual(config["port"], 53682)
        params = parse.parse_qs(parse.urlsplit(oauth.authorization_url(config, "http://127.0.0.1:53682/callback", "nonce", "challenge")).query)
        self.assertEqual(params["token_access_type"], ["offline"])
        self.assertEqual(params["require_role"], ["personal"])
        self.assert_error("oauth_invalid_configuration", oauth.configuration,
                          {"provider": "dropbox", "client_id": "fixture", "port": 0})

    def test_callback_rejects_mismatch_duplicates_other_host_and_paths(self):
        for callback in ("/callback?state=wrong&code=secret", "/callback?state=ok&state=ok&code=secret",
                         "http://evil.test/callback?state=ok&code=secret", "/other?state=ok&code=secret",
                         "/callback?state=ok&code=", "/callback?state=ok&code=a#fragment"):
            self.assert_error("oauth_callback_rejected", oauth.parse_callback, callback, "ok")
        self.assertEqual(oauth.parse_callback("/callback?state=ok&error=access_denied", "ok"), {"denied": True})
        self.assertEqual(oauth.parse_callback("/callback?state=ok&code=secret-code", "ok"), {"code": "secret-code"})

    def test_tokens_reject_missing_scope_and_unsupported_token_type(self):
        for response, expected in ((self.response(scope="openid"), "oauth_scope_denied"),
                                   (self.response(token_type="DPoP"), "oauth_invalid_response"),
                                   (self.response(expires_in=True), "oauth_invalid_response"),
                                   (self.response(access_token="token\nsecret"), "oauth_invalid_response")):
            self.assert_error(expected, oauth._tokens, response, self.config)

    def test_worker_stores_only_public_identity_and_references_in_state(self):
        connection = self.connect()
        self.assertEqual(connection["status"], "connected")
        self.assertIn(connection["id"], FakeKeychain.values)
        all_state = json.dumps(self.store.list("scans"))
        self.assertNotIn("fixture-access-secret", all_state)
        self.assertNotIn("fixture-refresh-secret", all_state)
        self.assertNotIn("code_verifier", all_state)
        self.assertEqual(oauth.adapter_configuration(self.store, connection["id"]), {
            "account": "person@example.com", "scopes": self.config["scopes"],
            "credential": {"source": "managed-oauth", "connection_id": connection["id"]}})

    def test_worker_wrong_account_does_not_save_credentials(self):
        session = self.session()
        with mock.patch.object(oauth, "_loopback_code", return_value=self.response()), \
             mock.patch.object(oauth, "_identity", return_value={"account_id": "other@example.com", "account_email": "other@example.com"}):
            oauth.run_worker(self.store, session["id"])
        self.assertEqual(FakeKeychain.values, {})
        status = oauth.session_status(self.store, session["id"])
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error"]["code"], "oauth_account_mismatch")

    def test_cancelled_session_cannot_be_reactivated_by_worker_setup(self):
        session = self.session()
        oauth.cancel_session(self.store, session["id"])
        updated = oauth._session_update(self.store, session["id"], status="waiting_for_consent", authorization_url="https://accounts.google.com/")
        self.assertEqual(updated["status"], "cancelled")
        callback = mock.Mock()
        with mock.patch.object(oauth, "_loopback_code", callback):
            oauth.run_worker(self.store, session["id"])
        callback.assert_not_called()
        self.assertEqual(FakeKeychain.values, {})

    def test_expired_session_status_clears_public_consent_url(self):
        session = self.session()
        session.update(status="waiting_for_consent", expires_at=time.time() - 1, authorization_url="https://accounts.google.com/example")
        self.store.put("scans", session)
        current = oauth.session_status(self.store, session["id"])
        self.assertEqual(current["status"], "expired")
        self.assertIsNone(current["authorization_url"])

    def test_start_uses_isolated_bounded_worker_without_secret_arguments(self):
        with mock.patch.object(oauth.subprocess, "Popen") as launch, mock.patch.object(oauth.time, "sleep"):
            result = oauth.start_connection(self.store, {"provider": "gmail", "client_id": self.config["client_id"]})
        self.assertEqual(result["status"], "starting")
        args, kwargs = launch.call_args
        self.assertIn("-I", args[0])
        self.assertIn("--worker", args[0])
        self.assertNotIn("token", repr(args))
        self.assertTrue(kwargs["start_new_session"])
        self.assertLessEqual(result["expires_at"] - time.time(), 300)

    def test_access_uses_valid_keychain_token_without_refresh(self):
        connection = self.connect()
        with mock.patch.object(oauth, "_http") as request:
            token = oauth.access_token(self.store, connection["id"], provider="gmail")
        self.assertEqual(token, "fixture-access-secret")
        request.assert_not_called()
        self.assert_error("oauth_invalid_record", oauth.access_token, self.store, connection["id"], provider="dropbox")

    def test_refresh_preserves_grant_and_handles_refresh_token_rotation(self):
        connection = self.connect()
        response = self.response(access_token="new-access-secret", refresh_token="new-refresh-secret")
        with mock.patch.object(oauth, "_http", return_value=response) as request, \
             mock.patch.object(oauth, "_identity", return_value={"account_id": connection["account_id"], "account_email": connection["account_email"]}):
            self.assertEqual(oauth.access_token(self.store, connection["id"], force_refresh=True), "new-access-secret")
        self.assertEqual(request.call_args.kwargs["form"]["refresh_token"], "fixture-refresh-secret")
        self.assertEqual(FakeKeychain.values[connection["id"]]["refresh_token"], "new-refresh-secret")
        self.assertEqual(oauth.connection_metadata(self.store, connection["id"])["grant_id"], connection["grant_id"])
        self.assertNotIn("new-refresh-secret", json.dumps(self.store.list("scans")))

    def test_refresh_timeout_keeps_rotated_secret_and_retry_only_verifies_identity(self):
        connection = self.connect()
        with mock.patch.object(oauth, "_http", return_value=self.response(access_token="new-access", refresh_token="new-refresh")), \
             mock.patch.object(oauth, "_identity", side_effect=CleanMacError("Fixture offline", code="oauth_network_error")):
            self.assert_error("oauth_network_error", oauth.access_token, self.store, connection["id"], force_refresh=True)
        self.assertEqual(FakeKeychain.values[connection["id"]]["pending_refresh"]["refresh_token"], "new-refresh")
        with mock.patch.object(oauth, "_http") as request, \
             mock.patch.object(oauth, "_identity", return_value={"account_id": connection["account_id"], "account_email": connection["account_email"]}):
            self.assertEqual(oauth.access_token(self.store, connection["id"]), "new-access")
        request.assert_not_called()
        self.assertNotIn("pending_refresh", FakeKeychain.values[connection["id"]])

    def test_refresh_scope_upgrade_is_not_silently_accepted(self):
        connection = self.connect()
        response = self.response(scope=" ".join(self.config["scopes"] + ["https://www.googleapis.com/auth/gmail.modify"]))
        with mock.patch.object(oauth, "_http", return_value=response):
            self.assert_error("oauth_grant_changed", oauth.access_token, self.store, connection["id"], force_refresh=True)
        self.assertEqual(oauth.connection_metadata(self.store, connection["id"])["scopes"], self.config["scopes"])

    def test_refresh_changed_account_disables_connection(self):
        connection = self.connect()
        with mock.patch.object(oauth, "_http", return_value=self.response()), \
             mock.patch.object(oauth, "_identity", return_value={"account_id": "other", "account_email": "other@example.com"}):
            self.assert_error("oauth_account_mismatch", oauth.access_token, self.store, connection["id"], force_refresh=True)
        self.assertEqual(oauth.connection_metadata(self.store, connection["id"])["status"], "reauthentication_required")
        self.assert_error("oauth_auth_required", oauth.access_token, self.store, connection["id"])

    def test_keychain_grant_substitution_is_rejected(self):
        connection = self.connect()
        FakeKeychain.values[connection["id"]]["grant_id"] = "0" * 64
        self.assert_error("oauth_grant_changed", oauth.access_token, self.store, connection["id"])

    def test_disconnect_is_exact_approved_action_and_removes_local_credentials(self):
        connection = self.connect()
        plan = accounts_tools.dispatch("accounts", "disconnect-plan", {"record_id": connection["id"]}, store=self.store)
        self.assert_error("approval_required", accounts_tools.dispatch, "accounts", "apply",
                          {"record_id": plan["id"], "approve": "0" * 64}, store=self.store)
        self.assertIn(connection["id"], FakeKeychain.values)
        receipt = accounts_tools.dispatch("accounts", "apply", {"record_id": plan["id"], "approve": plan["digest"]}, store=self.store)
        self.assertEqual(receipt["status"], "completed")
        self.assertFalse(receipt["result"]["provider_grant_revoked"])
        self.assertNotIn(connection["id"], FakeKeychain.values)
        self.assertEqual(oauth.connection_metadata(self.store, connection["id"])["status"], "disconnected")
        self.assert_error("oauth_auth_required", oauth.access_token, self.store, connection["id"])

    def test_disconnect_keychain_failure_blocks_further_account_use(self):
        connection = self.connect()
        with mock.patch.object(FakeKeychain, "delete", side_effect=CleanMacError("Fixture Keychain locked", code="oauth_keychain_denied")):
            self.assert_error("oauth_keychain_denied", oauth.disconnect, self.store, connection["id"])
        self.assertEqual(oauth.connection_metadata(self.store, connection["id"])["status"], "disconnecting")
        self.assert_error("oauth_auth_required", oauth.access_token, self.store, connection["id"])

    def test_transport_rejects_credential_redirects_and_nonprovider_hosts(self):
        self.assert_error("oauth_invalid_endpoint", oauth._http, "POST", "https://evil.example/token", form={})
        self.assert_error("oauth_redirect_rejected", oauth._NoRedirect().redirect_request,
                          None, None, 302, "redirect", {}, "https://evil.example/token")

    def test_provider_error_does_not_include_error_description_or_token(self):
        opener = mock.Mock()
        payload = io.BytesIO(b'{"error":"invalid_grant","error_description":"access-secret-must-not-leak"}')
        opener.open.side_effect = error.HTTPError("https://oauth2.googleapis.com/token", 400, "failed", {}, payload)
        with mock.patch.object(oauth.request, "build_opener", return_value=opener):
            with self.assertRaises(oauth.OAuthResponseError) as caught:
                oauth._http("POST", "https://oauth2.googleapis.com/token", form={"refresh_token": "private-secret"})
        self.assertEqual(caught.exception.oauth_error, "invalid_grant")
        self.assertNotIn("must-not-leak", str(caught.exception))
        self.assertNotIn("private-secret", str(caught.exception))

    def test_account_inspect_lists_requirements_without_token_access(self):
        self.connect()
        with mock.patch.object(FakeKeychain, "get", side_effect=AssertionError("inspect must not fetch token")):
            result = accounts_tools.dispatch("accounts", "inspect", {}, store=self.store)
        self.assertEqual(len(result["connections"]), 1)
        self.assertEqual(len(result["providers"]), 4)

    def test_accounts_dispatch_preserves_cli_configuration(self):
        configuration = {"provider": "gmail", "client_id": "fixture.apps.googleusercontent.com"}
        with mock.patch.object(oauth, "start_connection", return_value={"status": "starting"}) as start:
            result = accounts_tools.dispatch("accounts", "connect", {"configuration": configuration}, store=self.store)
        start.assert_called_once_with(self.store, configuration)
        self.assertEqual(result["status"], "starting")
        self.assertIn("client_id", accounts_tools.TOOL_SPECS[0]["configuration"])

    def test_loopback_listener_starts_without_hostname_resolution(self):
        session = self.session()
        with mock.patch("http.server.socket.getfqdn", side_effect=AssertionError("Loopback startup must not resolve hostnames")), \
             mock.patch.object(oauth, "_is_pending", return_value=False):
            self.assertIsNone(oauth._loopback_code(self.store, session))
        current = self.store.get("scans", session["id"])
        self.assertEqual(current["status"], "waiting_for_consent")
        redirect = parse.urlsplit(current["redirect_uri"])
        self.assertEqual(redirect.hostname, "127.0.0.1")
        self.assertGreater(redirect.port, 0)

    def test_loopback_listener_validates_state_and_closes_after_callback(self):
        session = self.session()
        outcomes = []
        with mock.patch.object(oauth, "_http", return_value=self.response()) as provider:
            worker = threading.Thread(target=lambda: outcomes.append(oauth._loopback_code(self.store, session)))
            worker.start()
            deadline = time.monotonic() + 3
            current = session
            while time.monotonic() < deadline:
                current = self.store.get("scans", session["id"])
                if current.get("status") == "waiting_for_consent":
                    break
                time.sleep(0.01)
            self.assertEqual(current["status"], "waiting_for_consent")
            params = parse.parse_qs(parse.urlsplit(current["authorization_url"]).query)
            callback = parse.urlsplit(params["redirect_uri"][0])
            client = http.client.HTTPConnection(callback.hostname, callback.port, timeout=2)
            client.request("GET", "/callback?state=wrong&code=not-valid")
            rejected = client.getresponse()
            self.assertEqual(rejected.status, 400)
            rejected.read()
            client.close()
            client = http.client.HTTPConnection(callback.hostname, callback.port, timeout=2)
            client.request("GET", "/callback?" + parse.urlencode({"state": params["state"][0], "code": "fixture-code-secret"}))
            accepted = client.getresponse()
            self.assertEqual(accepted.status, 200)
            self.assertNotIn(b"fixture-code-secret", accepted.read())
            client.close()
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(outcomes[0]["access_token"], "fixture-access-secret")
        form = provider.call_args.kwargs["form"]
        self.assertEqual(form["code"], "fixture-code-secret")
        self.assertTrue(43 <= len(form["code_verifier"]) <= 128)
        saved = json.dumps(self.store.list("scans"))
        self.assertNotIn("fixture-code-secret", saved)
        self.assertNotIn(form["code_verifier"], saved)
        self.assertNotIn("fixture-access-secret", saved)

    def test_microsoft_device_flow_obeys_pending_and_keeps_device_secret_in_memory(self):
        config = oauth.configuration({"provider": "onedrive", "client_id": "fixture-public-client"})
        session = self.session(config)
        device = {"device_code": "fixture-device-secret", "user_code": "ABC-DEF", "verification_uri": "https://microsoft.com/devicelogin",
                  "expires_in": 300, "interval": 1}
        response = self.response(scope="Files.Read User.Read")
        with mock.patch.object(oauth, "_http", side_effect=[device, oauth.OAuthResponseError(400, "authorization_pending"), response]) as provider, \
             mock.patch.object(oauth.time, "monotonic", side_effect=range(0, 10000, 10)), \
             mock.patch.object(oauth.time, "sleep"):
            result = oauth._device_code(self.store, session)
        self.assertEqual(result, response)
        self.assertEqual(provider.call_count, 3)
        self.assertEqual(provider.call_args.kwargs["form"]["device_code"], "fixture-device-secret")
        saved = self.store.get("scans", session["id"])
        self.assertEqual(saved["user_code"], "ABC-DEF")
        self.assertNotIn("fixture-device-secret", json.dumps(saved))


if __name__ == "__main__":
    unittest.main()

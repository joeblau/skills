"""Independent failure/race probes; credentials and provider transport are fakes."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, Mock
from urllib import request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cleanmac_core import oauth, accounts_tools
from cleanmac_core.common import CleanMacError, new_id, utc_now
from cleanmac_core.state import StateStore


class MemoryKeychain:
    values = {}
    def get(self, key): return copy.deepcopy(self.values[key])
    def put(self, key, value): self.values[key] = copy.deepcopy(value)
    def delete(self, key): self.values.pop(key, None)


class OAuthForwardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cm-oauth-forward-", dir="/tmp")
        self.store = StateStore(Path(self.temp.name).resolve() / "state")
        self.config = oauth.configuration({"provider": "gmail", "client_id": "review.apps.googleusercontent.com"})
        self.connection = {"schema_version": 1, "record_type": "oauth_connection", "tool": "accounts", "id": new_id("oauth-connection"),
            "created_at": utc_now(), "updated_at": utc_now(), "provider": "gmail", "configuration": self.config, "status": "connected",
            "grant_id": "a" * 64, "scopes": self.config["scopes"], "account_id": "fixture@example.test", "account_email": "fixture@example.test",
            "expires_at": time.time() + 3600, "refresh_available": True, "revocation_url": oauth.REVOCATION_URLS["gmail"]}
        self.store.put("scans", self.connection)
        self.tokens = {"access_token": "fixture-old-access", "refresh_token": "fixture-old-refresh", "expires_at": time.time() + 3600,
            "grant_id": self.connection["grant_id"], "scopes": self.config["scopes"], "account_id": self.connection["account_id"], "provider": "gmail"}
        MemoryKeychain.values = {self.connection["id"]: copy.deepcopy(self.tokens)}
        self.keychain = patch.object(oauth, "Keychain", MemoryKeychain); self.keychain.start()
        self.response = {"access_token": "fixture-new-access", "refresh_token": "fixture-new-refresh", "expires_in": 3600,
                         "token_type": "Bearer", "scope": " ".join(self.config["scopes"])}
        self.identity = {key: self.connection[key] for key in ("account_id", "account_email")}

    def tearDown(self):
        self.keychain.stop(); self.temp.cleanup()

    def session(self, status="starting"):
        return self.store.put("scans", {"schema_version": 1, "record_type": "oauth_session", "tool": "accounts", "id": new_id("oauth-session"),
            "created_at": utc_now(), "configuration": self.config, "status": status, "expires_at": time.time() + 100,
            "connection_id": self.connection["id"], "authorization_url": None})

    def test_rotated_refresh_lost_to_keychain_failure_is_never_reused(self):
        with patch.object(oauth, "_http", return_value=self.response) as exchange, \
             patch.object(MemoryKeychain, "put", side_effect=CleanMacError("Fixture authorization denied", code="oauth_keychain_denied")):
            with self.assertRaises(CleanMacError): oauth.access_token(self.store, self.connection["id"], force_refresh=True)
        self.assertEqual(exchange.call_count, 1)
        with patch.object(oauth, "_http", side_effect=AssertionError("old refresh must not be resent")), self.assertRaises(CleanMacError) as caught:
            oauth.access_token(self.store, self.connection["id"], force_refresh=True)
        self.assertEqual(caught.exception.code, "oauth_refresh_interrupted")
        self.assertNotIn("fixture-new-refresh", json.dumps(self.store.list("scans")))

    def test_ambiguous_refresh_transport_never_retries_old_grant(self):
        with patch.object(oauth, "_http", side_effect=CleanMacError("Fixture timeout", code="oauth_network_error")):
            with self.assertRaises(CleanMacError): oauth.access_token(self.store, self.connection["id"], force_refresh=True)
        with patch.object(oauth, "_http", side_effect=AssertionError("ambiguous refresh retried")), self.assertRaises(CleanMacError) as caught:
            oauth.access_token(self.store, self.connection["id"])
        self.assertEqual(caught.exception.code, "oauth_refresh_interrupted")

    def test_rotation_saved_before_final_state_failure_is_recovered_without_refresh(self):
        original_put = self.store.put
        def fail_final(kind, record):
            if kind == "scans" and record.get("record_type") == "oauth_connection" and record.get("status") == "connected":
                raise CleanMacError("Fixture disk unavailable", code="state_io_error")
            return original_put(kind, record)
        with patch.object(oauth, "_http", return_value=self.response), patch.object(oauth, "_identity", return_value=self.identity), patch.object(self.store, "put", side_effect=fail_final):
            with self.assertRaises(CleanMacError): oauth.access_token(self.store, self.connection["id"], force_refresh=True)
        self.assertEqual(MemoryKeychain.values[self.connection["id"]]["refresh_token"], "fixture-new-refresh")
        with patch.object(oauth, "_http", side_effect=AssertionError("rotation must not repeat")), patch.object(oauth, "_identity", return_value=self.identity):
            self.assertEqual(oauth.access_token(self.store, self.connection["id"]), "fixture-new-access")
        self.assertEqual(oauth.connection_metadata(self.store, self.connection["id"])["status"], "connected")

    def test_readonly_initial_consent_cannot_inherit_modify_scope(self):
        response = dict(self.response, scope=self.response["scope"] + " https://www.googleapis.com/auth/gmail.modify")
        with self.assertRaises(CleanMacError): oauth._tokens(response, self.config)
        changed = dict(self.connection, scopes=self.config["scopes"] + ["https://www.googleapis.com/auth/gmail.modify"])
        self.store.put("scans", changed)
        with self.assertRaises(CleanMacError): oauth.adapter_configuration(self.store, self.connection["id"])

    def test_stale_cancel_expiry_cannot_overwrite_connected_session(self):
        session = self.session("connected")
        for stale_status in ("cancelled", "expired", "failed", "waiting_for_consent"):
            self.assertEqual(oauth._session_update(self.store, session["id"], status=stale_status)["status"], "connected")
        self.assertEqual(oauth.access_token(self.store, self.connection["id"]), "fixture-old-access")

    def test_non_ascii_callback_state_rejected_with_sanitized_error(self):
        with self.assertRaises(CleanMacError) as caught: oauth.parse_callback("/callback?state=%C3%A9&code=private-code", "expected-state")
        self.assertEqual(caught.exception.code, "oauth_callback_rejected")
        self.assertNotIn("private-code", str(caught.exception))

    def test_device_consent_url_cannot_use_alternate_ports(self):
        config = oauth.configuration({"provider": "onedrive", "client_id": "fixture-public-id"})
        session = self.session(); session["configuration"] = config; self.store.put("scans", session)
        for url in ("https://microsoft.com:444/devicelogin", "https://microsoft.com:99999/devicelogin"):
            with patch.object(oauth, "_http", return_value={"device_code": "private-device-code", "user_code": "ABC-DEF", "verification_uri": url, "interval": 1, "expires_in": 300}), self.assertRaises(CleanMacError):
                oauth._device_code(self.store, session)

    def test_worker_environment_only_contains_selected_secret_reference(self):
        additions = {"PYTHONPATH": "/tmp/untrusted", "DYLD_INSERT_LIBRARIES": "/tmp/untrusted.dylib", "SSLKEYLOGFILE": "/tmp/no-key-log",
                     "CLEANMAC_OTHER_SECRET": "unrelated-secret", "CLEANMAC_REVIEW_CLIENT": "selected-client-secret"}
        with patch.dict(os.environ, additions), patch.object(oauth.subprocess, "Popen") as process, patch.object(oauth.time, "sleep"):
            oauth.start_connection(self.store, {"provider": "gmail", "client_id": self.config["client_id"], "client_secret_env": "CLEANMAC_REVIEW_CLIENT"})
        env = process.call_args.kwargs["env"]
        self.assertEqual(env["CLEANMAC_REVIEW_CLIENT"], "selected-client-secret")
        for key in additions:
            if key != "CLEANMAC_REVIEW_CLIENT": self.assertNotIn(key, env)
        self.assertNotIn("selected-client-secret", repr(process.call_args.args))

    def test_transport_disables_proxy_and_tls_key_logging_without_network(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, maximum): return b'{}'
        opener = Mock(); opener.open.return_value = Response()
        destination = Path(self.temp.name) / "must-not-create-tls-keys"
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://untrusted.invalid:1234", "SSLKEYLOGFILE": str(destination)}), patch.object(oauth.request, "build_opener", return_value=opener) as build:
            self.assertEqual(oauth._http("GET", "https://gmail.googleapis.com/gmail/v1/users/me/profile", token="private-token"), {})
        handlers = build.call_args.args
        self.assertEqual(next(item for item in handlers if isinstance(item, request.ProxyHandler)).proxies, {})
        tls = next(item for item in handlers if isinstance(item, request.HTTPSHandler))._context
        self.assertIsNone(tls.keylog_filename)
        self.assertTrue(tls.check_hostname)
        self.assertFalse(destination.exists())

    def test_disconnect_denial_blocks_existing_grant_and_stale_plan(self):
        plan = accounts_tools.dispatch("accounts", "disconnect-plan", {"record_id": self.connection["id"]}, store=self.store)
        changed = dict(self.connection, grant_id="b" * 64); self.store.put("scans", changed)
        with self.assertRaises(CleanMacError): accounts_tools.dispatch("accounts", "apply", {"record_id": plan["id"], "approve": plan["digest"]}, store=self.store)
        self.assertIn(self.connection["id"], MemoryKeychain.values)
        self.store.put("scans", self.connection)
        with patch.object(MemoryKeychain, "delete", side_effect=CleanMacError("Fixture denied", code="oauth_keychain_denied")):
            with self.assertRaises(CleanMacError): oauth.disconnect(self.store, self.connection["id"])
        with patch.object(MemoryKeychain, "get", side_effect=AssertionError("inactive grant must not read credentials")), self.assertRaises(CleanMacError):
            oauth.access_token(self.store, self.connection["id"])


if __name__ == "__main__": unittest.main()

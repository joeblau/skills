"""Account setup, status and credential lifecycle for cloud and Gmail tools."""
from __future__ import annotations

from . import oauth
from .actions import create_action_plan, execute_action_plan, checkpoint_action
from .common import CleanMacError

TOOL_SPECS = [{
    "name": "accounts", "title": "Connected accounts", "feature_ids": ["CM-054", "CM-056", "CM-057", "CM-058", "CM-063"],
    "actions": ["inspect", "connect", "status", "cancel", "refresh", "disconnect-plan", "apply"],
    "approval_actions": ["apply"], "status": "conditional",
    "requirements": ["macOS Keychain", "Your registered provider public client/app ID", "User browser consent and provider-enabled scopes"],
    "configuration": {"provider": "gmail, google-drive, onedrive, or dropbox", "client_id": "Public registered desktop/public-client application ID",
                      "grant": "readonly (default) or modify; changes require a new consent connection", "account": "Optional expected account email",
                      "timeout": "Consent window in seconds, 30–900 (default 300)",
                      "port": "Dropbox fixed registered callback port (default 53682); Google random by default",
                      "tenant": "Microsoft consumers (default), common, organizations, or tenant GUID",
                      "client_secret_env": "Optional CLEANMAC_ environment reference for a Google desktop client secret"},
    "examples": ["tool accounts --action inspect",
                 "tool accounts --action connect --config '{\"provider\":\"gmail\",\"client_id\":\"YOUR_CLIENT.apps.googleusercontent.com\",\"grant\":\"readonly\",\"account\":\"you@example.com\"}'",
                 "tool accounts --action status --record OAUTH_SESSION_ID"],
    "sources": oauth.SOURCES,
}]


def _record(options):
    identifier = options.get("record_id")
    if not isinstance(identifier, str) or not identifier:
        raise CleanMacError("Choose the exact account connection or consent session ID.", code="oauth_invalid_record")
    return identifier


def _inspect(store):
    connections = []
    for value in store.list("scans"):
        if value.get("record_type") == "oauth_connection" and value.get("tool") == "accounts":
            connections.append(oauth.connection_metadata(store, value["id"]))
    return {"tool": "accounts", "status": "complete", "connections": connections,
            "providers": [
                {"provider": "gmail", "setup": "Register a Google Desktop app OAuth client, configure consent/test users and enable Gmail API. A published shared client requires Google's applicable verification.",
                 "registration_url": "https://console.cloud.google.com/auth/clients", "scopes": oauth.SCOPES["gmail"]},
                {"provider": "google-drive", "setup": "Register a Google Desktop app OAuth client and enable Drive API. Cleanup requests drive scope in a separate modify connection.",
                 "registration_url": "https://console.cloud.google.com/auth/clients", "scopes": oauth.SCOPES["google-drive"]},
                {"provider": "onedrive", "setup": "Register a public client application, enable mobile/desktop public client/device-code flows, and select the supported account types. Tenant policy may require administrator consent.",
                 "registration_url": "https://entra.microsoft.com/", "scopes": oauth.SCOPES["onedrive"]},
                {"provider": "dropbox", "setup": "Register a scoped public app, enable the displayed permissions, and register exactly http://127.0.0.1:53682/callback (or your selected fixed port). App Folder access covers only that app folder; choose Full Dropbox only when required for the intended scope.",
                 "registration_url": "https://www.dropbox.com/developers/apps", "scopes": oauth.SCOPES["dropbox"]},
            ],
            "credential_storage": "Access and refresh tokens stay in macOS Keychain. JSON state stores account identity, scope, expiry and credential references only.",
            "consent": "Connect starts one explicitly requested sign-in worker for at most the configured window. Open its returned URL in a system browser and handle consent yourself; the skill never clicks Allow.",
            "limitations": ["No shared pre-registered OAuth application is bundled; developer registration and provider verification cannot be completed by this installer.",
                            "Disconnect removes this skill's local credentials; provider-side grant revocation is a separate account-settings handoff.",
                            "Provider policies, consent and Keychain prompts are external prerequisites; no live account integration is claimed by fixture tests."],
            "sources": oauth.SOURCES}


def dispatch(name, action, options, *, store, cancel=None, progress=None):
    if name != "accounts":
        raise CleanMacError("Unknown account tool.", code="unsupported_tool")
    config = options.get("configuration", {})
    if not isinstance(config, dict):
        raise CleanMacError("Account configuration must be an object.", code="oauth_invalid_configuration")
    if action == "inspect":
        return _inspect(store)
    if action == "connect":
        if cancel and cancel():
            return {"tool": name, "status": "cancelled", "started": False}
        return oauth.start_connection(store, config)
    if action == "status":
        identifier = _record(options)
        return oauth.session_status(store, identifier) if identifier.startswith("oauth-session-") else oauth.connection_metadata(store, identifier)
    if action == "cancel":
        return oauth.cancel_session(store, _record(options))
    if action == "refresh":
        identifier = _record(options)
        oauth.access_token(store, identifier, force_refresh=True)
        return oauth.connection_metadata(store, identifier)
    if action == "disconnect-plan":
        connection = oauth.connection_metadata(store, _record(options))
        return create_action_plan(store, name, "disconnect", {
            "connection_id": connection["id"], "provider": connection["provider"],
            "account_id": connection["account_id"], "grant_id": connection["grant_id"],
        }, {"action": "Remove this skill's stored account credentials", "provider": connection["provider"],
            "account": connection["account_email"], "connection_id": connection["id"],
            "effect": "Stops this skill's account access. Provider grant revocation remains an explicit account-settings action.",
            "recovery": "Reconnecting requires provider consent again."})
    if action == "apply":
        def validate(plan):
            payload = plan["payload"]
            connection = oauth.connection_metadata(store, payload["connection_id"], payload["provider"])
            if connection["account_id"] != payload["account_id"] or connection["grant_id"] != payload["grant_id"]:
                raise CleanMacError("The selected account grant changed; prepare a new disconnect plan.", code="oauth_grant_changed")

        def execute(plan):
            checkpoint_action({"connection_id": plan["payload"]["connection_id"], "phase": "disconnecting"})
            result = oauth.disconnect(store, plan["payload"]["connection_id"], plan["payload"]["provider"])
            checkpoint_action(result)
            return result

        return execute_action_plan(store, _record(options), options.get("approve"), name, "disconnect", validate, execute,
                                   cancel=cancel, progress=progress)
    raise CleanMacError("Choose inspect, connect, status, cancel, refresh, disconnect-plan, or apply.", code="unsupported_account_action")

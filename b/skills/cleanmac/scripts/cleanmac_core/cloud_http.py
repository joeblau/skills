"""Bounded metadata-only HTTPS transport for explicitly connected cloud accounts."""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

from .common import CleanMacError


HOSTS = {"google-drive": "www.googleapis.com", "onedrive": "graph.microsoft.com", "dropbox": "api.dropboxapi.com"}
PREFIXES = {"google-drive": "/drive/v3/", "onedrive": "/v1.0/", "dropbox": "/2/"}
MAX_RESPONSE = 8 * 1024 * 1024


class CloudHTTPError(CleanMacError):
    def __init__(self, status: int):
        self.status = status
        code = {401: "cloud_auth_required", 403: "cloud_permission_denied", 404: "cloud_item_missing",
                409: "cloud_item_changed", 412: "cloud_item_changed", 429: "cloud_rate_limited"}.get(status, "cloud_request_failed")
        super().__init__("The cloud provider rejected the request. No credential or provider response body is included.",
                         code, {"http_status": status})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CleanMacError("Cloud API redirects are not followed with credentials.", "cloud_redirect_rejected")


def validate_url(provider: str, url: str) -> None:
    try:
        parsed = urllib.parse.urlsplit(url)
        valid = (parsed.scheme == "https" and parsed.hostname == HOSTS[provider]
                 and parsed.port in (None, 443) and not parsed.username and not parsed.password
                 and not parsed.fragment and parsed.path.startswith(PREFIXES[provider]))
    except (ValueError, KeyError):
        valid = False
    if not valid:
        raise CleanMacError("Cloud metadata requests must stay on the selected provider's HTTPS API.", "cloud_invalid_endpoint")


class CloudHTTP:
    def __init__(self, provider: str, token: str):
        self.provider = provider
        self._token = token
        # No implicit proxy may receive authorization headers. HTTPS still uses
        # the system CA store. Redirects never inherit the bearer credential.
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        tls.load_default_certs()
        # Do not inherit SSLKEYLOGFILE from the invoking agent's debug session.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
                                                  urllib.request.HTTPSHandler(context=tls))

    def request(self, method: str, url: str, body=None, headers=None) -> dict:
        validate_url(self.provider, url)
        if method not in {"GET", "POST", "PATCH", "DELETE"}:
            raise CleanMacError("Unsupported cloud request method.", "cloud_invalid_request")
        extra = dict(headers or {})
        if set(extra) - {"If-Match", "Dropbox-API-Path-Root"}:
            raise CleanMacError("Unsupported cloud request header.", "cloud_invalid_request")
        extra.update({"Authorization": "Bearer " + self._token, "Accept": "application/json"})
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":"), allow_nan=False).encode()
            extra["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=extra)
        try:
            with self._opener.open(request, timeout=20) as response:
                data = response.read(MAX_RESPONSE + 1)
                if len(data) > MAX_RESPONSE:
                    raise CleanMacError("Cloud metadata response exceeded the bounded limit.", "cloud_response_limit")
                if not data:
                    return {}
                value = json.loads(data)
                if not isinstance(value, dict):
                    raise ValueError("object required")
                return value
        except urllib.error.HTTPError as error:
            error.close()
            raise CloudHTTPError(error.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise CleanMacError("Cloud request did not complete. An interrupted mutation needs reconciliation before retry.",
                                "cloud_network_error") from None
        except (ValueError, UnicodeError):
            raise CleanMacError("The cloud provider returned invalid metadata.", "cloud_invalid_response") from None

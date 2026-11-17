from pathlib import Path
import os
import ssl
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib import request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cleanmac_core import cloud_http, mail_http


class TransportPrivacyTests(unittest.TestCase):
    def test_debug_environment_cannot_enable_tls_secret_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            keylog = Path(temporary) / "tls-secrets"
            with patch.dict(os.environ, {"SSLKEYLOGFILE": str(keylog), "CLEANMAC_GMAIL_TEST": "fixture-only-token"}), patch.object(request, "build_opener") as opener:
                cloud_http.CloudHTTP("google-drive", "fixture-only-token")
                mail_http.GmailClient({"account": "fixture@example.com", "credential": {"source": "environment", "name": "CLEANMAC_GMAIL_TEST"}})
            self.assertEqual(opener.call_count, 2)
            for call in opener.call_args_list:
                handler = next(value for value in call.args if isinstance(value, request.HTTPSHandler))
                self.assertIsNone(handler._context.keylog_filename)
                self.assertTrue(handler._context.check_hostname)
                self.assertEqual(handler._context.verify_mode, ssl.CERT_REQUIRED)
                proxy = next(value for value in call.args if isinstance(value, request.ProxyHandler))
                self.assertEqual(proxy.proxies, {})
            self.assertFalse(keylog.exists())


if __name__ == "__main__":
    unittest.main()

"""Real loopback TLS protocol tests; synthetic source responses, no live backend account."""

import json
import ssl
import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from sre_suite.execution.http import JsonHttp
from sre_suite.telemetry.adapters import TelemetryAdapter, TelemetryError
from sre_suite.telemetry.contracts import Backend
from tests.test_telemetry import SCOPE, profile, query, response
from tests.test_tempo_traces import TRACE_ID, trace_profile, trace_response


class HttpsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        root = Path(cls.directory.name).resolve()
        config = root / "openssl.cnf"
        config.write_text(
            "[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
            "[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost\n"
        )
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-keyout",
                str(root / "key.pem"),
                "-out",
                str(root / "cert.pem"),
                "-config",
                str(config),
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )
        cls.ca = str(root / "cert.pem")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cls.ca, str(root / "key.pem"))

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                cls.requests.append((self.path, dict(self.headers)))
                self.send_response(cls.status)
                if cls.status == 302:
                    self.send_header("Location", "/credential-trap")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(cls.payload)))
                self.end_headers()
                try:
                    self.wfile.write(cls.payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.shutdown)

    @classmethod
    def shutdown(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        type(self).requests = []
        type(self).status = 200
        type(self).payload = b"{}"

    def client(self, *, trusted=True):
        return JsonHttp(
            f"https://localhost:{self.server.server_port}",
            "Basic c3ludGhldGljOm9ubHk=",
            headers={"X-Scope-OrgID": "synthetic-tenant"},
            local_ca_file=self.ca if trusted else None,
        )

    def test_all_three_actual_https_wire_requests(self):
        for backend in Backend:
            type(self).payload = json.dumps(response(backend)).encode()
            result = TelemetryAdapter(profile(backend), self.client()).query(query(backend))
            path, headers = self.requests[-1]
            params = parse_qs(urlsplit(path).query)
            self.assertEqual(
                params["q" if backend == Backend.TEMPO else "query"],
                [profile(backend).query(SCOPE)],
            )
            self.assertEqual(headers["Authorization"], "Basic c3ludGhldGljOm9ubHk=")
            self.assertEqual(headers["X-Scope-Orgid"], "synthetic-tenant")
            self.assertEqual(headers["Accept"], "application/json")
            self.assertEqual(result.backend, backend)

    def test_redirect_is_not_followed_with_credentials(self):
        type(self).status = 302
        with self.assertRaisesRegex(TelemetryError, "http_302"):
            TelemetryAdapter(profile(Backend.LOKI), self.client()).query(query(Backend.LOKI))
        self.assertEqual(len(self.requests), 1)

    def test_full_trace_v2_over_verified_https(self):
        type(self).payload = json.dumps(trace_response()).encode()
        request = replace(query(Backend.TEMPO), trace_id=TRACE_ID)
        result = TelemetryAdapter(trace_profile(), self.client()).query(request)
        self.assertEqual(result.full_trace.trace_id, TRACE_ID)
        endpoint = urlsplit(self.requests[-1][0])
        self.assertEqual(endpoint.path, "/api/v2/traces/" + TRACE_ID)
        self.assertEqual(parse_qs(endpoint.query)["span_pruning"], ["false"])
        self.assertNotIn("q", parse_qs(endpoint.query))

    def test_partial_full_trace_and_not_found_are_not_complete(self):
        raw = trace_response()
        raw["status"] = "PARTIAL"
        type(self).payload = json.dumps(raw).encode()
        request = replace(query(Backend.TEMPO), trace_id=TRACE_ID)
        adapter = TelemetryAdapter(trace_profile(), self.client())
        self.assertEqual(adapter.query(request).status.value, "partial")
        type(self).status = 404
        with self.assertRaisesRegex(TelemetryError, "http_404"):
            adapter.query(request)

    def test_http_failure_duplicate_json_and_byte_budget(self):
        for status, payload in (
            (401, b"private-response"),
            (429, b"private-response"),
            (200, b'{"status":"error","status":"success"}'),
            (200, b"x" * 1_500_001),
        ):
            type(self).status, type(self).payload = status, payload
            with self.assertRaises(TelemetryError) as caught:
                TelemetryAdapter(profile(Backend.PROMETHEUS), self.client()).query(query())
            self.assertNotIn("private-response", str(caught.exception))

    def test_certificate_trust_is_required(self):
        # A separate, unrelated CA must fail instead of silently disabling verification.
        client = self.client()
        import urllib.request

        from sre_suite.execution.http import NoRedirect

        client._opener = urllib.request.build_opener(NoRedirect())
        with self.assertRaisesRegex(TelemetryError, "transport_error"):
            TelemetryAdapter(profile(Backend.LOKI), client).query(query(Backend.LOKI))
        self.assertFalse(self.requests)


if __name__ == "__main__":
    unittest.main()

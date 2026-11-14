"""Real controller/runner and exporter HTTPS boundaries using only loopback credentials."""

import http.client
import json
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

from sre_suite.demo_fixture.export import flush, transports
from sre_suite.demo_fixture.server import server
from sre_suite.execution.http import JsonHttp, RemoteError
from sre_suite.execution.journal import Journal
from sre_suite.fixtures.contracts import FixtureRequest
from tests.demo_fixture import DemoFixture

AUTH = "Bearer synthetic-fixture-" + "a" * 32
METRICS = "Bearer synthetic-metrics-" + "b" * 32


class DemoHttpsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        cls.root = Path(temp.name).resolve()
        config = cls.root / "openssl.cnf"
        config.write_text(
            "[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n"
        )
        cls.cert, cls.key = cls.root / "cert.pem", cls.root / "key.pem"
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
                str(cls.key),
                "-out",
                str(cls.cert),
                "-config",
                str(config),
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )
        cls.context = ssl.create_default_context(cafile=str(cls.cert))

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.d = DemoFixture(
            Path(temp.name).resolve(),
            target={
                "base_url": "https://localhost:9443",
                "authorization_env": "SRE_FIXTURE_TARGET_AUTH",
                "ca_env": "SRE_FIXTURE_TARGET_CA_FILE",
            },
        )
        self.addCleanup(self.d.close)
        self.listener = server(self.d.controller, AUTH, METRICS, str(self.cert), str(self.key), 0)
        # Tests use an ephemeral port; the request still pins the reviewed policy origin.
        self.base = f"https://localhost:{self.listener.server_port}"
        self.thread = threading.Thread(target=self.listener.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)
        self.d.runner.target = JsonHttp(self.base, AUTH, local_ca_file=str(self.cert))

    def shutdown(self):
        self.listener.shutdown()
        self.listener.server_close()
        self.thread.join(timeout=5)

    def test_real_runner_protocol_and_separate_authenticated_scrape(self):
        self.d.runner.execute(self.d.request)
        request = Request(self.base + "/metrics", headers={"Authorization": METRICS})
        with urlopen(request, context=self.context, timeout=5) as response:
            body = response.read().decode()
        self.assertIn("sre_demo_requests_total", body)
        self.assertNotIn(AUTH, body)
        self.assertNotIn(METRICS, body)
        self.d.f.now = self.d.f.end
        artifact = self.d.runner.execute(self.d.request)
        ref = self.d.runner.publish(self.d.request, artifact)
        self.assertEqual(self.d.assessor.assess(self.d.request, ref)["outcome"], "assessed")

    def test_wrong_role_credentials_and_bad_command_do_not_execute(self):
        scenario = self.d.policy.scenarios[1]
        command = self.d.runner.command(FixtureRequest.parse(self.d.request), scenario)
        for auth in (None, "Bearer bad", METRICS):
            client = JsonHttp(self.base, auth, local_ca_file=str(self.cert))
            with self.assertRaises(RemoteError) as error:
                client.call("POST", scenario.path, command)
            self.assertEqual(error.exception.status, 401)
        command["binding"]["release"]["scope"]["generation"] = 2
        with self.assertRaises(RemoteError) as error:
            self.d.runner.target.call("POST", scenario.path, command)
        self.assertEqual(error.exception.status, 409)
        with self.d.controller.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM executions").fetchone()[0], 0)
        with self.assertRaises(RemoteError) as error:
            self.d.runner.target.call("GET", "/metrics")
        self.assertEqual(error.exception.status, 401)

    def test_duplicate_json_oversized_and_ambiguous_framing_are_rejected(self):
        for payload, headers in (
            (b'{"x":1,"x":2}', {"Content-Length": "13", "Content-Type": "application/json"}),
            (b"", {"Content-Length": "32769", "Content-Type": "application/json"}),
            (
                b"{}",
                {
                    "Content-Length": "2",
                    "Content-Type": "application/json",
                    "Transfer-Encoding": "chunked",
                },
            ),
            (b"{}", {"Content-Length": "2", "Content-Type": "text/plain"}),
        ):
            conn = http.client.HTTPSConnection(
                "localhost", self.listener.server_port, context=self.context, timeout=5
            )
            try:
                conn.request(
                    "POST", "/fixtures/http-journey", payload, {"Authorization": AUTH, **headers}
                )
                response = conn.getresponse()
                self.assertEqual(response.status, 409)
                self.assertEqual(response.read(), b'{"error":"fixture blocked"}')
            finally:
                conn.close()

    def test_untrusted_tls_never_reaches_controller(self):
        from urllib.request import build_opener

        self.d.runner.target._opener = build_opener()
        with self.assertRaises(RemoteError):
            self.d.runner.execute(self.d.request)
        with self.d.controller.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM executions").fetchone()[0], 0)

    def test_otlp_and_loki_exports_use_real_tls_and_signal_specific_credentials(self):
        self.d.runner.execute(self.d.request)
        received = []
        mode = ["partial"]

        class Sink(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append((self.path, self.headers["Authorization"], payload))
                if self.path == "/v1/traces":
                    body = (
                        b'{"partialSuccess":{"rejectedSpans":"1"}}'
                        if mode[0] == "partial"
                        else b"{}"
                    )
                    self.send_response(202 if mode[0] == "wrong-status" else 200)
                else:
                    body = b""
                    self.send_response(260 if mode[0] == "wrong-status" else 204)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(str(self.cert), str(self.key))
        sink.socket = tls.wrap_socket(sink.socket, server_side=True)
        thread = threading.Thread(target=sink.serve_forever, daemon=True)
        thread.start()
        try:
            config = {
                s: {
                    "base_url": f"https://localhost:{sink.server_port}",
                    "authorization_env": "SRE_DEMO_" + s.upper() + "_AUTH",
                    "tenant_env": None,
                    "ca_env": "SRE_DEMO_EXPORT_CA_FILE",
                }
                for s in ("traces", "logs")
            }
            clients = transports(
                config,
                {
                    "SRE_DEMO_TRACES_AUTH": "Bearer trace-ingest",
                    "SRE_DEMO_LOGS_AUTH": "Bearer log-ingest",
                    "SRE_DEMO_EXPORT_CA_FILE": str(self.cert),
                },
            )
            journal = Journal(self.d.root / "export-journal")
            with self.assertRaises(ValueError):
                flush(self.d.controller, clients, journal)
            mode[0] = "success"
            self.assertEqual(flush(self.d.controller, clients, journal), 1)
            self.assertEqual(len(received), 3)
            self.assertEqual(received[0][0:2], ("/loki/api/v1/push", "Bearer log-ingest"))
            self.assertEqual(received[1][0:2], ("/v1/traces", "Bearer trace-ingest"))
            self.assertEqual(received[1], received[2])
            self.assertNotIn(AUTH, json.dumps(received))
            mode[0] = "wrong-status"
            for signal, path in (("traces", "/v1/traces"), ("logs", "/loki/api/v1/push")):
                with self.assertRaises(RemoteError) as failure:
                    clients[signal].call("POST", path, {})
                self.assertEqual(failure.exception.status, 202 if signal == "traces" else 260)
        finally:
            sink.shutdown()
            sink.server_close()
            thread.join(timeout=5)

    def test_transient_context_outage_is_retryable_without_fixture_effects(self):
        scenario = self.d.policy.scenarios[0]
        command = self.d.runner.command(FixtureRequest.parse(self.d.request), scenario)
        with patch.object(self.d.controller.evidence, "context", side_effect=RemoteError(0)):
            with self.assertRaises(RemoteError) as failure:
                self.d.runner.target.call("POST", scenario.path, command)
            self.assertEqual(failure.exception.status, 503)
        with self.d.controller.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM executions").fetchone()[0], 0)
        self.d.runner.execute(self.d.request)

"""Actual TLS target requests, fixture identity fences and durable export acknowledgements."""

import json
import ssl
import subprocess
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from sre_suite.execution.http import JsonHttp, RemoteError
from sre_suite.execution.journal import Journal
from sre_suite.fixtures.journeys import JourneyTarget, validate
from sre_suite.target_service.reference import State, export_once, handler
from tests.test_target_service import identity


class TargetHttpsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.root = Path(directory.name).resolve()
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
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost,IP:127.0.0.1",
                "-keyout",
                str(cls.key),
                "-out",
                str(cls.cert),
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name).resolve()
        self.state = State(self.folder / "state.db", identity())
        self.token, self.metrics = "s" * 40, "m" * 40
        self.base = self.serve(handler(self.state, self.token, self.metrics))

    def serve(self, chosen):
        server = HTTPServer(("127.0.0.1", 0), chosen)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.cert), str(self.key))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(5)

        self.addCleanup(stop)
        return f"https://localhost:{server.server_port}"

    def test_runner_real_target_identity_trace_binding_and_response(self):
        now = datetime.now(UTC)
        config = validate(
            {
                "origins": {
                    "service": {
                        "base_url": self.base,
                        "authorization_env": "SRE_JOURNEY_SERVICE_AUTH",
                        "ca_env": "SRE_JOURNEY_CA_FILE",
                    }
                },
                "routes": {
                    "/reference/health": [
                        {
                            "origin": "service",
                            "method": "GET",
                            "path": "/health",
                            "payload": None,
                            "success_status": 200,
                        }
                    ]
                },
            },
            {"/reference/health"},
        )

        class Evidence:
            def context(self, scope, version):
                return {}

        target = JourneyTarget(
            config,
            "a" * 64,
            {
                "SRE_JOURNEY_SERVICE_AUTH": "Bearer " + self.token,
                "SRE_JOURNEY_CA_FILE": str(self.cert),
            },
            Evidence(),
            Journal(self.folder / "journal"),
            {"synthetic": True},
        )
        command = {
            "schema_version": 2,
            "binding": {
                "release": {
                    "scope": {
                        "service": "reference",
                        "environment": "development",
                        "revision": "b" * 40,
                        "environment_id": "preview-one",
                        "generation": 1,
                    }
                },
                "artifact_kind": "container-image-v1",
                "context_version": 0,
                "window_start": (now - timedelta(seconds=1)).isoformat(),
                "window_end": (now + timedelta(seconds=60)).isoformat(),
            },
            "policy_sha256": "a" * 64,
            "scenario": "health",
            "idempotency_key": "test",
            "trace_ids": ["a" * 32],
            "payload": {},
        }
        with patch("sre_suite.fixtures.target_service.verify", return_value=identity()):
            result = target.call("POST", "/reference/health", command)
            self.assertEqual(result["result"]["steps"][0]["body"], {"status": "ok"})
            self.assertEqual(len(self.state.pending()), 2)
            target.call("POST", "/reference/health", command)
            self.assertEqual(len(self.state.pending()), 2)
        with patch(
            "sre_suite.fixtures.target_service.verify",
            return_value=identity() | {"sre.environment.generation": "2"},
        ):
            with self.assertRaisesRegex(ValueError, "live target identity"):
                target.call("POST", "/reference/health", command)
        with self.assertRaises(RemoteError):
            JsonHttp(self.base, "Bearer wrong", local_ca_file=str(self.cert)).call(
                "GET", "/sre/identity"
            )
        with self.assertRaises(ValueError):
            JsonHttp(self.base, "Bearer " + self.token).call("GET", "/sre/identity")

    def test_real_export_rejects_partial_ack_then_retries_persisted_bytes(self):
        self.state.request(
            "/error", "00-" + "a" * 32 + "-" + "b" * 16 + "-01", "error", self.state.binding
        )
        calls = []
        partial = [True]

        class Receiver(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                calls.append((self.path, body, self.headers["Authorization"]))
                response = (
                    (b'{"partialSuccess":{"rejectedSpans":"1"}}' if partial[0] else b"{}")
                    if self.path == "/v1/traces"
                    else b""
                )
                self.send_response(200 if self.path == "/v1/traces" else 204)
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, *args):
                pass

        base = self.serve(Receiver)
        env = {
            "SRE_TARGET_TRACES_URL": base + "/v1/traces",
            "SRE_TARGET_LOGS_URL": base + "/loki/api/v1/push",
            "SRE_TARGET_TRACES_AUTH": "Bearer trace-private",
            "SRE_TARGET_LOGS_AUTH": "Bearer log-private",
            "SRE_TARGET_EXPORT_CA_FILE": str(self.cert),
        }
        with self.assertRaises(ValueError):
            export_once(self.state, env)
        self.assertEqual(len(self.state.pending()), 1)
        partial[0] = False
        reopened = State(self.state.path, identity())
        self.assertEqual(export_once(reopened, env), 1)
        self.assertEqual(len(reopened.pending()), 0)
        traces = [body for path, body, _ in calls if path == "/v1/traces"]
        self.assertEqual(traces[0], traces[1])
        self.assertNotIn("private", json.dumps(json.loads(traces[0])))

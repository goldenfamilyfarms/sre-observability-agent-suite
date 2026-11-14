"""Real loopback HTTPS requests to a synthetic fixture controller."""

import copy
import json
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import build_opener

from sre_suite.execution.http import RemoteError
from sre_suite.fixtures.config import target
from sre_suite.fixtures.contracts import FixturePolicy, FixtureRequest
from tests.fixtures_fixture import FixtureAgentFixture


class FixtureHttpsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        config = root / "openssl.cnf"
        config.write_text(
            "[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost\n"
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
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cls.ca, str(root / "key.pem"))

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                cls.requests.append((self.path, dict(self.headers), raw))
                if cls.mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "/credential-trap")
                    self.end_headers()
                    return
                response = {
                    k: raw[k]
                    for k in (
                        "schema_version",
                        "binding",
                        "policy_sha256",
                        "scenario",
                        "idempotency_key",
                    )
                }
                response.update(executed_at=cls.now.isoformat(), result={"ok": True})
                body = (
                    json.dumps(response).encode()
                    if cls.mode == "normal"
                    else b'{"duplicate":1,"duplicate":2}'
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.socket = tls.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.shutdown)

    @classmethod
    def shutdown(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.f = FixtureAgentFixture(Path(tmp.name).resolve())
        self.addCleanup(self.f.close)
        self.f.begin()
        raw = copy.deepcopy(self.f.raw)
        raw["target"].update(
            base_url=f"https://localhost:{self.server.server_port}",
            ca_env="SRE_FIXTURE_TARGET_CA_FILE",
        )
        self.policy = FixturePolicy.parse(raw)
        self.env = {
            "SRE_FIXTURE_TARGET_AUTH": "Bearer synthetic-fixture-only",
            "SRE_FIXTURE_TARGET_CA_FILE": self.ca,
        }
        self.request = copy.deepcopy(self.f.request)
        self.request["plan"].pop("digest")
        self.request["plan"]["fixture"] = self.request["dispatch"] = self.policy.dispatch
        self.f.runner.policy = self.policy
        self.f.runner.target = target(self.policy, self.env)
        type(self).now = self.f.now
        type(self).requests = []
        type(self).mode = "normal"

    def test_real_post_uses_exact_origin_authorization_payload_and_trace_ids(self):
        artifact = self.f.runner.execute(self.request)
        self.assertEqual(len(self.requests), 3)
        for path, headers, body in self.requests:
            self.assertTrue(path.startswith("/fixtures/"))
            self.assertEqual(headers["Authorization"], self.env["SRE_FIXTURE_TARGET_AUTH"])
            self.assertEqual(body["binding"], FixtureRequest.parse(self.request).binding)
            self.assertTrue(body["trace_ids"])
        self.assertEqual(self.f.runner.execute(self.request), artifact)
        self.assertEqual(len(self.requests), 3)

    def test_redirect_is_not_followed_and_duplicate_json_is_rejected(self):
        type(self).mode = "redirect"
        with self.assertRaises(RemoteError):
            self.f.runner.execute(self.request)
        self.assertEqual(len(self.requests), 1)
        type(self).mode = "duplicate"
        with self.assertRaises(ValueError):
            self.f.runner.execute(self.request)
        self.assertEqual(len(self.requests), 2)

    def test_untrusted_tls_rejected_before_target_request(self):
        from sre_suite.execution.http import JsonHttp

        self.f.runner.target = JsonHttp(
            self.policy.base_url, self.env["SRE_FIXTURE_TARGET_AUTH"], local_ca_file=self.ca
        )
        self.f.runner.target._opener = build_opener()
        with self.assertRaises(RemoteError):
            self.f.runner.execute(self.request)
        self.assertEqual(self.requests, [])

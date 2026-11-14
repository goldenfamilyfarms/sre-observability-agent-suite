"""Actual TLS gateway role separation and HTTP framing against a local backend."""

import http.client
import json
import socket
import ssl
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sre_suite.execution.http import JsonHttp, RemoteError
from sre_suite.execution.wire import canonical
from sre_suite.local_telemetry.config import configurations, initialize
from sre_suite.local_telemetry.gateway import server
from tests.test_local_pilot import LocalFixture


class GatewayTests(LocalFixture):
    def setUp(self):
        super().setUp()
        self.initialize()
        self.manifest_path.chmod(0o600)
        token = self.root / "metrics.token"
        token.write_text("Bearer " + "m" * 40)
        token.chmod(0o600)
        self.telemetry = self.root / "telemetry"
        self.config = initialize(
            self.telemetry,
            self.manifest_path,
            "https://localhost:9443",
            self.stack / "server.crt",
            token,
            mode="desktop",
        )
        self.calls = []
        self.redirect = False
        self.retention = (
            "compactor:\n  retention_enabled: true\n  delete_request_store: filesystem\n"
            "limits_config:\n  retention_period: 24h\n"
        )
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.calls.append((self.command, self.path, dict(self.headers)))
                self.send_response(302 if owner.redirect else 200)
                data = owner.retention.encode() if self.path == "/config" else b"{}"
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                owner.calls.append((self.command, self.path, dict(self.headers)))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *_):
                pass

        self.backend = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.backend_thread = threading.Thread(target=self.backend.serve_forever, daemon=True)
        self.backend_thread.start()
        self.addCleanup(self.stop_backend)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.config = replace(
            self.config,
            ports={**self.config.ports, "loki": self.backend.server_port, "loki_tls": port},
        )
        self.gateway = server(self.telemetry, self.config, "loki")
        self.gateway_thread = threading.Thread(target=self.gateway.serve_forever, daemon=True)
        self.gateway_thread.start()
        self.addCleanup(self.stop_gateway)
        self.tokens = json.loads((self.telemetry / "tokens.json").read_text())
        self.read = self.gateway_client("loki_read")
        self.write = self.gateway_client("logs_write")

    def gateway_client(self, role):
        return JsonHttp(
            self.config.url("loki"),
            self.tokens[role],
            local_ca_file=str(self.telemetry / "server.crt"),
        )

    def stop_gateway(self):
        self.gateway.shutdown()
        self.gateway.server_close()
        self.gateway_thread.join(timeout=5)

    def stop_backend(self):
        self.backend.shutdown()
        self.backend.server_close()
        self.backend_thread.join(timeout=5)

    def test_retention_probe_uses_effective_backend_config_and_separate_read_credential(self):
        document = json.loads((self.telemetry / "stack.json").read_text())
        document["ports"] = self.config.ports
        (self.telemetry / "stack.json").write_text(canonical(document))
        for backend, value in configurations(self.config).items():
            (self.telemetry / (backend + ".json")).write_text(canonical(value))

        def get(role):
            connection = http.client.HTTPSConnection(
                "localhost",
                self.config.ports["loki_tls"],
                context=ssl.create_default_context(cafile=str(self.telemetry / "server.crt")),
                timeout=3,
            )
            try:
                connection.request("GET", "/metrics", headers={"Authorization": self.tokens[role]})
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()

        self.assertEqual(get("logs_write")[0], 401)
        self.assertEqual(get("tempo_read")[0], 401)
        status, body = get("loki_read")
        self.assertEqual(status, 200)
        self.assertIn(b"sre_local_loki_retention_seconds{", body)
        self.assertTrue(body.endswith(b" 86400\n"))
        self.assertIn(("GET", "/config"), [(c[0], c[1]) for c in self.calls])
        self.retention = self.retention.replace("24h", "0s")
        self.assertEqual(get("loki_read")[0], 400)
        self.assertNotIn(b"sre_local_loki_retention_seconds", get("loki_read")[1])

    def test_auth_roles_and_credentials_never_forwarded(self):
        self.assertEqual(self.read.call("GET", "/loki/api/v1/query_range?query=synthetic"), {})
        self.assertIsNone(self.write.call("POST", "/loki/api/v1/push", {"streams": []}))
        for _, _, headers in self.calls:
            self.assertNotIn("Authorization", headers)
            self.assertNotIn("X-Scope-OrgID", headers)
        count = len(self.calls)
        for client, method, path in (
            (self.read, "POST", "/loki/api/v1/push"),
            (self.write, "GET", "/loki/api/v1/query_range"),
            (self.gateway_client("traces_write"), "POST", "/loki/api/v1/push"),
        ):
            with self.assertRaises(RemoteError) as error:
                client.call(method, path, {} if method == "POST" else None)
            self.assertEqual(error.exception.status, 401)
        self.assertEqual(len(self.calls), count)

    def test_untrusted_tls_framing_and_unlisted_routes_fail_closed(self):
        address = ("127.0.0.1", self.gateway.server_port)
        with socket.create_connection(address, timeout=5) as raw:
            with self.assertRaises(ssl.SSLCertVerificationError):
                ssl.create_default_context().wrap_socket(raw, server_hostname="localhost")
        for headers in (
            "Content-Length: 2\r\nContent-Length: 2\r\n",
            "Content-Length: 2\r\nTransfer-Encoding: chunked\r\n",
        ):
            context = ssl.create_default_context(cafile=str(self.telemetry / "server.crt"))
            with (
                socket.create_connection(address, timeout=5) as raw,
                context.wrap_socket(raw, server_hostname="localhost") as tls,
            ):
                tls.sendall(
                    (
                        "POST /loki/api/v1/push HTTP/1.0\r\nAuthorization: "
                        + self.tokens["logs_write"]
                        + "\r\nContent-Type: application/json\r\n"
                        + headers
                        + "\r\n{}"
                    ).encode()
                )
                self.assertIn(b"400", tls.recv(4096).split(b"\r\n")[0])
        with self.assertRaises(RemoteError) as error:
            self.read.call("GET", "/config")
        self.assertEqual(error.exception.status, 404)
        self.assertEqual(self.calls, [])

    def test_redirect_and_backend_failure_cannot_report_success(self):
        self.redirect = True
        with self.assertRaises(RemoteError) as error:
            self.read.call("GET", "/loki/api/v1/query_range")
        self.assertEqual(error.exception.status, 502)
        self.stop_backend()
        with self.assertRaises(RemoteError) as error:
            self.read.call("GET", "/ready")
        self.assertEqual(error.exception.status, 503)

"""Loopback TLS gateway: fixed backend ports/routes and separate ingest/read credentials."""

import hmac
import http.client
import json
import re
import socket
import ssl
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..demo_fixture.server import credential
from ..execution.wire import canonical
from ..verification.config import private_document
from .config import Config

LIMIT = 1_500_000


def route(backend: str, method: str, path: str) -> tuple[str, str]:
    if len(path) > 16384 or not path.startswith("/") or path.startswith("//"):
        raise ValueError("invalid request target")
    parsed = urlsplit(path)
    if parsed.fragment or parsed.scheme or parsed.netloc:
        raise ValueError("invalid request target")
    if method == "GET":
        allowed = {
            "prometheus": {"/api/v1/query_range"},
            "loki": {"/loki/api/v1/query_range"},
            "tempo": {"/api/search"},
        }
        if parsed.path in allowed[backend] or (
            backend == "tempo" and re.fullmatch(r"/api/v2/traces/[0-9a-f]{32}", parsed.path)
        ):
            return backend, backend + "_read"
        if path == "/metrics" and backend == "loki":
            return backend, backend + "_read"
        if path == "/ready":
            return backend, backend + "_read"
    if method == "POST" and not parsed.query:
        if backend == "loki" and path == "/loki/api/v1/push":
            return "loki", "logs_write"
        if backend == "tempo" and path == "/v1/traces":
            return "otlp", "traces_write"
    raise ValueError("route is not authorized")


def backend_call(
    port: int, method: str, path: str, body: bytes | None = None
) -> tuple[int, bytes, str]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(
            method,
            path,
            body=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        data = response.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise ValueError("response exceeds budget")
        return response.status, data, response.getheader("Content-Type", "application/json")
    finally:
        connection.close()


def retention_seconds(payload: bytes) -> int:
    """Read only the required fields from Loki's bounded, effective YAML config endpoint.

    Deliberately not a general YAML loader: duplicate/ambiguous fields and unrecognized
    representations fail closed instead of creating a retention claim.
    """
    document = payload.decode("utf-8")

    def field(section: str, name: str) -> str:
        sections = re.findall(r"^" + section + r":\n((?:[ \t]+[^\n]*\n|\n)*)", document, re.M)
        if len(sections) != 1:
            raise ValueError("ambiguous effective backend config")
        values = re.findall(r"^  " + name + r": ([^\n]+)$", sections[0], re.M)
        if len(values) != 1:
            raise ValueError("missing effective backend config")
        return str(values[0])

    if (
        field("compactor", "retention_enabled") != "true"
        or field("compactor", "delete_request_store") != "filesystem"
        or field("limits_config", "retention_period") not in {"24h", "24h0m0s", "1d", "1d0h0m0s"}
    ):
        raise ValueError("effective retention policy differs")
    return 86400


def server(root: Path, config: Config, backend: str) -> HTTPServer:
    tokens = private_document(root / "tokens.json")
    if not isinstance(tokens, dict) or set(tokens) != {
        "prometheus_read",
        "loki_read",
        "tempo_read",
        "logs_write",
        "traces_write",
    }:
        raise ValueError("invalid gateway credentials")
    hashes = {role: credential(value) for role, value in tokens.items()}
    if len(set(hashes.values())) != len(hashes):
        raise ValueError("separate read/ingest credentials required")

    class Handler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(5)

        def reply(self, status: int, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def handle_call(self) -> None:
            try:
                target, role = route(backend, self.command, self.path)
            except ValueError:
                self.reply(404, b'{"error":"unavailable"}')
                return
            try:
                auth = self.headers.get_all("Authorization", [])
                if len(auth) != 1 or not hmac.compare_digest(credential(auth[0]), hashes[role]):
                    raise ValueError("unauthorized")
            except ValueError:
                self.reply(401, b'{"error":"unauthorized"}')
                return
            try:
                lengths = self.headers.get_all("Content-Length", [])
                if self.headers.get("Transfer-Encoding") is not None:
                    raise ValueError("ambiguous body")
                if self.command == "POST":
                    if (
                        len(lengths) != 1
                        or not lengths[0].isdigit()
                        or not 0 < int(lengths[0]) <= LIMIT
                        or self.headers.get_all("Content-Type", []) != ["application/json"]
                    ):
                        raise ValueError("invalid body")
                    body = self.rfile.read(int(lengths[0]))
                    if len(body) != int(lengths[0]):
                        raise ValueError("incomplete body")
                else:
                    if lengths not in ([], ["0"]):
                        raise ValueError("GET body forbidden")
                    body = None
                if self.path == "/metrics":
                    # Config evidence comes from the stack owner, never the demo application.
                    # Readiness ties the validated read-only mounted config to a live backend.
                    if (
                        Config.load(root) != config
                        or backend_call(config.ports["loki"], "GET", "/ready")[0] != 200
                    ):
                        raise ValueError("backend policy unavailable")
                    config_status, effective, _ = backend_call(
                        config.ports["loki"], "GET", "/config"
                    )
                    if config_status != 200:
                        raise ValueError("effective backend config unavailable")
                    retention = retention_seconds(effective)
                    mapping = {
                        "service": "service_name",
                        "environment": "deployment_environment",
                        "revision": "service_version",
                        "environment_id": "preview_id",
                        "generation": "preview_generation",
                    }
                    labels = ",".join(
                        k + "=" + json.dumps(str(asdict(config.scope)[n]))
                        for n, k in mapping.items()
                    )
                    self.reply(
                        200,
                        (f"sre_local_loki_retention_seconds{{{labels}}} {retention}\n").encode(),
                        "text/plain; version=0.0.4",
                    )
                    return
                path = (
                    "/-/ready" if self.path == "/ready" and backend == "prometheus" else self.path
                )
                status, data, content_type = backend_call(
                    config.ports[target], self.command, path, body
                )
                if self.path == "/ready":
                    data, content_type = (
                        canonical({"status": "ready" if status == 200 else "unavailable"}).encode(),
                        "application/json",
                    )
                if 300 <= status < 400:
                    status, data, content_type = (
                        502,
                        b'{"error":"redirect refused"}',
                        "application/json",
                    )
                self.reply(status, data, content_type)
            except ValueError:
                self.reply(400, b'{"error":"invalid request"}')
            except Exception:
                self.reply(503, b'{"error":"backend unavailable"}')

        do_GET = handle_call
        do_POST = handle_call

        def log_message(self, format: str, *args: Any) -> None:
            pass

    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(str(root / "server.crt"), str(root / "server.key"))

    class TlsServer(HTTPServer):
        def get_request(self) -> tuple[socket.socket, Any]:
            connection, address = super().get_request()
            connection.settimeout(5)
            try:
                return tls.wrap_socket(connection, server_side=True), address
            except BaseException:
                connection.close()
                raise

    return TlsServer(("127.0.0.1", config.ports[backend + "_tls"]), Handler)

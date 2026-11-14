"""Loopback-only HTTPS controller with separate fixture and metric credentials."""

import hashlib
import hmac
import socket
import sqlite3
import ssl
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from ..api_wire import decode
from ..execution.http import RemoteError
from ..execution.wire import canonical
from .controller import Controller


def credential(value: str) -> str:
    if (
        not value.startswith("Bearer ")
        or len(value) < 39
        or len(value) > 256
        or any(ord(c) < 33 or ord(c) > 126 for c in value[7:])
    ):
        raise ValueError("use a private Bearer token with at least 32 characters")
    return hashlib.sha256(value.encode()).hexdigest()


def handler(
    controller: Controller,
    fixture_hash: str,
    metrics_hash: str,
    lifecycle_hash: str | None = None,
    ready: Callable[[], dict[str, Any]] | None = None,
) -> type[BaseHTTPRequestHandler]:
    if fixture_hash == metrics_hash or any(len(v) != 64 for v in (fixture_hash, metrics_hash)):
        raise ValueError("separate fixture and scrape credentials required")

    class Handler(BaseHTTPRequestHandler):
        server_version = "SRE-Demo"
        sys_version = ""

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(5)

        def authorized(self, expected: str) -> bool:
            values = self.headers.get_all("Authorization", [])
            if len(values) != 1:
                return False
            try:
                actual = credential(values[0])
            except (ValueError, UnicodeError):
                return False
            return hmac.compare_digest(actual, expected)

        def reply(self, status: int, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if not self.authorized(fixture_hash):
                self.reply(401, b'{"error":"unauthorized"}')
                return
            try:
                lengths = self.headers.get_all("Content-Length", [])
                if (
                    len(lengths) != 1
                    or not lengths[0].isdigit()
                    or not 0 < int(lengths[0]) <= 32768
                    or self.headers.get("Transfer-Encoding") is not None
                    or self.headers.get_all("Content-Type", []) != ["application/json"]
                ):
                    raise ValueError("invalid request framing")
                body = self.rfile.read(int(lengths[0]))
                if len(body) != int(lengths[0]):
                    raise ValueError("incomplete request")
                response = controller.execute(self.path, decode(body))
                self.reply(200, canonical(response).encode())
            except RemoteError as error:
                status = (
                    503 if error.status == 0 or error.status == 429 or error.status >= 500 else 409
                )
                self.reply(status, b'{"error":"fixture blocked"}')
            except sqlite3.OperationalError:
                self.reply(503, b'{"error":"fixture blocked"}')
            except Exception:
                self.reply(409, b'{"error":"fixture blocked"}')

        def do_GET(self) -> None:
            if self.path == "/ready" and lifecycle_hash is not None and ready is not None:
                if not self.authorized(lifecycle_hash):
                    self.reply(401, b'{"error":"unauthorized"}')
                    return
                try:
                    self.reply(200, canonical(ready()).encode())
                except Exception:
                    self.reply(409, b'{"error":"target unavailable"}')
                return
            if not self.authorized(metrics_hash):
                self.reply(401, b'{"error":"unauthorized"}')
            elif self.path != "/metrics":
                self.reply(404, b'{"error":"not found"}')
            else:
                try:
                    self.reply(
                        200,
                        controller.metrics().encode(),
                        "text/plain; version=0.0.4; charset=utf-8",
                    )
                except Exception:
                    self.reply(409, b'{"error":"scrape blocked"}')

        def log_message(self, format: str, *args: Any) -> None:
            # Do not put raw paths, request context or credentials into diagnostic logs.
            pass

    return Handler


def server(
    controller: Controller,
    fixture_token: str,
    metrics_token: str,
    cert: str,
    key: str,
    port: int,
    *,
    lifecycle_token: str | None = None,
    ready: Callable[[], dict[str, Any]] | None = None,
) -> HTTPServer:
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("invalid listen port")
    if lifecycle_token is not None and lifecycle_token in {fixture_token, metrics_token}:
        raise ValueError("lifecycle readiness requires a separate credential")
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(cert, key)

    class TlsServer(HTTPServer):
        def get_request(self) -> tuple[socket.socket, Any]:
            connection, address = super().get_request()
            connection.settimeout(5)
            try:
                return tls.wrap_socket(connection, server_side=True), address
            except BaseException:
                connection.close()
                raise

    return TlsServer(
        ("127.0.0.1", port),
        handler(
            controller,
            credential(fixture_token),
            credential(metrics_token),
            credential(lifecycle_token) if lifecycle_token else None,
            ready,
        ),
    )

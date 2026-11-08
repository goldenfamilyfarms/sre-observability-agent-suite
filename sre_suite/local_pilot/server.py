"""Loopback-only HTTPS reference host for the local pilot, never a production server."""

import io
import socket
import ssl
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO, cast
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer
from wsgiref.types import StartResponse, WSGIApplication, WSGIEnvironment

from ..api import EvidenceContextApi
from ..api_storage import postgres_backend
from ..auth import TokenAuthenticator
from .config import AUDIENCE, LocalConfig, dsn, load_credentials


class Handler(WSGIRequestHandler):
    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        for header in ("Content-Length", "Content-Type", "Authorization", "Host"):
            if len(self.headers.get_all(header, [])) > 1:
                self.send_error(400)
                return False
        if self.headers.get("Transfer-Encoding"):
            self.send_error(400)
            return False
        return True

    def get_environ(self) -> WSGIEnvironment:
        env = super().get_environ()
        env["HTTPS"] = "on"  # The server itself terminates TLS, never a forwarded header.
        return env

    def get_stderr(self) -> TextIO:
        return io.StringIO()  # Do not expose exception/request content in host logs.

    def log_message(self, format: str, *args: Any) -> None:
        pass


class LocalHttpsServer(WSGIServer):
    allow_reuse_address = True
    request_queue_size = 8

    def __init__(self, port: int, certificate: str, key: str, app: WSGIApplication) -> None:
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.load_cert_chain(certificate, key)
        super().__init__(("127.0.0.1", port), Handler)
        self.set_app(app)

    def get_request(self) -> tuple[socket.socket, Any]:
        client, address = super().get_request()
        client.settimeout(5)
        try:
            return self.context.wrap_socket(client, server_side=True), address
        except Exception:
            client.close()
            raise

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass


def application(root: Path, config: LocalConfig) -> WSGIApplication:
    from ..postgres import PostgresDatabase

    root = Path(root)
    connection = dsn(root, config)
    auth = TokenAuthenticator(AUDIENCE, load_credentials(root / "credentials.json"))
    api = EvidenceContextApi(auth, postgres_backend(connection), allow_preview_registration=True)

    def health() -> None:
        PostgresDatabase(connection, "sre_suite").close()

    return with_health(
        api, health, lambda: auth.replace_credentials(load_credentials(root / "credentials.json"))
    )


def with_health(
    api: WSGIApplication, health: Callable[[], None], reload: Callable[[], None]
) -> WSGIApplication:
    def app(env: WSGIEnvironment, start: StartResponse) -> list[bytes]:
        try:
            reload()  # Invalid/revoked configuration fails closed; never keep stale grants.
            if env.get("PATH_INFO") == "/healthz" and env.get("REQUEST_METHOD") == "GET":
                health()
                body = b'{"status":"ready"}'
                start(
                    "200 OK",
                    [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                        ("Cache-Control", "no-store"),
                    ],
                )
                return [body]
            return cast(list[bytes], api(env, start))
        except Exception:
            body = b'{"error":"service_unavailable"}'
            start(
                "503 Service Unavailable",
                [("Content-Type", "application/json"), ("Content-Length", str(len(body)))],
            )
            return [body]

    return app


def serve(root: Path) -> None:
    from ..execution.pilot import _private

    config = LocalConfig.load(root)
    for name in ("server.crt", "server.key"):
        _private(root / name, directory=False)
    with LocalHttpsServer(
        config.api_port,
        str(root / "server.crt"),
        str(root / "server.key"),
        application(root, config),
    ) as server:
        server.serve_forever(poll_interval=0.2)

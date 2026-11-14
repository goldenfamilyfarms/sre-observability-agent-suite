"""Standalone nonproduction Python reference target; standard library only.

Exports measured HTTP traces, structured logs and Prometheus metrics. It never fabricates
coverage for databases, messaging, gRPC or JVM capabilities that it does not implement.
"""

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import ssl
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

RESOURCE_KEYS = {
    "service.name",
    "deployment.environment.name",
    "service.version",
    "sre.source.revision",
    "sre.environment.id",
    "sre.environment.generation",
    "sre.artifact.digest",
    "sre.plan.digest",
}


def canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def resource(env: dict[str, str]) -> dict[str, str]:
    pairs = [item.split("=", 1) for item in env["OTEL_RESOURCE_ATTRIBUTES"].split(",")]
    values = dict(pairs)
    if len(values) != len(pairs) or set(values) != RESOURCE_KEYS:
        raise ValueError("exact deployment resource binding required")
    if values["deployment.environment.name"] not in {"development", "staging"}:
        raise ValueError("fault fixture reference cannot run in production")
    for key in ("service.version", "sre.source.revision"):
        if not re.fullmatch(r"[0-9a-f]{40}", values[key]):
            raise ValueError("full source revisions required")
    for key in ("sre.artifact.digest", "sre.plan.digest"):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", values[key]):
            raise ValueError("image and plan digests required")
    if (
        not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", values["sre.environment.id"])
        or not re.fullmatch(r"[1-9][0-9]{0,17}", values["sre.environment.generation"])
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", values["service.name"])
    ):
        raise ValueError("bounded service and preview identity required")
    return values


def attributes(value: dict[str, str]) -> list[dict[str, Any]]:
    return [{"key": k, "value": {"stringValue": v}} for k, v in sorted(value.items())]


class State:
    def __init__(self, path: Path, identity: dict[str, str]) -> None:
        self.path, self.identity = path, identity
        self.binding = hashlib.sha256(canonical(identity).encode()).hexdigest()
        if not path.is_absolute() or path.is_symlink():
            raise ValueError("absolute regular state path required")
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS binding(value TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS requests(
                  key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status INTEGER NOT NULL,
                  body TEXT NOT NULL, trace TEXT NOT NULL, log TEXT NOT NULL,
                  duration REAL NOT NULL, route TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox(
                  key TEXT NOT NULL, signal TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY(key,signal));
            """)
            rows = db.execute("SELECT value FROM binding").fetchall()
            if rows and rows != [(self.binding,)]:
                raise ValueError("state belongs to another artifact/plan/preview generation")
            db.execute("INSERT OR IGNORE INTO binding VALUES (?)", (self.binding,))
        path.chmod(0o600)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def request(
        self, path: str, traceparent: str, key: str, binding: str
    ) -> tuple[int, dict[str, Any]]:
        if binding != self.binding or path not in {"/health", "/unicode", "/error"}:
            raise ValueError("target or route differs")
        match = re.fullmatch(r"00-([0-9a-f]{32})-([0-9a-f]{16})-01", traceparent)
        if (
            not match
            or not all(int(v, 16) for v in match.groups())
            or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", key)
        ):
            raise ValueError("reviewed trace and idempotency key required")
        trace_id, parent = match.groups()
        fingerprint = hashlib.sha256(canonical([path, traceparent, binding]).encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT fingerprint,status,body FROM requests WHERE key=?", (key,)
            ).fetchone()
            if old:
                if old[0] != fingerprint:
                    raise ValueError("idempotency key reused with another request")
                return old[1], json.loads(old[2])
            if db.execute("SELECT count(*) FROM requests").fetchone()[0] >= 10000:
                raise ValueError("reference workload budget exhausted")
            start, span_id = time.time_ns(), secrets.token_hex(8)
            error = None
            if path == "/error":
                try:
                    raise RuntimeError("controlled reference failure")
                except RuntimeError as caught:
                    error = {
                        "exception.type": type(caught).__name__,
                        "exception.message": str(caught),
                    }
                status, body = 500, {"error": "controlled failure"}
            elif path == "/unicode":
                status, body = 200, {"message": "Hello, 世界 🌾"}
            else:
                status, body = 200, {"status": "ok"}
            end = max(time.time_ns(), start + 1)
            span = {
                "traceId": trace_id,
                "spanId": span_id,
                "parentSpanId": parent,
                "name": "GET " + path,
                "kind": 2,
                "startTimeUnixNano": str(start),
                "endTimeUnixNano": str(end),
                "status": {"code": 2 if error else 1},
                "attributes": attributes(
                    {
                        "http.request.method": "GET",
                        "http.route": path,
                        "http.response.status_code": str(status),
                    }
                ),
                "events": [
                    {"name": "exception", "timeUnixNano": str(end), "attributes": attributes(error)}
                ]
                if error
                else [],
            }
            trace = {
                "resourceSpans": [
                    {
                        "resource": {"attributes": attributes(self.identity)},
                        "scopeSpans": [
                            {
                                "scope": {"name": "sre-python-reference", "version": "1"},
                                "spans": [span],
                            }
                        ],
                    }
                ]
            }
            log = {
                "resource": self.identity,
                "timestamp_ns": str(end),
                "event": "request_completed",
                "trace_id": trace_id,
                "span_id": span_id,
                "route": path,
                "status": status,
                "duration_seconds": (end - start) / 1e9,
                "severity": "ERROR" if error else "INFO",
            }
            if error:
                log["exception"] = error
            db.execute(
                "INSERT INTO requests VALUES (?,?,?,?,?,?,?,?)",
                (
                    key,
                    fingerprint,
                    status,
                    canonical(body),
                    canonical(trace),
                    canonical(log),
                    (end - start) / 1e9,
                    path,
                ),
            )
            db.executemany(
                "INSERT INTO outbox(key,signal) VALUES (?,?)", [(key, "traces"), (key, "logs")]
            )
        return status, body

    def metrics(self) -> str:
        labels = {k.replace(".", "_"): v for k, v in self.identity.items()}
        with self.connect() as db:
            rows = db.execute(
                "SELECT route,status,count(*),sum(duration) FROM requests GROUP BY route,status"
            ).fetchall()
            pending = db.execute("SELECT count(*) FROM outbox WHERE delivered=0").fetchone()[0]
        lines = [
            "# TYPE sre_target_requests_total counter",
            "# TYPE sre_target_request_duration_seconds_sum counter",
        ]
        for route, status, count, duration in rows:
            selected = labels | {"route": route, "status": str(status)}
            label = ",".join(
                k + "=" + json.dumps(v, ensure_ascii=False) for k, v in sorted(selected.items())
            )
            lines.extend(
                [
                    f"sre_target_requests_total{{{label}}} {count}",
                    f"sre_target_request_duration_seconds_sum{{{label}}} {duration}",
                ]
            )
        lines.append(f"sre_target_outbox_pending {pending}")
        return "\n".join(lines) + "\n"

    def pending(self) -> list[tuple[str, str, dict[str, Any]]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT o.key,o.signal,r.trace,r.log FROM outbox o "
                "JOIN requests r ON r.key=o.key WHERE o.delivered=0 "
                "ORDER BY o.key,o.signal LIMIT 20"
            ).fetchall()
        output = []
        for key, signal, trace, log in rows:
            if signal == "traces":
                payload = json.loads(trace)
            else:
                entry = json.loads(log)
                labels = {k.replace(".", "_"): v for k, v in self.identity.items()}
                payload = {
                    "streams": [{"stream": labels, "values": [[entry["timestamp_ns"], log]]}]
                }
            output.append((key, signal, payload))
        return output

    def acknowledge(self, key: str, signal: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE outbox SET delivered=1 WHERE key=? AND signal=?", (key, signal))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        return None


def export_once(state: State, env: dict[str, str]) -> int:
    delivered = 0
    for key, signal, payload in state.pending():
        prefix = "SRE_TARGET_" + signal.upper()
        url, auth = env[prefix + "_URL"], env[prefix + "_AUTH"]
        parsed = urlsplit(url)
        expected = "/v1/traces" if signal == "traces" else "/loki/api/v1/push"
        if (
            parsed.scheme != "https"
            or parsed.path != expected
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not parsed.hostname
        ):
            raise ValueError("exact HTTPS telemetry endpoint required")
        if not auth or any(ord(c) < 32 or ord(c) > 126 for c in auth):
            raise ValueError("private export credential required")
        ca = env.get("SRE_TARGET_EXPORT_CA_FILE")
        context = ssl.create_default_context(cafile=ca)
        opener = build_opener(NoRedirect(), HTTPSHandler(context=context))
        headers = {"Authorization": auth, "Content-Type": "application/json"}
        tenant = env.get(prefix + "_TENANT")
        if tenant:
            if any(ord(c) < 33 or ord(c) > 126 for c in tenant):
                raise ValueError("invalid tenant")
            headers["X-Scope-OrgID"] = tenant
        with opener.open(
            Request(url, canonical(payload).encode(), headers=headers, method="POST"), timeout=5
        ) as reply:
            raw = reply.read(65537)
            if len(raw) > 65536 or reply.status != (200 if signal == "traces" else 204):
                raise ValueError("telemetry export rejected")
            if signal == "traces":
                response = json.loads(raw)
                if response != {} and response != {"partialSuccess": {"rejectedSpans": "0"}}:
                    raise ValueError("partial trace export rejected")
            elif raw:
                raise ValueError("invalid log export acknowledgement")
        state.acknowledge(key, signal)
        delivered += 1
    return delivered


def handler(state: State, token: str, metrics_token: str) -> type[BaseHTTPRequestHandler]:
    if len(token) < 32 or len(metrics_token) < 32 or token == metrics_token:
        raise ValueError("distinct private workload and scrape tokens required")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(5)

        def reply(self, status: int, value: object, metrics: bool = False) -> None:
            data = str(value).encode() if metrics else canonical(value).encode()
            self.send_response(status)
            self.send_header(
                "Content-Type", "text/plain; version=0.0.4" if metrics else "application/json"
            )
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            # Cluster probes do not execute a workload or expose identity/telemetry.
            if self.path == "/ready":
                self.reply(200, {"ready": True})
                return
            expected = metrics_token if self.path == "/metrics" else token
            supplied = self.headers.get_all("Authorization", [])
            if len(supplied) != 1 or not hmac.compare_digest(supplied[0], "Bearer " + expected):
                self.reply(401, {"error": "unauthorized"})
                return
            try:
                if self.headers.get("Transfer-Encoding") or self.headers.get(
                    "Content-Length"
                ) not in (None, "0"):
                    raise ValueError("unexpected request body")
                if self.path == "/metrics":
                    self.reply(200, state.metrics(), True)
                elif self.path == "/sre/identity":
                    self.reply(200, {"resource": state.identity})
                else:

                    def header(name: str) -> str:
                        values = self.headers.get_all(name, [])
                        if len(values) != 1:
                            raise ValueError("unique workload headers required")
                        return values[0]

                    status, body = state.request(
                        self.path,
                        header("traceparent"),
                        header("Idempotency-Key"),
                        header("X-SRE-Target"),
                    )
                    self.reply(status, body)
            except Exception:
                self.reply(409, {"error": "fixture blocked"})

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Nonproduction Python target service")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--state", type=Path, default=Path("/tmp/sre-target.db"))
    args = parser.parse_args()
    env = dict(os.environ)
    state = State(args.state, resource(env))
    http = HTTPServer(
        (args.bind, args.port),
        handler(state, env["SRE_TARGET_TOKEN"], env["SRE_TARGET_METRICS_TOKEN"]),
    )
    stop = threading.Event()

    def export_loop() -> None:
        while not stop.wait(1):
            try:
                export_once(state, env)
            except Exception:
                # Pending rows survive retries; no credentials or raw request data in diagnostics.
                pass

    worker = threading.Thread(target=export_loop, daemon=True)
    worker.start()
    try:
        http.serve_forever()
    finally:
        stop.set()
        http.server_close()
        worker.join(timeout=10)


if __name__ == "__main__":
    main()

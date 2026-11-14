"""Atomic local fixture effects, independently pinned target identity and durable outbox."""

import copy
import hashlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from typing import Any

from ..api_wire import fields, text
from ..contracts import aware
from ..execution.evidence_client import EvidenceClient
from ..execution.wire import canonical, digest
from ..fixtures.contracts import FixturePolicy, FixtureRequest
from ..telemetry.contracts import CandidateBinding, epoch_ns
from ..telemetry.schedule_config import private_path
from .scenarios import INVALID_CONTEXTS, JOURNEY, reviewed
from .service import CAPABILITIES, handle

SCHEMA = """
CREATE TABLE IF NOT EXISTS identity (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS executions (key TEXT PRIMARY KEY, command TEXT NOT NULL,
 response TEXT NOT NULL, events TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (key TEXT NOT NULL, signal TEXT NOT NULL,
 payload TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0 CHECK(delivered IN (0,1)),
 PRIMARY KEY(key, signal));
CREATE TABLE IF NOT EXISTS delivery (
 key TEXT NOT NULL, signal TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 failures INTEGER NOT NULL DEFAULT 0, acknowledged_ns INTEGER,
 PRIMARY KEY(key, signal));
"""


def service_source() -> str:
    return files("sre_suite.demo_fixture").joinpath("service.py").read_text()


def recipe(repository: str, revision: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not re.fullmatch(
        r"[0-9a-f]{40}", revision
    ):
        raise ValueError("full repository and base revision required")
    return {
        "kind": "source-candidate-v1",
        "repository": repository,
        "base_revision": revision,
        "edits": [{"path": "demo_service.py", "content": service_source()}],
    }


def validate_target(
    policy: FixturePolicy, request: FixtureRequest, source_recipe: object
) -> dict[str, Any]:
    reviewed(policy)
    request.authorize(policy)
    source = fields(source_recipe, {"kind", "repository", "base_revision", "edits"})
    expected = recipe(text(source["repository"]), request.scope.revision)
    if source != expected or request.binding["release"]["artifact_digest"] != "sha256:" + digest(
        expected
    ):
        raise ValueError("running demo bytes do not match the independently pinned candidate")
    return expected


def attributes(value: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for k, v in value.items():
        kind = "boolValue" if type(v) is bool else "intValue" if type(v) is int else "stringValue"
        result.append({"key": k, "value": {kind: str(v) if type(v) is int else v}})
    return result


class Controller:
    def __init__(
        self,
        policy: FixturePolicy,
        request: object,
        source_recipe: object,
        root: Path,
        evidence: EvidenceClient,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.policy = copy.deepcopy(policy)
        self.request = FixtureRequest.parse(copy.deepcopy(request))
        policy = self.policy
        expected = validate_target(policy, self.request, source_recipe)
        private_path(root, directory=True)
        self.path = root / "controller.sqlite3"
        if not self.path.exists():
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        private_path(self.path, directory=False)
        self.evidence, self.clock = evidence, clock
        self.process_started = time.monotonic()
        self.trace_id = next(s.trace_id for s in policy.verification.sources if s.trace_id)
        identity = canonical(
            {
                "binding": self.request.binding,
                "dispatch": policy.dispatch,
                "recipe": expected,
                **({"demo": self.request.demo} if self.request.demo else {}),
            }
        )
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT value FROM identity WHERE singleton=1").fetchone()
            if existing and existing[0] != identity:
                raise ValueError("controller state belongs to another target generation or plan")
            conn.execute("INSERT OR IGNORE INTO identity VALUES (1,?)", (identity,))

    def labels_for(self, backend: str) -> dict[str, str]:
        profile = next(
            s.profile for s in self.policy.verification.sources if s.profile.backend == backend
        )
        return profile.bindings(
            self.request.scope,
            CandidateBinding(
                self.request.binding["release"]["artifact_digest"],
                self.request.binding["release"]["plan_digest"],
            ),
        )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        # No external side effect occurs in this transaction: only pure demo calls and local
        # response/metric/outbox rows. A rollback may recompute pure work, never duplicate commits.
        conn = sqlite3.connect(self.path, timeout=2)
        conn.execute("PRAGMA synchronous=FULL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def guard(self, *, execution: bool = False) -> datetime:
        now = self.clock()
        aware(now)
        if not self.request.start <= now <= self.request.end + timedelta(seconds=120):
            raise ValueError("target window is closed")
        if execution and now >= self.request.end:
            raise ValueError("new fixture effects require an open window")
        self.evidence.context(self.request.scope, self.request.binding["context_version"])
        return now

    def execute(self, path: str, value: object) -> dict[str, Any]:
        command = fields(
            value,
            {
                "schema_version",
                "binding",
                "policy_sha256",
                "scenario",
                "idempotency_key",
                "trace_ids",
                "payload",
            },
        )
        scenario = next((s for s in self.policy.scenarios if s.path == path), None)
        if scenario is None:
            raise ValueError("unknown scenario")
        # Canonical byte comparison rejects bool/int aliases and any altered command field.
        expected = {
            "schema_version": 2,
            "binding": self.request.binding,
            "policy_sha256": self.policy.fingerprint,
            "scenario": scenario.id,
            "idempotency_key": digest({"run": self.request.identity, "scenario": scenario.id}),
            "trace_ids": [self.trace_id],
            "payload": scenario.payload,
        }
        if canonical(command) != canonical(expected):
            raise ValueError("command differs from independently authorized target inputs")
        self.guard()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT command,response FROM executions WHERE key=?",
                (expected["idempotency_key"],),
            ).fetchone()
            if previous:
                if previous[0] != canonical(expected):
                    raise ValueError("idempotency key conflict")
                self.guard()
                return dict(json.loads(previous[1]))
            start = self.guard(execution=True)
            result, events = self.perform(scenario.id, start)
            end = self.guard(execution=True)
            response = {
                k: expected[k]
                for k in (
                    "schema_version",
                    "binding",
                    "policy_sha256",
                    "scenario",
                    "idempotency_key",
                )
            }
            response.update(executed_at=end.isoformat(), result=result)
            # Validate the same public protocol before committing any response or telemetry.
            if len(canonical(response).encode()) > 16384:
                raise ValueError("response exceeds fixture budget")
            conn.execute(
                "INSERT INTO executions VALUES (?,?,?,?)",
                (
                    expected["idempotency_key"],
                    canonical(expected),
                    canonical(response),
                    canonical(events),
                ),
            )
            if events:
                traces, logs = self.telemetry(events, start, end)
                for signal, payload in (("traces", traces), ("logs", logs)):
                    conn.execute(
                        "INSERT INTO outbox(key,signal,payload) VALUES (?,?,?)",
                        (expected["idempotency_key"], signal, canonical(payload)),
                    )
            return response

    def perform(
        self, scenario: str, start: datetime
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if scenario == "capabilities":
            # Absence is supported by both a pinned source and a live execution of its API.
            status, body, _ = handle("GET", "/health", None)
            if (status, body) != (200, "ok"):
                raise ValueError("demo capability probe failed")
            return {
                "capabilities": dict(CAPABILITIES),
                "capabilities_sha256": digest(CAPABILITIES),
            }, []
        if scenario == "invalid-context":
            safe = True
            omitted = 0
            for context in INVALID_CONTEXTS:
                status, body, event = handle("GET", "/DEMO_SECRET_CANARY\nforged", context)
                omitted += int("trace_id" not in event)
                safe &= (
                    (status, body) == (404, "not found")
                    and set(event) == {"event", "route", "status"}
                    and "DEMO_SECRET_CANARY" not in canonical(event)
                )
            return {"omitted_contexts": omitted, "safe_logs": safe}, []
        if scenario != "http-journey":
            raise ValueError("unimplemented scenario")
        events = []
        statuses = {}
        for name, method, path, _ in JOURNEY:
            before = self.clock()
            status, body, event = handle(method, path, self.trace_id)
            after = self.clock()
            if not start <= before <= after < self.request.end:
                raise ValueError("service execution exceeded window or clock moved backwards")
            span = hashlib.sha256((self.request.identity + name).encode()).hexdigest()[:16]
            event.update(
                case=name,
                method=method,
                span_id=span,
                event_id=name,
                timestamp=after.isoformat(),
                severity="ERROR" if status >= 500 else "INFO",
                service=self.request.scope.service,
                revision=self.request.scope.revision,
                environment=self.request.scope.environment.value,
                message=body,
            )
            events.append({"body": event, "start_ns": epoch_ns(before), "end_ns": epoch_ns(after)})
            statuses[name] = status
        return {
            "completed_requests": len(events),
            "server_errors": sum(s >= 500 for s in statuses.values()),
            "statuses": statuses,
        }, events

    def telemetry(
        self, events: list[dict[str, Any]], start: datetime, end: datetime
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        root = hashlib.sha256((self.request.identity + "root").encode()).hexdigest()[:16]
        spans: list[dict[str, Any]] = [
            {
                "traceId": self.trace_id,
                "spanId": root,
                "name": "demo-fixture-journey",
                "kind": 2,
                "startTimeUnixNano": str(epoch_ns(start)),
                "endTimeUnixNano": str(epoch_ns(end)),
                "attributes": attributes(
                    {
                        "demo.sampled": True,
                        "http.request.method": "POST",
                        "http.route": "/fixtures/http-journey",
                        "http.response.status_code": 200,
                    }
                ),
            }
        ]
        values = []
        for row in events:
            event = row["body"]
            error = event["status"] >= 500
            spans.append(
                {
                    "traceId": self.trace_id,
                    "spanId": event["span_id"],
                    "parentSpanId": root,
                    "name": event["method"] + " " + event["route"],
                    "kind": 1,
                    "startTimeUnixNano": str(row["start_ns"]),
                    "endTimeUnixNano": str(row["end_ns"]),
                    "status": {"code": 2 if error else 1},
                    "attributes": attributes(
                        {
                            "http.request.method": event["method"],
                            "http.route": event["route"],
                            "http.response.status_code": event["status"],
                            "demo.case": event["case"],
                            "demo.sampled": True,
                        }
                    ),
                    "events": [
                        {
                            "name": "exception",
                            "timeUnixNano": str(row["end_ns"]),
                            "attributes": attributes(
                                {
                                    "exception.type": event["exception_type"],
                                    "exception.message": event["exception_message"],
                                    "exception.stacktrace": canonical(event["exception_stack"]),
                                }
                            ),
                        }
                    ]
                    if error
                    else [],
                }
            )
            values.append([str(row["end_ns"]), canonical(event)])
        resource = {
            **self.labels_for("tempo"),
            "service.name": self.request.scope.service,
            "service.version": self.request.scope.revision,
        }
        return (
            {
                "resourceSpans": [
                    {
                        "resource": {"attributes": attributes(resource)},
                        "scopeSpans": [{"scope": {"name": "sre-demo-fixture-v1"}, "spans": spans}],
                    }
                ]
            },
            {"streams": [{"stream": self.labels_for("loki"), "values": values}]},
        )

    def metrics(self) -> str:
        now = self.guard()
        with self.connect() as conn:
            events = [
                event
                for (raw,) in conn.execute("SELECT events FROM executions")
                for event in json.loads(raw)
            ]
            pending = conn.execute("SELECT COUNT(*) FROM outbox WHERE delivered=0").fetchone()[0]
            deliveries = conn.execute(
                "SELECT signal,SUM(attempts),SUM(failures),MAX(acknowledged_ns) "
                "FROM delivery GROUP BY signal"
            ).fetchall()
            outbox = conn.execute("SELECT signal,payload,delivered FROM outbox").fetchall()
        labels = ",".join(
            k + "=" + json.dumps(v, ensure_ascii=False)
            for k, v in sorted(self.labels_for("prometheus").items())
        )
        lines = [
            "# TYPE sre_demo_requests_total counter",
            "# TYPE sre_demo_request_duration_seconds histogram",
        ]
        # No request path, token, trace ID or arbitrary input becomes a metric label.
        for case, _, _, _ in JOURNEY:
            selected = [r for r in events if r["body"]["case"] == case]
            common = labels + ",case=" + json.dumps(case)
            durations = [(r["end_ns"] - r["start_ns"]) / 1e9 for r in selected]
            lines.append(f"sre_demo_requests_total{{{common}}} {len(selected)}")
            for bound in (0.001, 0.01, 0.1, 1.0, float("inf")):
                le = "+Inf" if bound == float("inf") else str(bound)
                lines.append(
                    f'sre_demo_request_duration_seconds_bucket{{{common},le="{le}"}} '
                    f"{sum(d <= bound for d in durations)}"
                )
            lines.append(f"sre_demo_request_duration_seconds_sum{{{common}}} {sum(durations)}")
            lines.append(f"sre_demo_request_duration_seconds_count{{{common}}} {len(durations)}")
        lines.append(f"sre_demo_outbox_pending{{{labels}}} {pending}")
        from .instrumentation import process_metrics

        measurements = {
            "sre_demo_target_info": 1,
            "sre_demo_observation_seconds": (now - self.request.start).total_seconds(),
            "sre_demo_errors_total": sum(r["body"]["status"] >= 500 for r in events),
            **process_metrics(self.process_started),
        }
        for name, value in measurements.items():
            lines.append(f"{name}{{{labels}}} {value}")
        lines.append(f'sre_demo_duration_unit_info{{{labels},unit="seconds"}} 1')
        for signal, attempts, failures, acknowledged in deliveries:
            common = labels + ",signal=" + json.dumps(signal)
            lines.extend(
                [
                    f"sre_demo_export_attempts_total{{{common}}} {attempts}",
                    f"sre_demo_export_failures_total{{{common}}} {failures}",
                ]
            )
            # Missing acknowledgement stays absent, never represented as zero latency.
            if acknowledged is not None and events:
                latency = (acknowledged - min(r["end_ns"] for r in events)) / 1e9
                lines.append(f"sre_demo_export_ack_latency_seconds{{{common}}} {latency}")
        # Audit durable payload contents against committed events; no silent discard path.
        for signal, payload, delivered in outbox:
            raw = json.loads(payload)
            actual = (
                sum(len(g["spans"]) for r in raw["resourceSpans"] for g in r["scopeSpans"])
                if signal == "traces"
                else sum(len(r["values"]) for r in raw["streams"])
            )
            intended = len(events) + 1 if signal == "traces" else len(events)
            common = labels + ",signal=" + json.dumps(signal)
            lines.append(f"sre_demo_outbox_missing_items{{{common}}} {max(0, intended - actual)}")
            lines.append(f"sre_demo_export_acknowledged{{{common}}} {delivered}")
        return "\n".join(lines) + "\n"

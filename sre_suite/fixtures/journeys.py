"""Reviewed multi-service HTTP journeys with stable tracing and at-most-once POST intent."""

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from ..api_wire import fields, text
from ..control.contracts import wire
from ..execution.config import https_url
from ..execution.evidence_client import EvidenceClient
from ..execution.http import JsonHttp, RemoteError
from ..execution.journal import Journal
from ..execution.wire import canonical, digest
from ..serialization import scope_from_dict
from ..telemetry.contracts import integer


def validate(value: object, paths: set[str]) -> dict[str, Any]:
    raw = wire(fields(value, {"origins", "routes"}))
    origins, routes = raw["origins"], raw["routes"]
    if (
        not isinstance(origins, dict)
        or not 1 <= len(origins) <= 10
        or not isinstance(routes, dict)
        or set(routes) != paths
    ):
        raise ValueError("every reviewed scenario must map to a bounded journey")
    for key, origin in origins.items():
        text(key, 64)
        fields(origin, {"base_url", "authorization_env", "ca_env"})
        base = https_url(text(origin["base_url"]), allow_loopback=origin["ca_env"] is not None)
        if urlsplit(base).path:
            raise ValueError("journey targets must be HTTPS origins")
        if not re.fullmatch(r"SRE_JOURNEY_[A-Z0-9_]+_AUTH", text(origin["authorization_env"])):
            raise ValueError("journey-only credentials required")
        if origin["ca_env"] not in {None, "SRE_JOURNEY_CA_FILE"}:
            raise ValueError("invalid journey CA variable")
    total = 0
    for steps in routes.values():
        if not isinstance(steps, list) or not 1 <= len(steps) <= 10:
            raise ValueError("bounded sequential journey required")
        total += len(steps)
        for step in steps:
            fields(step, {"origin", "method", "path", "payload", "success_status"})
            if step["origin"] not in origins or step["method"] not in {"GET", "POST"}:
                raise ValueError("unknown origin or unsupported request method")
            if (
                not re.fullmatch(r"/[A-Za-z0-9_/-]+", text(step["path"], 200))
                or "//" in step["path"]
            ):
                raise ValueError("exact relative journey route required")
            if step["method"] == "GET" and step["payload"] is not None:
                raise ValueError("GET cannot send a body")
            if step["payload"] is not None and (
                not isinstance(step["payload"], dict)
                or len(canonical(step["payload"]).encode()) > 8192
            ):
                raise ValueError("bounded fixed request payload required")
            integer(step["success_status"], 200, 299)
    if total > 40:
        raise ValueError("journey suite exceeds request budget")
    return raw


class JourneyTarget(JsonHttp):
    """Fixture adapter: does not interpolate response data into future requests or credentials."""

    def __init__(
        self,
        configuration: dict[str, Any],
        policy_sha256: str,
        env: Mapping[str, str],
        evidence: EvidenceClient,
        journal: Journal,
    ) -> None:
        self.configuration, self.policy, self.env = configuration, policy_sha256, env
        self.evidence, self.journal = evidence, journal

    def call(self, method: str, path: str, payload: object = None) -> object:
        command = wire(
            fields(
                payload,
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
        )
        if (
            method != "POST"
            or path not in self.configuration["routes"]
            or command["schema_version"] != 2
            or command["policy_sha256"] != self.policy
        ):
            raise ValueError("unreviewed journey dispatch")
        binding = command["binding"]
        scope = scope_from_dict(binding["release"]["scope"])
        from ..contracts import Environment

        if (
            scope.environment == Environment.PRODUCTION
            or binding["artifact_kind"] != "container-image-v1"
        ):
            raise ValueError("journeys require a nonproduction deployed image")
        start, end = (
            datetime.fromisoformat(binding["window_start"]),
            datetime.fromisoformat(binding["window_end"]),
        )

        def guard() -> None:
            if not start <= datetime.now(UTC) < end:
                raise ValueError("journey window closed")
            self.evidence.context(scope, binding["context_version"])

        guard()
        trace_ids = command["trace_ids"]
        if (
            not isinstance(trace_ids, list)
            or len(trace_ids) != 1
            or not re.fullmatch(r"[0-9a-f]{32}", str(trace_ids[0]))
            or int(trace_ids[0], 16) == 0
        ):
            raise ValueError("one reviewed trace ID required per journey window")
        results = []
        for index, step in enumerate(self.configuration["routes"][path]):
            key = "journey-" + digest([command, path, index, step])

            def invoke(step: dict[str, Any] = step, key: str = key) -> dict[str, Any]:
                guard()
                if step["method"] == "POST":
                    nonce = uuid4().hex
                    intent = self.journal.compute(key + "-intent", step, lambda: {"owner": nonce})
                    if intent["owner"] != nonce:
                        raise ValueError(
                            "uncertain POST outcome; reconcile before a new authorized run"
                        )
                origin = self.configuration["origins"][step["origin"]]
                authorization = self.env[origin["authorization_env"]]
                if not authorization or any(ord(c) < 32 or ord(c) > 126 for c in authorization):
                    raise ValueError("invalid journey credential")
                transport = JsonHttp(
                    origin["base_url"],
                    authorization,
                    headers={
                        "traceparent": "00-" + trace_ids[0] + "-" + digest(key)[:16] + "-01",
                        "Idempotency-Key": key,
                    },
                    timeout=5,
                    expected_status=step["success_status"],
                    local_ca_file=self.env[origin["ca_env"]] if origin["ca_env"] else None,
                )
                try:
                    body = transport.call(step["method"], step["path"], step["payload"])
                    status = step["success_status"]
                except RemoteError as error:
                    if not 400 <= error.status <= 599:
                        raise
                    status, body = error.status, None
                guard()
                if len(canonical(body).encode()) > 8192:
                    raise ValueError("journey response exceeds evidence budget")
                return {
                    "status": status,
                    "body": body,
                    "origin": step["origin"],
                    "path": step["path"],
                    "executed_at": datetime.now(UTC).isoformat(),
                }

            result = self.journal.compute(key, step, invoke, guard)
            guard()
            results.append(result)
        # Scenario expectations remain in the independently reviewed assessor policy.
        return {
            k: command[k]
            for k in ("schema_version", "binding", "policy_sha256", "scenario", "idempotency_key")
        } | {
            "executed_at": results[-1]["executed_at"],
            "result": {"steps": results, "requests": len(results)},
        }

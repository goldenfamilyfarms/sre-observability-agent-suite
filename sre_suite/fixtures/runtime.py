"""Independent runner evidence and deterministic assessment; no model-authored oracle."""

import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from ..api import evidence_id
from ..api_wire import decode, evidence_from_wire, fields, text
from ..contracts import aware
from ..execution.evidence_client import EvidenceClient
from ..execution.http import JsonHttp
from ..execution.journal import Journal
from ..execution.wire import canonical, digest
from ..verification.predicates import matches
from .contracts import FixturePolicy, FixtureRequest, Scenario


class FixtureRuntime:
    def __init__(
        self,
        policy: FixturePolicy,
        evidence: EvidenceClient,
        journal: Journal,
        *,
        target: JsonHttp | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.policy, self.evidence, self.journal = policy, evidence, journal
        self.target, self.clock = target, clock

    def guard(self, request: FixtureRequest, heartbeat: Callable[[], None]) -> None:
        heartbeat()
        request.authorize(self.policy)
        if (
            not request.start - timedelta(hours=1)
            <= self.clock()
            <= request.end + timedelta(seconds=120)
        ):
            raise ValueError("fixture dispatch is stale or too early")
        self.evidence.context(request.scope, request.binding["context_version"])
        heartbeat()

    def command(self, request: FixtureRequest, scenario: Scenario) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "binding": request.binding,
            "policy_sha256": self.policy.fingerprint,
            "scenario": scenario.id,
            "idempotency_key": digest({"run": request.identity, "scenario": scenario.id}),
            "trace_ids": sorted(
                {s.trace_id for s in self.policy.verification.sources if s.trace_id}
            ),
            "payload": scenario.payload,
        }

    def response(
        self, request: FixtureRequest, scenario: Scenario, value: object
    ) -> dict[str, Any]:
        row = fields(
            value,
            {
                "schema_version",
                "binding",
                "policy_sha256",
                "scenario",
                "idempotency_key",
                "executed_at",
                "result",
            },
        )
        cmd = self.command(request, scenario)
        if any(
            row[k] != cmd[k]
            for k in ("schema_version", "binding", "policy_sha256", "scenario", "idempotency_key")
        ):
            raise ValueError("fixture response binding mismatch")
        stamp = datetime.fromisoformat(text(row["executed_at"], 64))
        aware(stamp)
        if not request.start <= stamp <= min(request.end, self.clock()):
            raise ValueError("fixture response outside observation window")
        if not isinstance(row["result"], dict) or len(canonical(row).encode()) > 16384:
            raise ValueError("fixture response exceeds result budget")
        return dict(row)

    def execute(self, raw: object, heartbeat: Callable[[], None] = lambda: None) -> dict[str, Any]:
        request = FixtureRequest.parse(raw)
        self.guard(request, heartbeat)
        results = []
        for scenario in self.policy.scenarios:
            command = self.command(request, scenario)

            def invoke(
                scenario: Scenario = scenario, command: dict[str, Any] = command
            ) -> dict[str, Any]:
                self.guard(request, heartbeat)
                if not request.start <= self.clock() < request.end or self.target is None:
                    raise ValueError("fixture execution window is not open")
                # Save intent before POST. A lost response is retried only with the same stable key.
                self.journal.compute(
                    command["idempotency_key"] + ":intent",
                    command,
                    lambda: {"started_at": self.clock().isoformat()},
                    heartbeat,
                )
                response = self.target.call("POST", scenario.path, command)
                self.guard(request, heartbeat)
                if self.clock() > request.end:
                    raise ValueError("fixture execution exceeded its window")
                return self.response(request, scenario, response)

            result = self.journal.compute(
                command["idempotency_key"] + ":response", command, invoke, heartbeat
            )
            results.append(self.response(request, scenario, result))
        self.guard(request, heartbeat)
        document = {
            "schema_version": 2,
            "kind": "fixture-execution",
            "binding": request.binding,
            "policy_sha256": self.policy.fingerprint,
            "status": "completed",
            "scenarios": results,
        }
        saved = self.journal.compute(
            request.identity + ":execution",
            {"binding": request.binding, "dispatch": request.dispatch},
            lambda: document,
            heartbeat,
        )
        return {"workflow_id": request.identity, "artifact": digest(saved)}

    def publish(
        self, raw: object, artifact: object, heartbeat: Callable[[], None] = lambda: None
    ) -> dict[str, Any]:
        request = FixtureRequest.parse(raw)
        self.guard(request, heartbeat)
        ref = fields(artifact, {"workflow_id", "artifact"})
        if ref["workflow_id"] != request.identity or self.clock() < request.end:
            raise ValueError("fixture publication requires a completed window")
        doc = self.journal.read(request.identity + ":execution", text(ref["artifact"], 64))
        self.validate_execution(request, doc)
        content = (canonical(doc) + "\n").encode()
        evidence = self.evidence.upload(
            request.identity,
            request.scope,
            content,
            request.end,
            request.end + timedelta(minutes=15),
        )
        if evidence.producer != self.policy.runner:
            raise ValueError("incorrect runner credential")
        self.evidence.verify(evidence)
        self.guard(request, heartbeat)
        result: dict[str, Any] = json.loads(canonical(asdict(evidence)))
        return result

    def validate_execution(self, request: FixtureRequest, value: object) -> dict[str, Any]:
        doc = fields(
            value, {"schema_version", "kind", "binding", "policy_sha256", "status", "scenarios"}
        )
        if (
            doc["schema_version"] != 2
            or doc["kind"] != "fixture-execution"
            or doc["binding"] != request.binding
            or doc["policy_sha256"] != self.policy.fingerprint
            or doc["status"] != "completed"
        ):
            raise ValueError("fixture execution proof mismatch")
        rows = doc["scenarios"]
        if not isinstance(rows, list) or len(rows) != len(self.policy.scenarios):
            raise ValueError("incomplete fixture execution")
        for scenario, row in zip(self.policy.scenarios, rows, strict=True):
            self.response(request, scenario, row)
        return dict(doc)

    def assess(
        self, raw: object, reference: object, heartbeat: Callable[[], None] = lambda: None
    ) -> dict[str, Any]:
        request = FixtureRequest.parse(raw)
        self.guard(request, heartbeat)
        ref = evidence_from_wire(reference)
        if (
            self.clock() < request.end
            or ref.producer != self.policy.runner
            or not ref.valid_for(request.scope, self.clock())
            or ref.id != evidence_id(self.policy.runner, request.scope, request.identity)
            or ref.observed_at != request.end
        ):
            raise ValueError("untrusted fixture execution reference")
        execution = self.validate_execution(request, decode(self.evidence.read(ref)))
        for scenario, response in zip(self.policy.scenarios, execution["scenarios"], strict=True):
            heartbeat()
            if not matches(response["result"], scenario.assertions):
                raise ValueError("independent fixture assertion failed")
        rules = {r.id: r for r in self.policy.verification.rules}
        # Expected telemetry values are fixed by reviewed policy, never copied from observations.
        checks = {
            key: {"applicable": rules[key].applicable, **assessment}
            for key, assessment in self.policy.checks.items()
        }
        doc = {
            "schema_version": 2,
            "kind": "certification-fixture",
            **request.binding,
            "checks": checks,
            "execution": {"policy_sha256": self.policy.fingerprint, "evidence": asdict(ref)},
        }
        saved = self.journal.compute(
            request.identity + ":assessment",
            {"request": request.binding, "runner": reference},
            lambda: json.loads(canonical(doc)),
            heartbeat,
        )
        self.guard(request, heartbeat)
        evidence = self.evidence.upload(
            request.identity + "-assessment",
            request.scope,
            (canonical(saved) + "\n").encode(),
            request.end,
            min(ref.expires_at, request.end + timedelta(minutes=15)),
        )
        if evidence.producer != self.policy.verification.assessor:
            raise ValueError("incorrect assessor credential")
        self.evidence.verify(evidence)
        self.guard(request, heartbeat)
        return {
            "outcome": "assessed",
            "workflow_id": request.identity,
            "evidence": json.loads(canonical(asdict(evidence))),
        }

"""Reviewed certification handoff and current evidence gate for engineering executions."""

import json
import re
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..api_wire import decode, envelope, evidence_from_wire, fields, text
from ..contracts import Release
from ..telemetry.schedule_contracts import TelemetrySchedule
from ..verification.contracts import VerificationInput, VerificationPolicy
from ..verification.runtime import CertificationEngine, identity
from .wire import canonical, digest

if TYPE_CHECKING:
    from .runtime import ExecutionRuntime
    from .telemetry import EngineeringTelemetry


class EngineeringCertification:
    def __init__(self, path: Path, telemetry: "EngineeringTelemetry | None") -> None:
        from ..verification.config import private_document

        row = envelope(
            private_document(path, 1_048_576), {"policy", "task_queue", "fixture_wait_seconds"}
        )
        self.policy = VerificationPolicy.parse(row["policy"])
        self.queue = text(row["task_queue"], 80)
        self.wait = row["fixture_wait_seconds"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", self.queue):
            raise ValueError("invalid certification queue")
        if type(self.wait) is not int or not 1 <= self.wait <= 600:
            raise ValueError("fixture wait must be 1..600 seconds")
        if telemetry is None or self.policy.scope != telemetry.policy.scope:
            raise ValueError("certification requires matching engineering telemetry")
        if self.policy.collector != telemetry.producer:
            raise ValueError("certification collector differs from engineering telemetry")
        expected = {p.id: p for p in telemetry.policy.profiles}
        selected = dict(telemetry.profiles)
        if {s.profile.id for s in self.policy.sources} != set(selected):
            raise ValueError("certification sources must exactly cover engineering collections")
        for source in self.policy.sources:
            if (
                source.profile != expected[source.profile.id]
                or source.trace_id != selected[source.profile.id]
                or source.step_seconds != 15
                or source.limit != 100
            ):
                raise ValueError("certification source differs from engineering request")
        self.fingerprint = digest(
            {"policy": self.policy.fingerprint, "queue": self.queue, "wait": self.wait}
        )

    def plan(
        self, runtime: "ExecutionRuntime", arguments: dict[str, Any], heartbeat: Callable[[], None]
    ) -> dict[str, Any]:
        reference = arguments["reference"]
        manifest = runtime._guard(reference, heartbeat)
        telemetry = runtime.prepare_telemetry(arguments, heartbeat)
        schedules = [TelemetrySchedule.parse(e["schedule"]) for e in telemetry["schedules"]]
        start, end = schedules[0].request.start, schedules[0].request.end
        if any((s.request.start, s.request.end) != (start, end) for s in schedules):
            raise ValueError("certification requires a single exact observation window")
        release = Release(
            self.policy.scope,
            telemetry["binding"]["artifact_digest"],
            telemetry["binding"]["plan_digest"],
            ("certification",),
        )
        document = json.loads(
            canonical(
                {
                    "release": asdict(release),
                    "artifact_kind": telemetry["artifact_kind"],
                    "window_start": start.isoformat(),
                    "window_end": end.isoformat(),
                    "context_version": manifest["context"]["version"],
                    "policy_sha256": self.policy.fingerprint,
                    "catalog_sha256": self.policy.catalog_sha256,
                    "task_queue": self.queue,
                    "fixture_wait_seconds": self.wait,
                    "telemetry_plan": telemetry["digest"],
                }
            )
        )
        saved = runtime.journal.compute(
            reference["key"] + ":certification-plan",
            {"reference": reference, "policy": self.fingerprint, "telemetry": telemetry["digest"]},
            lambda: document,
            heartbeat,
        )
        if saved != document:
            raise ValueError("certification plan changed")
        return {**saved, "digest": digest(saved)}

    def bind(
        self, runtime: "ExecutionRuntime", arguments: dict[str, Any], heartbeat: Callable[[], None]
    ) -> dict[str, Any]:
        plan = arguments["certification_plan"]
        saved = runtime.journal.read(
            arguments["reference"]["key"] + ":certification-plan", text(plan["digest"], 64)
        )
        if {k: v for k, v in plan.items() if k != "digest"} != saved:
            raise ValueError("certification handoff plan mismatch")
        runtime._guard(arguments["reference"], heartbeat)
        stages = runtime._results(arguments["reference"], arguments["results"], heartbeat)
        if len(stages) != 4 or any(s["verdict"] != "passed" for s in stages):
            raise ValueError("certification requires all engineering gates")
        assert runtime.telemetry is not None
        telemetry = runtime.telemetry.verify(runtime, arguments, heartbeat)
        if not telemetry["ready"]:
            raise ValueError("certification requires complete engineering telemetry")
        fixture = evidence_from_wire(arguments["certification_fixture"])
        start, end = (
            datetime.fromisoformat(plan["window_start"]),
            datetime.fromisoformat(plan["window_end"]),
        )
        if (
            fixture.producer != self.policy.assessor
            or not fixture.valid_for(self.policy.scope, runtime.clock())
            or not end <= fixture.observed_at <= end + timedelta(seconds=120)
        ):
            raise ValueError("invalid independent fixture reference")
        doc = decode(runtime.evidence.read(fixture))
        expected = {
            k: plan[k]
            for k in (
                "release",
                "artifact_kind",
                "window_start",
                "window_end",
                "context_version",
                "policy_sha256",
                "catalog_sha256",
            )
        }
        if (
            not isinstance(doc, dict)
            or doc.get("schema_version") != 2
            or doc.get("kind") != "certification-fixture"
            or any(digest(doc.get(k)) != digest(v) for k, v in expected.items())
        ):
            raise ValueError("fixture does not attest the exact engineering plan")
        observations = []
        for entry, report in zip(
            arguments["telemetry"]["plan"]["schedules"],
            arguments["telemetry"]["reports"],
            strict=True,
        ):
            schedule = TelemetrySchedule.parse(entry["schedule"])
            observations.append(
                (
                    schedule.request.profile,
                    evidence_from_wire(report["collections"][0]["evidence"]),
                )
            )
        release = Release(
            self.policy.scope,
            plan["release"]["artifact_digest"],
            plan["release"]["plan_digest"],
            ("certification",),
        )
        value = VerificationInput(
            release,
            plan["artifact_kind"],
            start,
            end,
            plan["context_version"],
            self.policy.fingerprint,
            fixture,
            tuple(observations),
        )
        request = {
            "input": value.wire(),
            "id": identity(value),
            "task_queue": self.queue,
            "plan": plan["digest"],
        }
        saved = runtime.journal.compute(
            arguments["reference"]["key"] + ":certification-input",
            request,
            lambda: request,
            heartbeat,
        )
        runtime._guard(arguments["reference"], heartbeat)
        return saved

    def verify(
        self, runtime: "ExecutionRuntime", arguments: dict[str, Any], heartbeat: Callable[[], None]
    ) -> dict[str, Any]:
        bundle = fields(arguments.get("certification"), {"request", "receipt"})
        receipt = bundle["receipt"]
        # Failure receipts convey no authority; never convert a child failure into a passing gate.
        if not isinstance(receipt, dict) or receipt.get("verdict") not in {
            "PASSED",
            "FAILED",
            "INCOMPLETE",
        }:
            raise ValueError("missing certification outcome")
        if receipt["verdict"] != "PASSED":
            return {"ready": False, "verdict": receipt["verdict"], "evidence": None}
        request = self.bind(runtime, arguments, heartbeat)
        if bundle["request"] != request:
            raise ValueError("certificate does not match saved engineering input")
        engine = CertificationEngine(
            self.policy, runtime.evidence, runtime.journal, clock=runtime.clock
        )
        gate = engine.gate(request["input"], receipt)
        runtime._guard(arguments["reference"], heartbeat)
        summary: dict[str, Any] = json.loads(
            canonical(
                {
                    "ready": gate.passed,
                    "verdict": "PASSED",
                    "evidence": asdict(gate.evidence),
                    "release": asdict(gate.release),
                    "workflow_id": request["id"],
                    "policy_sha256": self.policy.fingerprint,
                }
            )
        )
        return summary

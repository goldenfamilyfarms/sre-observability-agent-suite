"""Operator-authorized candidate observations; no deployment or certification authority."""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ..api import evidence_id
from ..api_wire import decode, envelope, evidence_from_wire, fields, text
from ..serialization import scope_from_dict
from ..telemetry.contracts import (
    Backend,
    CandidateBinding,
    QueryPolicy,
    QueryProfile,
    QueryRequest,
    epoch_ns,
    integer,
    profile_document,
)
from ..telemetry.schedule_contracts import TelemetrySchedule, policy_digest
from .wire import canonical, digest

if TYPE_CHECKING:
    from .runtime import ExecutionRuntime


def validate_observed(result: dict[str, Any], profile: QueryProfile, request: QueryRequest) -> None:
    """Recheck returned attribution; an `observed` receipt alone is not evidence."""
    expected = profile.bindings(request.scope, request.binding)
    if profile.backend == Backend.TEMPO:
        from ..telemetry.traces import full_trace

        raw_trace = result.get("full_trace")
        if not isinstance(raw_trace, dict) or not isinstance(raw_trace.get("otlp_json"), str):
            raise ValueError("full trace evidence required")
        trace, issues = full_trace(
            {"trace": decode(raw_trace["otlp_json"].encode())}, profile, request
        )
        if issues or digest(asdict(trace)) != digest(raw_trace):
            raise ValueError("full trace evidence is incomplete or changed")
        return
    rows = result.get("metrics" if profile.backend == Backend.PROMETHEUS else "logs")
    if not isinstance(rows, list) or not rows:
        raise ValueError("observed evidence must contain measurements")
    for row in rows:
        labels = dict(row["labels"])
        if any(labels.get(key) != value for key, value in expected.items()):
            raise ValueError("returned telemetry candidate does not match")
        samples = row["samples"] if profile.backend == Backend.PROMETHEUS else [row]
        if not isinstance(samples, list) or not samples:
            raise ValueError("observed series has no samples")
        for sample in samples:
            timestamp = integer(sample["timestamp_ns"], 0, 2**63 - 1)
            if not epoch_ns(request.start) <= timestamp <= epoch_ns(request.end):
                raise ValueError("sample lies outside planned observation window")


class EngineeringTelemetry:
    def __init__(self, path: Path) -> None:
        from ..local_pilot.config import private_json
        from ..telemetry.schedule_config import private_path

        private_path(path, directory=False)
        row = envelope(
            private_json(path),
            {"policy", "profiles", "task_queue", "delay_seconds", "window_seconds", "producer"},
        )
        self.policy = QueryPolicy.parse(row["policy"])
        self.queue = text(row["task_queue"], 100)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", self.queue):
            raise ValueError("invalid telemetry task queue")
        self.delay = integer(row["delay_seconds"], 0, 1800)
        self.window = integer(row["window_seconds"], 1, 300)
        self.producer = text(row["producer"], 200)
        profiles = row["profiles"]
        if not isinstance(profiles, list) or not 1 <= len(profiles) <= 3:
            raise ValueError("require one to three reviewed telemetry profiles")
        self.profiles: list[tuple[str, str | None]] = []
        backends = set()
        for item in profiles:
            selection = fields(item, {"profile", "trace_id"})
            name = text(selection["profile"], 64)
            trace_id = None if selection["trace_id"] is None else text(selection["trace_id"], 32)
            profile = next((p for p in self.policy.profiles if p.id == name), None)
            if profile is None or not profile.binding_labels or profile.backend in backends:
                raise ValueError("require unique backends with candidate digest mappings")
            if profile.backend == Backend.TEMPO:
                if (
                    not profile.allow_full_traces
                    or trace_id is None
                    or not re.fullmatch(r"[0-9a-f]{32}", trace_id)
                    or int(trace_id, 16) == 0
                ):
                    raise ValueError("engineering telemetry requires an authorized full trace ID")
            elif trace_id is not None:
                raise ValueError("trace ID only applies to Tempo")
            backends.add(profile.backend)
            self.profiles.append((name, trace_id))
        self.fingerprint = digest(
            {
                "policy": policy_digest(self.policy),
                "profiles": self.profiles,
                "queue": self.queue,
                "delay": self.delay,
                "window": self.window,
                "producer": self.producer,
                "binding_version": 1,
            }
        )

    def prepare(
        self, runtime: "ExecutionRuntime", arguments: dict[str, Any], heartbeat: Callable[[], None]
    ) -> dict[str, Any]:
        reference, results = arguments["reference"], arguments["results"]
        manifest = runtime._guard(reference, heartbeat)
        loaded = runtime._results(reference, results, heartbeat)
        if len(loaded) != 4 or any(r["verdict"] != "passed" for r in loaded):
            raise ValueError("telemetry requires all four engineering stages")
        scope = scope_from_dict(manifest["ticket"]["scope"])
        if scope != self.policy.scope:
            raise ValueError("telemetry policy differs from execution scope")
        recipe = {
            "kind": "source-candidate-v1",
            "repository": runtime.settings.repository,
            "base_revision": scope.revision,
            "edits": sorted(loaded[0]["edits"], key=lambda edit: edit["path"]),
        }
        binding = CandidateBinding("sha256:" + digest(recipe), "sha256:" + reference["manifest"])
        request = {"reference": reference, "results": results, "policy": self.fingerprint}

        def build() -> dict[str, Any]:
            pull_request = None

            def guard() -> None:
                runtime._guard(reference, heartbeat)
                runtime._results(reference, results, heartbeat)

            # The draft may feed separately authorized preview automation. This never deploys.
            if runtime.settings.github_prs:
                with runtime.journal.locked(reference["key"] + ":publication", heartbeat):
                    pull_request = runtime.github.publish(
                        reference["key"],
                        reference["manifest"],
                        results[0]["artifact"],
                        scope,
                        loaded[0],
                        guard,
                        heartbeat,
                    )
            start = runtime.clock().replace(microsecond=0) + timedelta(seconds=self.delay + 1)
            end = start + timedelta(seconds=self.window)
            if end + timedelta(seconds=120) >= min(
                datetime.fromisoformat(stage["expires_at"]) for stage in loaded
            ):
                raise ValueError("candidate evidence expires before collection can finish")
            schedules = []
            for name, trace_id in self.profiles:
                query = QueryRequest(scope, name, start, end, trace_id=trace_id, binding=binding)
                self.policy.resolve(query)
                schedule = TelemetrySchedule(
                    "engineering-" + digest({"execution": reference["key"], "profile": name})[:48],
                    policy_digest(self.policy),
                    query,
                    end,
                    1,
                    1,
                    120,
                )
                schedules.append({"id": schedule.workflow_id, "schedule": schedule.wire()})
            guard()
            result: dict[str, Any] = json.loads(
                canonical(
                    {
                        "binding": asdict(binding),
                        "artifact_kind": recipe["kind"],
                        "manifest": reference["manifest"],
                        "implementation": results[0]["artifact"],
                        "task_queue": self.queue,
                        "schedules": schedules,
                        "pull_request": pull_request,
                    }
                )
            )

            return result

        plan = runtime.journal.compute(
            reference["key"] + ":telemetry-plan", request, build, heartbeat
        )
        runtime._guard(reference, heartbeat)
        runtime._results(reference, results, heartbeat)
        if plan["pull_request"] is not None:
            runtime.github.verify(cast(dict[str, Any], plan["pull_request"]), heartbeat)
        return {**plan, "digest": digest(plan)}

    def verify(
        self, runtime: "ExecutionRuntime", arguments: dict[str, Any], heartbeat: Callable[[], None]
    ) -> dict[str, Any]:
        reference = arguments["reference"]
        telemetry = fields(arguments.get("telemetry"), {"plan", "reports"})
        plan = fields(
            telemetry["plan"],
            {
                "binding",
                "artifact_kind",
                "manifest",
                "implementation",
                "task_queue",
                "schedules",
                "pull_request",
                "digest",
            },
        )
        saved = runtime.journal.read(reference["key"] + ":telemetry-plan", text(plan["digest"], 64))
        if digest({k: v for k, v in plan.items() if k != "digest"}) != digest(saved):
            raise ValueError("telemetry plan changed")
        manifest = runtime._guard(reference, heartbeat)
        loaded = runtime._results(reference, arguments["results"], heartbeat)
        recipe = {
            "kind": "source-candidate-v1",
            "repository": runtime.settings.repository,
            "base_revision": self.policy.scope.revision,
            "edits": sorted(loaded[0]["edits"], key=lambda edit: edit["path"]),
        }
        expected = CandidateBinding("sha256:" + digest(recipe), "sha256:" + reference["manifest"])
        if (
            plan["binding"] != asdict(expected)
            or plan["manifest"] != reference["manifest"]
            or plan["implementation"] != arguments["results"][0]["artifact"]
        ):
            raise ValueError("telemetry does not bind the reviewed artifact and plan")
        reports = telemetry["reports"]
        if not isinstance(reports, list) or len(reports) != len(saved["schedules"]):
            raise ValueError("missing telemetry child outcomes")
        complete = True
        references = []
        for entry, report in zip(saved["schedules"], reports, strict=True):
            heartbeat()
            schedule = TelemetrySchedule.parse(entry["schedule"])
            schedule.authorize(self.policy)
            request = schedule.request
            if request.binding != expected or request.scope != self.policy.scope:
                raise ValueError("candidate schedule mismatch")
            if not isinstance(report, dict) or report.get("outcome") not in {
                "blocked",
                "collected",
            }:
                raise ValueError("invalid telemetry child outcome")
            receipts = report.get("collections")
            if not isinstance(receipts, list) or len(receipts) > 1:
                raise ValueError("invalid telemetry collections")
            if report["outcome"] != "collected" or len(receipts) != 1:
                complete = False
            for receipt in receipts:
                row = fields(
                    receipt, {"slot", "status", "evidence_id", "evidence_sha256", "evidence"}
                )
                evidence = evidence_from_wire(row["evidence"])
                if (
                    type(row["slot"]) is not int
                    or row["slot"] != 0
                    or not evidence.valid_for(request.scope, runtime.clock())
                    or evidence.producer != self.producer
                    or row["evidence_id"] != evidence.id
                    or row["evidence_sha256"] != evidence.sha256
                    or evidence.id
                    != evidence_id(self.producer, request.scope, "telemetry-" + evidence.sha256)
                ):
                    raise ValueError("telemetry evidence reference mismatch")
                document = decode(runtime.evidence.read(evidence))
                if not isinstance(document, dict):
                    raise ValueError("invalid telemetry evidence")
                profile = self.policy.resolve(request)
                query = profile.query(request.scope, request.binding)
                hashed_query = (
                    canonical({"trace_id": request.trace_id, "scope_selector": query})
                    if request.trace_id
                    else query
                )
                result = document.get("result")
                if (
                    document.get("schema_version") != 2
                    or document.get("kind") != "telemetry-observation"
                    or digest(document.get("request")) != digest(asdict(request))
                    or digest(document.get("profile")) != digest(profile_document(profile))
                    or document.get("rendered_query") != query
                    or document.get("context_version") != manifest["context"]["version"]
                    or document.get("certification_verdict") is not None
                    or document.get("observed_at") != evidence.observed_at.isoformat()
                    or evidence.expires_at != evidence.observed_at + timedelta(minutes=15)
                    or not request.end
                    <= evidence.observed_at
                    <= request.end + timedelta(seconds=120)
                    or not isinstance(result, dict)
                    or result.get("backend") != profile.backend
                    or result.get("query_sha256")
                    != hashlib.sha256(hashed_query.encode()).hexdigest()
                    or result.get("status") != row["status"]
                    or row["status"] not in {"observed", "partial", "empty"}
                ):
                    raise ValueError("telemetry document does not attest the planned observation")
                runtime.evidence.context(request.scope, manifest["context"]["version"])
                if row["status"] != "observed" or result.get("issues") != []:
                    complete = False
                else:
                    validate_observed(result, profile, request)
                references.append(row["evidence"])
        if plan["pull_request"] is not None:
            runtime.github.verify(cast(dict[str, Any], plan["pull_request"]), heartbeat)
        return {
            "ready": complete,
            "binding": plan["binding"],
            "artifact_kind": plan["artifact_kind"],
            "plan": plan["digest"],
            "evidence": references,
            "pull_request": plan["pull_request"],
            "certification_verdict": None,
        }

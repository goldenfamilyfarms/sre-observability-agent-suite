"""Reviewed scenarios, independent expected values and exact release/window dispatch."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from ..api_wire import envelope, fields, text
from ..certification import load_catalog
from ..contracts import Environment, Release, Scope, aware
from ..execution.config import https_url
from ..execution.wire import canonical, digest
from ..serialization import scope_from_dict
from ..telemetry.contracts import integer
from ..verification.contracts import VerificationPolicy
from ..verification.predicates import number, selection

BINDING_FIELDS = {
    "release",
    "artifact_kind",
    "window_start",
    "window_end",
    "context_version",
    "policy_sha256",
    "catalog_sha256",
}


def queue(value: object) -> str:
    result = text(value, 80)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", result):
        raise ValueError("invalid fixture queue")
    return result


def dispatch(value: object) -> dict[str, Any]:
    row = fields(value, {"policy_sha256", "runner_queue", "task_queue"})
    if not re.fullmatch(r"[0-9a-f]{64}", text(row["policy_sha256"], 64)):
        raise ValueError("invalid fixture policy digest")
    result = {
        "policy_sha256": row["policy_sha256"],
        "runner_queue": queue(row["runner_queue"]),
        "task_queue": queue(row["task_queue"]),
    }
    if result["runner_queue"] == result["task_queue"]:
        raise ValueError("runner and assessor require separate queues")
    return result


@dataclass(frozen=True)
class Scenario:
    id: str
    path: str
    payload: dict[str, Any]
    assertions: dict[str, object]


@dataclass(frozen=True)
class FixturePolicy:
    verification: VerificationPolicy
    runner: str
    base_url: str
    authorization_env: str
    ca_env: str | None
    runner_queue: str
    assessor_queue: str
    scenarios: tuple[Scenario, ...]
    checks: dict[str, Any]
    fingerprint: str
    journeys: dict[str, Any] | None = None
    target_service: dict[str, Any] | None = None

    @property
    def dispatch(self) -> dict[str, Any]:
        return {
            "policy_sha256": self.fingerprint,
            "runner_queue": self.runner_queue,
            "task_queue": self.assessor_queue,
        }

    @classmethod
    def parse(cls, value: object) -> "FixturePolicy":
        row = envelope(
            value,
            {
                "verification_policy",
                "runner",
                "target",
                "runner_queue",
                "assessor_queue",
                "scenarios",
                "checks",
            }
            | (
                {k for k in ("journeys", "target_service") if k in value}
                if isinstance(value, dict)
                else set()
            ),
        )
        policy = VerificationPolicy.parse(row["verification_policy"])
        runner = text(row["runner"], 128)
        if policy.scope.environment == Environment.PRODUCTION or runner in {
            policy.collector,
            policy.assessor,
            policy.verifier,
        }:
            raise ValueError("fixtures require non-production scope and an independent runner")
        target = fields(row["target"], {"base_url", "authorization_env", "ca_env"})
        auth = text(target["authorization_env"], 80)
        if not re.fullmatch(r"SRE_FIXTURE_TARGET_[A-Z0-9_]*AUTH", auth):
            raise ValueError("use a fixture-only authorization variable")
        ca = None if target["ca_env"] is None else text(target["ca_env"], 80)
        if ca not in {None, "SRE_FIXTURE_TARGET_CA_FILE"}:
            raise ValueError("invalid fixture CA variable")
        base = https_url(text(target["base_url"]), allow_loopback=ca is not None)
        if urlsplit(base).path or (
            ca and urlsplit(base).hostname not in {"127.0.0.1", "localhost"}
        ):
            raise ValueError("target must be one HTTPS origin; custom CAs are loopback-only")
        runner_queue, assessor_queue = queue(row["runner_queue"]), queue(row["assessor_queue"])
        if runner_queue == assessor_queue:
            raise ValueError("independent queues required")
        items = row["scenarios"]
        if not isinstance(items, list) or not 1 <= len(items) <= 40:
            raise ValueError("require 1..40 bounded fixture scenarios")
        scenarios = []
        for item in items:
            entry = fields(item, {"id", "path", "payload", "assertions"})
            name, path = text(entry["id"], 64), text(entry["path"], 200)
            if (
                not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name)
                or not re.fullmatch(r"/[A-Za-z0-9_/-]+", path)
                or "//" in path
            ):
                raise ValueError("invalid scenario identity or exact endpoint")
            payload = entry["payload"]
            if not isinstance(payload, dict) or len(canonical(payload).encode()) > 8192:
                raise ValueError("fixture payload must be a bounded JSON object")
            assertions = selection(entry["assertions"])
            if not assertions:
                raise ValueError("every scenario requires independent response assertions")
            scenarios.append(Scenario(name, path, payload, assertions))
        names = {s.id for s in scenarios}
        if len(names) != len(scenarios):
            raise ValueError("duplicate fixture scenario")
        checks = fields(row["checks"], {c.id for c in load_catalog().checks})
        rules = {r.id: r for r in policy.rules}
        used = set()
        for check in load_catalog().checks:
            assessment = fields(checks[check.id], {"scenario", "expected", "justification"})
            name = text(assessment["scenario"], 64)
            if name not in names:
                raise ValueError("every check requires an executed scenario or capability probe")
            used.add(name)
            expected = assessment["expected"]
            if (
                not isinstance(expected, list)
                or len(expected) > 500
                or len(canonical(expected).encode()) > 8192
            ):
                raise ValueError("invalid independent expectations")
            if (
                not isinstance(assessment["justification"], str)
                or len(assessment["justification"]) > 512
            ):
                raise ValueError("invalid applicability justification")
            if not rules[check.id].applicable:
                text(assessment["justification"], 512)
                if expected:
                    raise ValueError("not-applicable checks cannot assert telemetry expectations")
            elif check.assertion in {"coverage.v1", "links.v1", "metric_expected.v1"}:
                if not expected:
                    raise ValueError("independent oracle is required")
                if check.assertion == "metric_expected.v1":
                    for v in expected:
                        number(v)
                elif check.assertion == "links.v1":
                    for v in expected:
                        pair = fields(v, {"trace_id", "span_id"})
                        for field, size in (("trace_id", 32), ("span_id", 16)):
                            if (
                                not re.fullmatch(
                                    r"[0-9a-f]{" + str(size) + "}", text(pair[field], size)
                                )
                                or int(str(pair[field]), 16) == 0
                            ):
                                raise ValueError("invalid expected trace link")
        if used != names:
            raise ValueError("unmapped scenarios are not authorized")
        journeys = None
        if "journeys" in row:
            from .journeys import validate as validate_journeys

            journeys = validate_journeys(row["journeys"], {s.path for s in scenarios})
        target_service = None
        if "target_service" in row:
            from .target_service import validate as validate_target

            if journeys is None:
                raise ValueError("target service proof requires reviewed journeys")
            target_service = validate_target(
                row["target_service"], runner, policy.assessor, policy.collector, policy.verifier
            )
        return cls(
            policy,
            runner,
            base,
            auth,
            ca,
            runner_queue,
            assessor_queue,
            tuple(scenarios),
            dict(checks),
            digest(row),
            journeys,
            target_service,
        )


@dataclass(frozen=True)
class FixtureRequest:
    binding: dict[str, Any]
    dispatch: dict[str, Any]
    scope: Scope
    start: datetime
    end: datetime
    demo: dict[str, Any] | None = None

    @classmethod
    def parse(cls, value: object) -> "FixtureRequest":
        raw = fields(value, {"plan", "dispatch"})
        plan = raw["plan"]
        if not isinstance(plan, dict) or not BINDING_FIELDS <= plan.keys():
            raise ValueError("missing exact certification plan")
        # Exported engineering plans carry an immutable digest; standalone bindings may omit it.
        if "digest" in plan and plan["digest"] != digest(
            {k: v for k, v in plan.items() if k != "digest"}
        ):
            raise ValueError("certification plan changed")
        routing = dispatch(raw["dispatch"])
        if "fixture" in plan and plan["fixture"] != routing:
            raise ValueError("fixture routing differs from engineering plan")
        release = fields(
            plan["release"], {"scope", "artifact_digest", "plan_digest", "required_gates"}
        )
        scope = scope_from_dict(release["scope"])
        if (
            release["required_gates"] != ["certification"]
            or plan["artifact_kind"] not in {"source-candidate-v1", "container-image-v1"}
            or scope.environment == Environment.PRODUCTION
        ):
            raise ValueError("unsupported fixture release")
        Release(
            scope,
            text(release["artifact_digest"]),
            text(release["plan_digest"]),
            ("certification",),
        )
        start, end = (
            datetime.fromisoformat(text(plan[k], 64)) for k in ("window_start", "window_end")
        )
        aware(start)
        aware(end)
        if not timedelta(0) < end - start <= timedelta(seconds=300):
            raise ValueError("fixture window must be at most five minutes")
        integer(plan["context_version"], 0, 2**63 - 1)
        for k in ("policy_sha256", "catalog_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", text(plan[k], 64)):
                raise ValueError("invalid policy digest")
        from ..demo_fixture.managed_contracts import demo_dispatch

        demo = demo_dispatch(plan["demo"]) if "demo" in plan else None
        if demo and (end - start).total_seconds() <= demo["settle_seconds"]:
            raise ValueError("demo window must leave time for export and settlement")
        return cls({k: plan[k] for k in sorted(BINDING_FIELDS)}, routing, scope, start, end, demo)

    @property
    def identity(self) -> str:
        return "sre-fixture-" + digest(
            {
                "binding": self.binding,
                "dispatch": self.dispatch,
                **({"demo": self.demo} if self.demo else {}),
            }
        )

    def authorize(self, policy: FixturePolicy) -> None:
        if (
            self.scope != policy.verification.scope
            or self.dispatch != policy.dispatch
            or self.binding["policy_sha256"] != policy.verification.fingerprint
            or self.binding["catalog_sha256"] != policy.verification.catalog_sha256
        ):
            raise ValueError("fixture request is outside reviewed policy")

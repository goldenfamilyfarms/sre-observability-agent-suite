"""Reviewed demo stimuli and complete catalog-to-scenario mapping, not a telemetry oracle."""

import copy
from typing import Any

from ..api_wire import fields
from ..certification import load_catalog
from ..execution.wire import digest
from ..fixtures.contracts import FixturePolicy
from ..verification.contracts import VerificationPolicy
from .service import CAPABILITIES

JOURNEY = (
    ("health", "GET", "/health", 200),
    ("not-found", "GET", "/missing", 404),
    ("method", "POST", "/health", 405),
    ("error", "GET", "/error", 500),
    ("unicode", "GET", "/unicode", 200),
    ("redaction", "GET", "/DEMO_SECRET_CANARY\nforged", 404),
)
INVALID_CONTEXTS: tuple[object, ...] = (
    None,
    "",
    "0" * 32,
    "a" * 31,
    "a" * 33,
    "A" * 32,
    "g" * 32,
    "a" * 32 + "\n",
    "DEMO_SECRET_CANARY\nforged",
    42,
    True,
    [],
    {},
)


def scenarios() -> list[dict[str, Any]]:
    return [
        {
            "id": "capabilities",
            "path": "/fixtures/capabilities",
            "payload": {},
            "assertions": {"/capabilities_sha256": digest(CAPABILITIES)},
        },
        {
            "id": "http-journey",
            "path": "/fixtures/http-journey",
            "payload": {},
            "assertions": {
                "/completed_requests": 6,
                "/server_errors": 1,
                **{f"/statuses/{case}": status for case, _, _, status in JOURNEY},
            },
        },
        {
            "id": "invalid-context",
            "path": "/fixtures/invalid-context",
            "payload": {},
            "assertions": {"/omitted_contexts": len(INVALID_CONTEXTS), "/safe_logs": True},
        },
    ]


# These are absent features of the pinned implementation, never a fallback for missing data.
ABSENT = {
    "outbound_http": ["TRACES-007", "TRACES-008", "TRACES-025"],
    "grpc": ["TRACES-009", "TRACES-010", "TRACES-011", "LOGS-015"],
    "database": [
        *[f"TRACES-{n:03}" for n in range(12, 16)],
        "LOGS-029",
        *[f"PROMQL-{n:03}" for n in range(29, 33)],
    ],
    "cache": ["TRACES-016", "PROMQL-033"],
    "messaging": [
        *[f"TRACES-{n:03}" for n in range(17, 23)],
        *[f"LOGS-{n:03}" for n in range(16, 20)],
        *[f"PROMQL-{n:03}" for n in range(24, 29)],
    ],
    "async": ["TRACES-023"],
    "retries": ["TRACES-033", "LOGS-023"],
    "timeouts": ["TRACES-034", "LOGS-024"],
    "cancellation": ["TRACES-035", "LOGS-025"],
    "jvm": [f"PROMQL-{n:03}" for n in range(16, 20)],
}


def matrix() -> dict[str, dict[str, Any]]:
    absent = {check: capability for capability, checks in ABSENT.items() for check in checks}
    result = {}
    for check in load_catalog().checks:
        feature = absent.get(check.id)
        security = check.id in {"TRACES-037", "LOGS-026", "LOGS-027", "LOGS-028", "LOGS-030"}
        result[check.id] = {
            "concern": check.title,
            "scenario": "capabilities"
            if feature
            else "invalid-context"
            if security
            else "http-journey",
            "applicable": feature is None,
            "justification": (
                f"Pinned Python demo has no {feature} capability; "
                "executed capability probe must confirm absence."
                if feature
                else ""
            ),
            "expectation_required": feature is None
            and check.assertion in {"coverage.v1", "links.v1", "metric_expected.v1"},
        }
    return result


def prepare_policy(
    verification: object, expectations: object, target: object, runner: str
) -> dict[str, Any]:
    """Bind reviewed stimuli to independently reviewed private rules and fixed oracles.

    Retention, exporter, scrape and SLO checks stay applicable. The controller cannot
    certify those concerns; the verifier must receive real matching backend evidence.
    """
    policy = VerificationPolicy.parse(verification)
    validate_sources(policy)
    mapping = matrix()
    expected = fields(expectations, {k for k, v in mapping.items() if v["expectation_required"]})
    for rule in policy.rules:
        if rule.applicable != mapping[rule.id]["applicable"]:
            raise ValueError("applicability differs from reviewed demo capability matrix")
    raw = {
        "schema_version": 2,
        "verification_policy": copy.deepcopy(verification),
        "runner": runner,
        "target": copy.deepcopy(target),
        "runner_queue": "sre-demo-fixture-runner-v1",
        "assessor_queue": "sre-demo-fixture-assessor-v1",
        "scenarios": scenarios(),
        "checks": {
            k: {
                "scenario": v["scenario"],
                "expected": copy.deepcopy(expected.get(k, [])),
                "justification": v["justification"],
            }
            for k, v in mapping.items()
        },
    }
    FixturePolicy.parse(raw)
    return raw


def reviewed(policy: FixturePolicy) -> None:
    """Reject an edited stimulus/assertion map at the target boundary as well as the runner."""
    from dataclasses import asdict

    validate_sources(policy.verification)
    mapping = matrix()
    if [asdict(s) for s in policy.scenarios] != scenarios():
        raise ValueError("demo scenarios differ from reviewed contract")
    for rule in policy.verification.rules:
        item = mapping[rule.id]
        check = policy.checks[rule.id]
        if rule.applicable != item["applicable"] or any(
            check[k] != item[k] for k in ("scenario", "justification")
        ):
            raise ValueError("demo check mapping differs from reviewed contract")


def validate_sources(policy: VerificationPolicy) -> None:
    if len(policy.sources) != 3 or {s.profile.backend for s in policy.sources} != {
        "prometheus",
        "loki",
        "tempo",
    }:
        raise ValueError("demo requires one source for each backend")
    for source in policy.sources:
        labels = dict(source.profile.scope_labels) | dict(source.profile.binding_labels)
        if set(labels.values()) & {"case", "le", "__name__"}:
            raise ValueError("scope labels collide with demo metric dimensions")
        for field, label in labels.items():
            if label in {"service.name", "service.version"} and (field, label) not in {
                ("service", "service.name"),
                ("revision", "service.version"),
            }:
                raise ValueError("scope labels collide with resource identity")
        if source.limit < 7:
            raise ValueError("demo sources must admit the entire seven-span journey")

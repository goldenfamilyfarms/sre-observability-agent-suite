"""Versioned catalog and fail-closed report aggregation. No telemetry queries run here."""

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from importlib.resources import files

from .contracts import Evidence, Release, aware, nonempty


class Category(StrEnum):
    TRACES = "traces"
    LOGS = "logs"
    PROMQL = "promql"


class Outcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    ERROR = "ERROR"


@dataclass(frozen=True)
class CheckDefinition:
    id: str
    category: Category
    title: str
    applicability: str
    fixture: str
    expected_result: str
    query_template: str | None
    assertion: str | None
    timeout_seconds: int
    mandatory: bool
    severity: str
    evidence_requirements: tuple[str, ...]

    @property
    def implemented(self) -> bool:
        return bool(self.query_template and self.assertion)


@dataclass(frozen=True)
class Catalog:
    version: str
    checks: tuple[CheckDefinition, ...]

    def __post_init__(self) -> None:
        nonempty(self.version, "catalog version")
        if len({check.id for check in self.checks}) != 120:
            raise ValueError("catalog requires 120 unique IDs")
        if Counter(check.category for check in self.checks) != {
            category: 40 for category in Category
        }:
            raise ValueError("catalog requires 40 traces, 40 logs, and 40 PromQL checks")


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    catalog_version: str
    release: Release
    window_start: datetime
    window_end: datetime
    outcome: Outcome
    evidence: tuple[Evidence, ...]
    assertion_passed: bool | None = None
    justification: str = ""

    def __post_init__(self) -> None:
        aware(self.window_start)
        aware(self.window_end)
        if self.window_end <= self.window_start:
            raise ValueError("invalid observation window")
        if not isinstance(self.outcome, Outcome):
            raise ValueError("invalid check outcome")


@dataclass(frozen=True)
class CertificationReport:
    schema_version: str
    catalog_version: str
    release: Release
    window_start: datetime
    window_end: datetime
    verdict: str
    results: tuple[CheckResult, ...]
    reasons: tuple[str, ...]


def load_catalog() -> Catalog:
    raw = json.loads(files("sre_suite").joinpath("catalog.json").read_text())
    return Catalog(
        raw["version"],
        tuple(
            CheckDefinition(
                **{
                    **check,
                    "category": Category(check["category"]),
                    "evidence_requirements": tuple(check["evidence_requirements"]),
                }
            )
            for check in raw["checks"]
        ),
    )


def certify(
    catalog: Catalog,
    release: Release,
    window_start: datetime,
    window_end: datetime,
    results: tuple[CheckResult, ...],
    now: datetime,
) -> CertificationReport:
    for stamp in (window_start, window_end, now):
        aware(stamp)
    if not window_start < window_end <= now:
        raise ValueError("invalid or future observation window")
    expected = {check.id for check in catalog.checks}
    actual = [result.check_id for result in results]
    reasons: list[str] = []
    failed = False
    if len(set(actual)) != len(actual) or set(actual) != expected:
        reasons.append("all 120 catalog slots must occur exactly once; no unknown IDs")
    by_id = {result.check_id: result for result in results}
    for check in catalog.checks:
        result = by_id.get(check.id)
        if result is None:
            continue
        if (
            result.catalog_version != catalog.version
            or result.release != release
            or (result.window_start, result.window_end) != (window_start, window_end)
        ):
            reasons.append(f"{check.id}: result scope/version/window mismatch")
            continue
        if not result.evidence or not all(
            item.valid_for(release.scope, now) and window_start <= item.observed_at <= window_end
            for item in result.evidence
        ):
            reasons.append(f"{check.id}: missing, stale, or out-of-window evidence")
            continue
        if not check.implemented:
            reasons.append(f"{check.id}: query/assertion adapter not implemented")
        if result.outcome is Outcome.NOT_APPLICABLE:
            if not result.justification.strip():
                reasons.append(f"{check.id}: applicability justification required")
            continue
        if result.outcome is Outcome.FAIL:
            if check.mandatory:
                failed = True
        elif result.outcome is not Outcome.PASS or result.assertion_passed is not True:
            reasons.append(f"{check.id}: deterministic assertion has not passed")
    # Structural uncertainty always prevents a certificate, even when failures exist.
    verdict = "INCOMPLETE" if reasons else "FAILED" if failed else "PASSED"
    return CertificationReport(
        "1",
        catalog.version,
        release,
        window_start,
        window_end,
        verdict,
        tuple(sorted(results, key=lambda result: result.check_id)),
        tuple(reasons),
    )

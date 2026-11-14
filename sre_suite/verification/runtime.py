"""Authenticated evidence resolution, deterministic 120-slot evaluation and immutable reports."""

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from ..api import evidence_id
from ..api_wire import decode, evidence_from_wire, fields, text
from ..certification import CheckResult, Outcome, certify, load_catalog
from ..contracts import Evidence, GateResult
from ..execution.evidence_client import EvidenceClient
from ..execution.http import RemoteError
from ..execution.journal import Journal
from ..execution.telemetry import validate_observed
from ..execution.wire import canonical, digest
from ..telemetry.contracts import epoch_ns, profile_document, request_document
from .contracts import Source, VerificationInput, VerificationPolicy
from .predicates import evaluate, matches
from .records import records


def identity(value: VerificationInput) -> str:
    return "sre-certification-" + digest(value.wire())


class CertificationEngine:
    def __init__(
        self,
        policy: VerificationPolicy,
        evidence: EvidenceClient,
        journal: Journal,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        budget_seconds: int = 120,
    ) -> None:
        if type(budget_seconds) is not int or not 1 <= budget_seconds <= 300:
            raise ValueError("invalid verification budget")
        self.policy, self.evidence, self.journal = policy, evidence, journal
        self.clock, self.monotonic, self.budget = clock, monotonic, budget_seconds

    def run(
        self, raw: object, *, publish: bool = False, heartbeat: Callable[[], None] = lambda: None
    ) -> dict[str, Any]:
        value = VerificationInput.parse(raw)
        policy = self.policy
        if value.policy_sha256 != policy.fingerprint or value.release.scope != policy.scope:
            raise ValueError("verification authority changed")
        if (
            not value.window_start < value.window_end <= self.clock()
            or value.window_start < self.clock() - timedelta(hours=24)
        ):
            raise ValueError("certification requires a recent completed window")
        catalog = load_catalog()
        deadline = self.monotonic() + self.budget

        def checkpoint() -> None:
            heartbeat()
            if self.monotonic() >= deadline:
                raise TimeoutError("certification budget exceeded")

        def fence() -> None:
            checkpoint()
            self.evidence.context(policy.scope, value.context_version)
            checkpoint()

        fence()
        references: dict[str, Evidence] = {}

        def read(ref: Evidence, producer: str) -> dict[str, Any]:
            checkpoint()
            if (
                not ref.valid_for(policy.scope, self.clock())
                or ref.producer != producer
                or not value.window_end
                <= ref.observed_at
                <= value.window_end + timedelta(seconds=120)
            ):
                raise ValueError("untrusted or stale evidence")
            document = decode(self.evidence.read(ref))
            checkpoint()
            if not isinstance(document, dict):
                raise ValueError("invalid evidence document")
            references[ref.id] = ref
            return document

        fixture: dict[str, Any] | None = None
        fixture_error = "fixture_unverified"
        try:
            fixture = read(value.fixture, policy.assessor)
            fields(
                fixture,
                {
                    "schema_version",
                    "kind",
                    "release",
                    "artifact_kind",
                    "policy_sha256",
                    "catalog_sha256",
                    "context_version",
                    "window_start",
                    "window_end",
                    "checks",
                },
            )
            if (
                fixture["schema_version"] != 2
                or fixture["kind"] != "certification-fixture"
                or digest(fixture["release"]) != digest(asdict(value.release))
                or fixture["artifact_kind"] != value.artifact_kind
                or fixture["policy_sha256"] != policy.fingerprint
                or fixture["catalog_sha256"] != policy.catalog_sha256
                or fixture["context_version"] != value.context_version
                or fixture["window_start"] != value.window_start.isoformat()
                or fixture["window_end"] != value.window_end.isoformat()
            ):
                raise ValueError("fixture binding mismatch")
            fields(fixture["checks"], {c.id for c in catalog.checks})
        except RemoteError as error:
            if error.status in {0, 429} or error.status >= 500:
                raise
            fixture = None
        except (ValueError, TypeError, KeyError):
            fixture = None

        sources = {s.profile.id: s for s in policy.sources}
        observations = dict(value.observations)
        if set(observations) - set(sources):
            raise ValueError("unreviewed observation source")
        data: dict[str, list[dict[str, Any]]] = {}
        errors: dict[str, tuple[Outcome, str]] = {}
        for name, source in sources.items():
            checkpoint()
            ref = observations.get(name)
            if ref is None:
                errors[name] = (Outcome.INCONCLUSIVE, "missing_observation")
                continue
            try:
                document = read(ref, policy.collector)
                data[name] = self.observation(document, source, value, ref)
            except RemoteError as error:
                if error.status in {0, 429} or error.status >= 500:
                    raise
                errors[name] = (Outcome.ERROR, "evidence_read_failed")
            except IncompleteObservation:
                errors[name] = (Outcome.INCONCLUSIVE, "empty_or_partial_observation")
            except (ValueError, TypeError, KeyError, OverflowError):
                errors[name] = (Outcome.ERROR, "invalid_observation_binding_or_payload")
        spans: dict[tuple[str, str], dict[str, Any]] = {}
        for rows in data.values():
            for row in rows:
                if "span_id" in row:
                    key = (row["trace_id"], row["span_id"])
                    if key in spans and digest(spans[key]) != digest(row):
                        raise ValueError("conflicting trace observations")
                    spans[key] = row
        context = list(spans.values())
        results = []
        for check, rule in zip(catalog.checks, policy.rules, strict=True):
            checkpoint()
            evidence = [value.fixture] if fixture is not None else []
            outcome, reason, passed = Outcome.ERROR, fixture_error, None
            if fixture is not None:
                try:
                    assessment = fields(
                        fixture["checks"][check.id],
                        {"applicable", "scenario", "expected", "justification"},
                    )
                    text(assessment["scenario"], 256)
                    expected = assessment["expected"]
                    if (
                        type(assessment["applicable"]) is not bool
                        or assessment["applicable"] != rule.applicable
                        or not isinstance(expected, list)
                        or len(expected) > 500
                    ):
                        raise ValueError("invalid fixture assessment")
                    if not rule.applicable:
                        reason = text(assessment["justification"], 512)
                        outcome = Outcome.NOT_APPLICABLE
                    elif rule.source in errors:
                        outcome, reason = errors[rule.source]
                    else:
                        assert rule.source is not None
                        rows = [
                            r
                            for r in data[rule.source]
                            if r["_in_scope"] and matches(r, rule.selector)
                        ]
                        evidence.append(observations[rule.source])
                        operator = str(check.assertion).removesuffix(".v1")
                        if (
                            len(rows) < rule.minimum_records
                            or (
                                operator in {"coverage", "links", "metric_expected"}
                                and not expected
                            )
                            or (operator == "correlation" and not context)
                        ):
                            outcome, reason = (
                                Outcome.INCONCLUSIVE,
                                "insufficient_records_or_fixture_expectations",
                            )
                        else:
                            try:
                                passed = evaluate(
                                    operator,
                                    rule.parameters,
                                    rows,
                                    expected,
                                    epoch_ns(value.window_start),
                                    epoch_ns(value.window_end),
                                    context,
                                )
                            except (ValueError, TypeError, KeyError, OverflowError):
                                passed = False
                            outcome = Outcome.PASS if passed else Outcome.FAIL
                            reason = (
                                "predicate_true" if passed else "predicate_false_or_invalid_field"
                            )
                except (ValueError, TypeError, KeyError):
                    outcome, reason, passed = Outcome.ERROR, "invalid_fixture_assessment", None
            results.append(
                CheckResult(
                    check.id,
                    catalog.version,
                    value.release,
                    value.window_start,
                    value.window_end,
                    outcome,
                    tuple(evidence),
                    passed,
                    reason,
                )
            )
        fence()
        now = self.clock()
        if any(not ref.valid_for(policy.scope, now) for ref in references.values()):
            raise ValueError("evidence expired during evaluation")
        report = certify(
            catalog,
            value.release,
            value.window_start,
            value.window_end,
            tuple(results),
            now,
            collection_grace_seconds=120,
        )
        document = json.loads(
            canonical(
                {
                    "schema_version": 2,
                    "kind": "telemetry-certification",
                    "catalog_version": catalog.version,
                    "catalog_sha256": policy.catalog_sha256,
                    "policy_sha256": policy.fingerprint,
                    "release": asdict(value.release),
                    "artifact_kind": value.artifact_kind,
                    "window_start": value.window_start.isoformat(),
                    "window_end": value.window_end.isoformat(),
                    "context_version": value.context_version,
                    "evaluated_at": now.isoformat(),
                    "expires_at": min(
                        (r.expires_at for r in references.values()), default=now
                    ).isoformat(),
                    "verdict": report.verdict,
                    "reasons": report.reasons,
                    "results": [
                        {
                            "check_id": r.check_id,
                            "outcome": r.outcome,
                            "assertion_passed": r.assertion_passed,
                            "reason": r.justification,
                            "evidence_ids": [e.id for e in r.evidence],
                        }
                        for r in report.results
                    ],
                    "evidence": [asdict(ref) for _, ref in sorted(references.items())],
                    "merged": False,
                    "deployed": False,
                }
            )
        )
        checkpoint()
        saved = self.journal.compute(
            identity(value) + ":report", value.wire(), lambda: document, heartbeat
        )
        # A cached certificate never skips freshness/current-generation checks.
        if (
            datetime.fromisoformat(saved["expires_at"]) <= self.clock()
            and saved["verdict"] == "PASSED"
        ):
            raise ValueError("cached certificate expired")
        fence()
        if publish:
            observed = datetime.fromisoformat(saved["evaluated_at"])
            expires = datetime.fromisoformat(saved["expires_at"])
            if not observed <= self.clock() < expires:
                raise ValueError("report has no current evidence validity interval")
            content = (canonical(saved) + "\n").encode()
            ref = self.evidence.upload(identity(value), policy.scope, content, observed, expires)
            if ref.producer != policy.verifier:
                raise ValueError("unexpected authenticated verifier identity")
            self.evidence.verify(ref)
            fence()
            return {
                "workflow_id": identity(value),
                "verdict": saved["verdict"],
                "report_sha256": ref.sha256,
                "evidence": json.loads(canonical(asdict(ref))),
            }
        return saved

    def gate(self, raw: object, receipt: object) -> GateResult:
        """Resolve a certificate into a gate after rechecking all current evidence fences."""
        value = VerificationInput.parse(raw)
        row = fields(receipt, {"workflow_id", "verdict", "report_sha256", "evidence"})
        ref = evidence_from_wire(row["evidence"])
        policy = self.policy
        if (
            value.policy_sha256 != policy.fingerprint
            or value.release.scope != policy.scope
            or row["workflow_id"] != identity(value)
            or row["verdict"] != "PASSED"
            or ref.producer != policy.verifier
            or ref.sha256 != row["report_sha256"]
            or ref.id != evidence_id(policy.verifier, policy.scope, identity(value))
            or not ref.valid_for(policy.scope, self.clock())
        ):
            raise ValueError("untrusted or stale certificate reference")
        self.evidence.context(policy.scope, value.context_version)
        document = decode(self.evidence.read(ref))
        if not isinstance(document, dict) or (
            document.get("kind") != "telemetry-certification"
            or document.get("schema_version") != 2
            or document.get("verdict") != "PASSED"
            or digest(document.get("release")) != digest(asdict(value.release))
            or document.get("artifact_kind") != value.artifact_kind
            or document.get("policy_sha256") != policy.fingerprint
            or document.get("catalog_sha256") != policy.catalog_sha256
            or document.get("catalog_version") != load_catalog().version
            or document.get("context_version") != value.context_version
            or document.get("window_start") != value.window_start.isoformat()
            or document.get("window_end") != value.window_end.isoformat()
            or document.get("evaluated_at") != ref.observed_at.isoformat()
            or document.get("expires_at") != ref.expires_at.isoformat()
            or document.get("reasons") != []
        ):
            raise ValueError("certificate binding mismatch")
        if value.fixture.producer != policy.assessor or any(
            evidence.producer != policy.collector for _, evidence in value.observations
        ):
            raise ValueError("certificate evidence producer mismatch")
        supplied = {r.id: r for r in [value.fixture, *dict(value.observations).values()]}
        verified = {}
        for item in document["evidence"]:
            evidence = evidence_from_wire(item)
            if (
                evidence.id in verified
                or supplied.get(evidence.id) != evidence
                or not evidence.valid_for(policy.scope, self.clock())
            ):
                raise ValueError("certificate input evidence mismatch")
            self.evidence.verify(evidence)
            verified[evidence.id] = evidence
        if not verified or ref.expires_at != min(e.expires_at for e in verified.values()):
            raise ValueError("certificate validity interval mismatch")
        results = document["results"]
        if len(results) != 120 or {r["check_id"] for r in results} != {r.id for r in policy.rules}:
            raise ValueError("certificate coverage mismatch")
        by_id = {r["check_id"]: r for r in results}
        for rule in policy.rules:
            result = by_id[rule.id]
            expected = {value.fixture.id}
            if rule.applicable:
                assert rule.source is not None
                expected.add(dict(value.observations)[rule.source].id)
                if result["outcome"] != "PASS" or result["assertion_passed"] is not True:
                    raise ValueError("applicable check did not pass")
            elif result["outcome"] != "NOT_APPLICABLE" or not result["reason"].strip():
                raise ValueError("invalid applicability decision")
            if set(result["evidence_ids"]) != expected or not expected <= verified.keys():
                raise ValueError("check evidence mismatch")
        self.evidence.context(policy.scope, value.context_version)
        if not ref.valid_for(policy.scope, self.clock()):
            raise ValueError("certificate expired during verification")
        return GateResult("certification", value.release, True, ref)

    def observation(
        self, doc: dict[str, Any], source: Source, value: VerificationInput, ref: Evidence
    ) -> list[dict[str, Any]]:
        request = source.request(value.release, value.window_start, value.window_end)
        profile = source.profile
        query = profile.query(request.scope, request.binding)
        query_identity = (
            canonical({"trace_id": request.trace_id, "scope_selector": query})
            if request.trace_id
            else query
        )
        result = doc.get("result")
        if (
            doc.get("schema_version") != 2
            or doc.get("kind") != "telemetry-observation"
            or digest(doc.get("request")) != digest(request_document(request))
            or digest(doc.get("profile")) != digest(profile_document(profile))
            or doc.get("rendered_query") != query
            or doc.get("context_version") != value.context_version
            or doc.get("observed_at") != ref.observed_at.isoformat()
            or doc.get("certification_verdict") is not None
            or not isinstance(result, dict)
            or result.get("backend") != profile.backend
            or result.get("query_sha256") != hashlib.sha256(query_identity.encode()).hexdigest()
            or not re.fullmatch(r"[0-9a-f]{64}", str(result.get("response_sha256")))
        ):
            raise ValueError("observation does not match reviewed input")
        if result.get("status") in {"empty", "partial"} or result.get("issues") != []:
            raise IncompleteObservation
        if result.get("status") != "observed":
            raise ValueError("invalid observation status")
        validate_observed(result, profile, request)
        return records(result, request)


class IncompleteObservation(Exception):
    pass

"""Strict reviewed 120-slot policies and exact candidate/window evidence inputs."""

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from ..api_wire import envelope, evidence_from_wire, fields, text
from ..certification import Catalog, load_catalog
from ..contracts import Evidence, Release, Scope, aware
from ..execution.wire import digest
from ..serialization import scope_from_dict
from ..telemetry.contracts import CandidateBinding, QueryPolicy, QueryProfile, QueryRequest, integer
from .predicates import selection, validate

CORE = {
    "TRACES-027",
    "TRACES-028",
    "TRACES-029",
    "LOGS-001",
    "LOGS-005",
    "PROMQL-006",
    "PROMQL-009",
}
BACKENDS = {"traces": "tempo", "logs": "loki", "promql": "prometheus"}


def catalog_digest(catalog: Catalog) -> str:
    return digest(asdict(catalog))


@dataclass(frozen=True)
class Source:
    profile: QueryProfile
    trace_id: str | None
    step_seconds: int
    limit: int

    def request(self, release: Release, start: datetime, end: datetime) -> QueryRequest:
        return QueryRequest(
            release.scope,
            self.profile.id,
            start,
            end,
            self.step_seconds,
            self.limit,
            self.trace_id,
            CandidateBinding(release.artifact_digest, release.plan_digest),
        )


@dataclass(frozen=True)
class Rule:
    id: str
    applicable: bool
    source: str | None
    selector: dict[str, object]
    parameters: dict[str, Any]
    minimum_records: int


@dataclass(frozen=True)
class VerificationPolicy:
    scope: Scope
    collector: str
    assessor: str
    verifier: str
    sources: tuple[Source, ...]
    rules: tuple[Rule, ...]
    catalog_sha256: str
    fingerprint: str

    @classmethod
    def parse(cls, value: object) -> "VerificationPolicy":
        raw = envelope(
            value,
            {"scope", "collector", "assessor", "verifier", "catalog_sha256", "sources", "checks"},
        )
        catalog = load_catalog()
        if raw["catalog_sha256"] != catalog_digest(catalog):
            raise ValueError("catalog fingerprint mismatch")
        collector, assessor = text(raw["collector"], 128), text(raw["assessor"], 128)
        verifier = text(raw["verifier"], 128)
        if len({collector, assessor, verifier}) != 3:
            raise ValueError("collector, fixture assessor and verifier require distinct identities")
        scope = scope_from_dict(raw["scope"])
        items = raw["sources"]
        if not isinstance(items, list) or not 1 <= len(items) <= 120:
            raise ValueError("require 1..120 reviewed sources")
        sources = []
        for value in items:
            row = fields(value, {"profile", "trace_id", "step_seconds", "limit"})
            profile = QueryPolicy.parse(
                {"schema_version": 2, "scope": asdict(scope), "profiles": [row["profile"]]}
            ).profiles[0]
            trace_id = None if row["trace_id"] is None else text(row["trace_id"], 32)
            if (
                not profile.binding_labels
                or (
                    profile.backend == "tempo"
                    and (not profile.allow_full_traces or trace_id is None)
                )
                or (profile.backend != "tempo" and trace_id is not None)
            ):
                raise ValueError("certification requires bound sources and full traces")
            source = Source(
                profile,
                trace_id,
                integer(row["step_seconds"], 1, 3600),
                integer(row["limit"], 1, 500),
            )
            # Apply the same strict trace identity validation without live time or network access.
            start = datetime.fromisoformat("2026-01-01T00:00:00+00:00")
            source.request(
                Release(scope, "sha256:" + "a" * 64, "sha256:" + "b" * 64, ("certification",)),
                start,
                start + timedelta(seconds=1),
            )
            sources.append(source)
        by_id = {s.profile.id: s for s in sources}
        if len(by_id) != len(sources):
            raise ValueError("duplicate source identity")
        checks = fields(raw["checks"], {c.id for c in catalog.checks})
        rules = []
        for check in catalog.checks:
            row = fields(
                checks[check.id],
                {"applicable", "source", "selector", "parameters", "minimum_records"},
            )
            if type(row["applicable"]) is not bool:
                raise ValueError("applicability must be explicitly reviewed")
            applicable = row["applicable"]
            name = None if row["source"] is None else text(row["source"], 64)
            if not applicable:
                if (
                    check.id in CORE
                    or name is not None
                    or row["selector"] != {}
                    or row["parameters"] != {}
                ):
                    raise ValueError("invalid not-applicable rule")
                parameters = {}
            else:
                if name not in by_id or by_id[name].profile.backend != BACKENDS[check.category]:
                    raise ValueError("check requires matching reviewed source backend")
                parameters = validate(str(check.assertion).removesuffix(".v1"), row["parameters"])
            rules.append(
                Rule(
                    check.id,
                    applicable,
                    name,
                    selection(row["selector"]),
                    parameters,
                    integer(row["minimum_records"], 1, 5000),
                )
            )
        # Canonical JSON is bounded/duplicate-checked by the private loader or API decoder.
        return cls(
            scope,
            collector,
            assessor,
            verifier,
            tuple(sources),
            tuple(rules),
            catalog_digest(catalog),
            digest(raw),
        )


@dataclass(frozen=True)
class VerificationInput:
    release: Release
    artifact_kind: str
    window_start: datetime
    window_end: datetime
    context_version: int
    policy_sha256: str
    fixture: Evidence
    observations: tuple[tuple[str, Evidence], ...]

    @classmethod
    def parse(cls, value: object) -> "VerificationInput":
        raw = envelope(
            value,
            {
                "release",
                "artifact_kind",
                "window_start",
                "window_end",
                "context_version",
                "policy_sha256",
                "fixture",
                "observations",
            },
        )
        row = fields(raw["release"], {"scope", "artifact_digest", "plan_digest", "required_gates"})
        gates = row["required_gates"]
        if not isinstance(gates, list) or not 1 <= len(gates) <= 30:
            raise ValueError("invalid release gates")
        release = Release(
            scope_from_dict(row["scope"]),
            text(row["artifact_digest"]),
            text(row["plan_digest"]),
            tuple(text(v, 128) for v in gates),
        )
        if raw["artifact_kind"] != "source-candidate-v1":
            raise ValueError("only source-candidate provenance is currently implemented")
        start, end = (
            datetime.fromisoformat(text(raw[name])) for name in ("window_start", "window_end")
        )
        for stamp in (start, end):
            aware(stamp)
        if not timedelta(0) < end - start <= timedelta(hours=1):
            raise ValueError("invalid certification window")
        items = raw["observations"]
        if not isinstance(items, list) or len(items) > 120:
            raise ValueError("invalid observation bundle")
        observations = []
        for item in items:
            entry = fields(item, {"source", "evidence"})
            observations.append((text(entry["source"], 64), evidence_from_wire(entry["evidence"])))
        if len(dict(observations)) != len(observations):
            raise ValueError("duplicate observation source")
        return cls(
            release,
            str(raw["artifact_kind"]),
            start,
            end,
            integer(raw["context_version"], 0, 2**63 - 1),
            text(raw["policy_sha256"], 64),
            evidence_from_wire(raw["fixture"]),
            tuple(observations),
        )

    def wire(self) -> dict[str, Any]:
        from ..execution.wire import canonical

        value = asdict(self)
        value["observations"] = [{"source": k, "evidence": asdict(v)} for k, v in self.observations]
        result: dict[str, Any] = json.loads(canonical({"schema_version": 2, **value}))
        return result

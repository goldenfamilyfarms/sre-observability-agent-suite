"""Operator-reviewed query policies and bounded, instance-scoped observation contracts."""

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from ..api_wire import envelope, fields, text
from ..contracts import Scope, aware
from ..execution.config import https_url
from ..serialization import scope_from_dict

SCOPE_FIELDS = frozenset({"service", "environment", "revision", "environment_id", "generation"})


class Backend(StrEnum):
    PROMETHEUS = "prometheus"
    LOKI = "loki"
    TEMPO = "tempo"


class ObservationStatus(StrEnum):
    OBSERVED = "observed"
    EMPTY = "empty"
    PARTIAL = "partial"


def integer(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid integer bound")
    return value


def identifier(value: object) -> str:
    result = text(value, 64)
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", result):
        raise ValueError("invalid telemetry identifier")
    return result


def epoch_ns(value: datetime) -> int:
    aware(value)
    delta = value - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


@dataclass(frozen=True)
class CandidateBinding:
    """Digests of an immutable source recipe and the frozen engineering manifest."""

    artifact_digest: str
    plan_digest: str

    def __post_init__(self) -> None:
        for value in (self.artifact_digest, self.plan_digest):
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
                raise ValueError("candidate binding requires full SHA-256 digests")

    @classmethod
    def parse(cls, value: object) -> "CandidateBinding":
        row = fields(value, {"artifact_digest", "plan_digest"})
        return cls(text(row["artifact_digest"]), text(row["plan_digest"]))


@dataclass(frozen=True)
class QueryRequest:
    scope: Scope
    profile: str
    start: datetime
    end: datetime
    step_seconds: int = 15
    limit: int = 100
    trace_id: str | None = None
    binding: CandidateBinding | None = None

    def __post_init__(self) -> None:
        if self.binding is not None and not isinstance(self.binding, CandidateBinding):
            raise ValueError("invalid candidate binding")
        identifier(self.profile)
        aware(self.start)
        aware(self.end)
        if not timedelta(0) < self.end - self.start <= timedelta(hours=1):
            raise ValueError("query window must be positive and at most one hour")
        if epoch_ns(self.start) < 0:
            raise ValueError("query window must follow the Unix epoch")
        integer(self.step_seconds, 1, 3600)
        integer(self.limit, 1, 500)
        if self.trace_id is not None and (
            not re.fullmatch(r"[0-9a-f]{32}", self.trace_id) or int(self.trace_id, 16) == 0
        ):
            raise ValueError("trace_id requires a nonzero full lowercase hex trace ID")

    def current(self, now: datetime) -> None:
        aware(now)
        if self.end > now or self.start < now - timedelta(hours=24):
            raise ValueError("query window must be in the past 24 hours")

    @classmethod
    def parse(cls, value: object) -> "QueryRequest":
        names = {"scope", "profile", "start", "end", "step_seconds", "limit"}
        if isinstance(value, dict) and "trace_id" in value:
            names.add("trace_id")
        if isinstance(value, dict) and "binding" in value:
            names.add("binding")
        raw = envelope(value, names)
        return cls(
            scope_from_dict(raw["scope"]),
            text(raw["profile"]),
            datetime.fromisoformat(text(raw["start"])),
            datetime.fromisoformat(text(raw["end"])),
            integer(raw["step_seconds"], 1, 3600),
            integer(raw["limit"], 1, 500),
            None if raw.get("trace_id") is None else text(raw["trace_id"], 32),
            None if raw.get("binding") is None else CandidateBinding.parse(raw["binding"]),
        )


@dataclass(frozen=True)
class QueryProfile:
    id: str
    backend: Backend
    base_url: str
    authorization_env: str
    tenant_env: str | None
    query_template: str
    scope_labels: tuple[tuple[str, str], ...]
    allow_full_traces: bool = False
    binding_labels: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        identifier(self.id)
        if type(self.allow_full_traces) is not bool or (
            self.allow_full_traces and self.backend != Backend.TEMPO
        ):
            raise ValueError("full traces require explicit Tempo profile authorization")
        if not isinstance(self.backend, Backend):
            raise ValueError("invalid backend")
        https_url(self.base_url)
        for variable in (self.authorization_env, self.tenant_env):
            if variable is not None and not re.fullmatch(
                r"SRE_TELEMETRY_[A-Z0-9_]{1,64}", variable
            ):
                raise ValueError("use a telemetry-specific credential environment variable")
        text(self.query_template, 4096)
        if self.query_template.count("${scope}") != 1 or "${" in self.query_template.replace(
            "${scope}", ""
        ):
            raise ValueError("query template requires exactly one scope selector placeholder")
        labels = dict(self.scope_labels)
        if (
            len(self.scope_labels) != 5
            or set(labels) != SCOPE_FIELDS
            or len(set(labels.values())) != 5
        ):
            raise ValueError("all five scope dimensions require distinct source mappings")
        pattern = (
            r"[A-Za-z_][A-Za-z0-9_.]{0,127}"
            if self.backend == Backend.TEMPO
            else (r"[A-Za-z_][A-Za-z0-9_]{0,127}")
        )
        binding_labels = dict(self.binding_labels)
        if self.binding_labels and (
            len(self.binding_labels) != 2
            or set(binding_labels) != {"artifact_digest", "plan_digest"}
            or len(set([*labels.values(), *binding_labels.values()])) != 7
        ):
            raise ValueError("candidate digests require two distinct additional mappings")
        if any(
            not re.fullmatch(pattern, label)
            for label in [*labels.values(), *binding_labels.values()]
        ):
            raise ValueError("invalid telemetry scope mapping")

    def bindings(self, scope: Scope, binding: CandidateBinding | None = None) -> dict[str, str]:
        values = asdict(scope)
        result = {label: str(values[name]) for name, label in self.scope_labels}
        if binding is not None:
            if not self.binding_labels:
                raise ValueError("profile has no candidate binding mappings")
            result.update({label: asdict(binding)[name] for name, label in self.binding_labels})
        return result

    def query(self, scope: Scope, binding: CandidateBinding | None = None) -> str:
        bindings = self.bindings(scope, binding)
        if self.backend == Backend.TEMPO:
            selector = " && ".join(
                f"resource.{key} = {json.dumps(value)}" for key, value in sorted(bindings.items())
            )
        else:
            selector = ",".join(
                f"{key}={json.dumps(value)}" for key, value in sorted(bindings.items())
            )
        return self.query_template.replace("${scope}", "{" + selector + "}")


@dataclass(frozen=True)
class QueryPolicy:
    scope: Scope
    profiles: tuple[QueryProfile, ...]

    def __post_init__(self) -> None:
        if not 1 <= len(self.profiles) <= 20 or len({p.id for p in self.profiles}) != len(
            self.profiles
        ):
            raise ValueError("invalid or duplicate profiles")

    def resolve(self, request: QueryRequest) -> QueryProfile:
        if request.scope != self.scope:
            raise ValueError("request does not match the operator-authorized scope")
        for profile in self.profiles:
            if profile.id == request.profile:
                if request.trace_id is not None and not profile.allow_full_traces:
                    raise ValueError("full trace fetching is not authorized")
                profile.bindings(request.scope, request.binding)
                return profile
        raise ValueError("query profile not authorized")

    @classmethod
    def parse(cls, value: object) -> "QueryPolicy":
        raw = envelope(value, {"scope", "profiles"})
        items = raw["profiles"]
        if not isinstance(items, list) or not 1 <= len(items) <= 20:
            raise ValueError("invalid profiles")
        profiles = []
        for item in items:
            optional = (
                {"allow_full_traces"}
                if isinstance(item, dict) and ("allow_full_traces" in item)
                else set()
            )
            if isinstance(item, dict) and "binding_labels" in item:
                optional.add("binding_labels")
            row = fields(
                item,
                {
                    "id",
                    "backend",
                    "base_url",
                    "authorization_env",
                    "tenant_env",
                    "query_template",
                    "scope_labels",
                }
                | optional,
            )
            labels = fields(row["scope_labels"], set(SCOPE_FIELDS))
            if "binding_labels" in row and row["binding_labels"] not in ({}, []):
                fields(row["binding_labels"], {"artifact_digest", "plan_digest"})
            profiles.append(
                QueryProfile(
                    text(row["id"]),
                    Backend(text(row["backend"])),
                    text(row["base_url"]),
                    text(row["authorization_env"]),
                    None if row["tenant_env"] is None else text(row["tenant_env"]),
                    text(row["query_template"]),
                    tuple(sorted((k, text(v, 128)) for k, v in labels.items())),
                    boolean(row.get("allow_full_traces", False)),
                    tuple(
                        sorted(
                            (k, text(v, 128))
                            for k, v in fields(
                                row["binding_labels"], {"artifact_digest", "plan_digest"}
                            ).items()
                        )
                    )
                    if row.get("binding_labels")
                    else (),
                )
            )
        return cls(scope_from_dict(raw["scope"]), tuple(profiles))


@dataclass(frozen=True)
class Sample:
    timestamp_ns: int
    value: float


@dataclass(frozen=True)
class MetricSeries:
    labels: tuple[tuple[str, str], ...]
    samples: tuple[Sample, ...]


@dataclass(frozen=True)
class LogEntry:
    labels: tuple[tuple[str, str], ...]
    timestamp_ns: int
    line: str
    metadata: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class TraceSummary:
    trace_id: str
    root_service: str
    root_name: str
    start_ns: int
    duration_ms: int


def boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("expected explicit boolean")
    return value


@dataclass(frozen=True)
class TraceSpan:
    span_id: str
    parent_span_id: str | None
    name: str
    start_ns: int
    end_ns: int
    resource_index: int
    scope_index: int
    matches_scope: bool


@dataclass(frozen=True)
class FullTrace:
    trace_id: str
    spans: tuple[TraceSpan, ...]
    # Preserve the complete bounded payload, including resources, instrumentation scopes,
    # attributes, events, links, status, flags and forward-compatible extension fields.
    otlp_json: str


@dataclass(frozen=True)
class QueryResult:
    backend: Backend
    status: ObservationStatus
    issues: tuple[str, ...]
    query_sha256: str
    response_sha256: str
    metrics: tuple[MetricSeries, ...] = ()
    logs: tuple[LogEntry, ...] = ()
    traces: tuple[TraceSummary, ...] = ()
    full_trace: FullTrace | None = None


def request_document(request: QueryRequest) -> dict[str, object]:
    value = asdict(request)
    if request.binding is None:
        value.pop("binding")
    return value


def profile_document(profile: QueryProfile) -> dict[str, object]:
    value = asdict(profile)
    if not profile.binding_labels:
        value.pop("binding_labels")
    else:
        value["scope_labels"] = sorted(profile.scope_labels)
        value["binding_labels"] = sorted(profile.binding_labels)
    return value

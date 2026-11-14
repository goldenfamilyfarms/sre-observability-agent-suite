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
class QueryRequest:
    scope: Scope
    profile: str
    start: datetime
    end: datetime
    step_seconds: int = 15
    limit: int = 100

    def __post_init__(self) -> None:
        identifier(self.profile)
        aware(self.start)
        aware(self.end)
        if not timedelta(0) < self.end - self.start <= timedelta(hours=1):
            raise ValueError("query window must be positive and at most one hour")
        if epoch_ns(self.start) < 0:
            raise ValueError("query window must follow the Unix epoch")
        integer(self.step_seconds, 1, 3600)
        integer(self.limit, 1, 500)

    def current(self, now: datetime) -> None:
        aware(now)
        if self.end > now or self.start < now - timedelta(hours=24):
            raise ValueError("query window must be in the past 24 hours")

    @classmethod
    def parse(cls, value: object) -> "QueryRequest":
        raw = envelope(value, {"scope", "profile", "start", "end", "step_seconds", "limit"})
        return cls(
            scope_from_dict(raw["scope"]),
            text(raw["profile"]),
            datetime.fromisoformat(text(raw["start"])),
            datetime.fromisoformat(text(raw["end"])),
            integer(raw["step_seconds"], 1, 3600),
            integer(raw["limit"], 1, 500),
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

    def __post_init__(self) -> None:
        identifier(self.id)
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
        if any(not re.fullmatch(pattern, label) for label in labels.values()):
            raise ValueError("invalid telemetry scope mapping")

    def bindings(self, scope: Scope) -> dict[str, str]:
        values = asdict(scope)
        return {label: str(values[name]) for name, label in self.scope_labels}

    def query(self, scope: Scope) -> str:
        bindings = self.bindings(scope)
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
                },
            )
            labels = fields(row["scope_labels"], set(SCOPE_FIELDS))
            profiles.append(
                QueryProfile(
                    text(row["id"]),
                    Backend(text(row["backend"])),
                    text(row["base_url"]),
                    text(row["authorization_env"]),
                    None if row["tenant_env"] is None else text(row["tenant_env"]),
                    text(row["query_template"]),
                    tuple(sorted((k, text(v, 128)) for k, v in labels.items())),
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

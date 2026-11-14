"""Bounded GET-only Prometheus range, Loki log-range and Tempo TraceQL search clients."""

import hashlib
import math
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Protocol, cast
from urllib.parse import urlencode

from ..api_wire import decode, text
from ..execution.http import JsonHttp, RemoteError
from ..execution.wire import canonical
from .contracts import (
    Backend,
    LogEntry,
    MetricSeries,
    ObservationStatus,
    QueryProfile,
    QueryRequest,
    QueryResult,
    Sample,
    TraceSummary,
    epoch_ns,
    integer,
)


class TelemetryError(Exception):
    """Only a static code may cross a public CLI/log boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ReadTransport(Protocol):
    def call(self, method: str, path: str, payload: object = None) -> object: ...


def transport(profile: QueryProfile, env: Mapping[str, str]) -> JsonHttp:
    authorization = env.get(profile.authorization_env, "")
    # Complete Basic or Bearer header allows managed Grafana and standalone gateways.
    if not re.fullmatch(r"(?:Bearer|Basic) [A-Za-z0-9._~+/=-]{1,8192}", authorization):
        raise TelemetryError("invalid_credentials")
    headers = {}
    if profile.tenant_env:
        tenant = env.get(profile.tenant_env, "")
        # No cross-tenant federation (the | separator), whitespace or header injection.
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,150}", tenant):
            raise TelemetryError("invalid_tenant")
        headers["X-Scope-OrgID"] = tenant
    return JsonHttp(profile.base_url, authorization, headers=headers, timeout=20)


def obj(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return cast(dict[str, object], value)


def array(value: object, limit: int) -> list[object]:
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError("invalid collection size")
    return value


def string(value: object, limit: int = 4096) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("invalid string")
    return value


def labels(value: object) -> tuple[tuple[str, str], ...]:
    raw = obj(value)
    if len(raw) > 100:
        raise ValueError("too many labels")
    return tuple(sorted((text(k, 128), string(v)) for k, v in raw.items()))


def scoped_labels(
    value: object, profile: QueryProfile, request: QueryRequest
) -> tuple[tuple[str, str], ...]:
    result = labels(value)
    actual = dict(result)
    if any(actual.get(k) != v for k, v in profile.bindings(request.scope, request.binding).items()):
        raise TelemetryError("response_scope_mismatch")
    return result


def nanoseconds(value: object) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value):
        raise ValueError("invalid nanosecond timestamp")
    return integer(int(value), 0, 2**63 - 1)


def seconds_ns(value: object) -> int:
    if type(value) not in (int, float):
        raise ValueError("invalid sample timestamp")
    nanos = Decimal(str(value)) * 1_000_000_000
    if not nanos.is_finite() or nanos != nanos.to_integral_value():
        raise ValueError("invalid sample timestamp precision")
    return integer(int(nanos), 0, 2**63 - 1)


def response_data(raw: object, expected_type: str) -> tuple[list[object], list[str]]:
    top = obj(raw)
    if top.get("status") != "success" or "error" in top or "errorType" in top:
        raise TelemetryError("backend_query_error")
    data = obj(top.get("data"))
    if data.get("resultType") != expected_type:
        raise ValueError("unsupported result type")
    issues = []
    for key in ("warnings", "infos"):
        for item in array(top.get(key, []), 100):
            text(item)
        if top.get(key):
            issues.append("backend_" + key)
    return array(data.get("result"), 500), issues


def prometheus(
    raw: object, profile: QueryProfile, request: QueryRequest
) -> tuple[tuple[MetricSeries, ...], list[str]]:
    rows, issues = response_data(raw, "matrix")
    if len(rows) > request.limit:
        raise ValueError("series limit exceeded")
    if len(rows) == request.limit:
        issues.append("result_limit")
    result = []
    seen = set()
    count = 0
    for item in rows:
        row = obj(item)
        if set(row) != {"metric", "values"}:
            raise ValueError("unsupported sample representation")
        identity = scoped_labels(row["metric"], profile, request)
        if identity in seen:
            raise ValueError("duplicate metric series")
        seen.add(identity)
        samples = []
        last = -1
        for entry in array(row["values"], 5000):
            pair = array(entry, 2)
            if len(pair) != 2:
                raise ValueError("invalid sample")
            timestamp = seconds_ns(pair[0])
            value = float(text(pair[1], 128))
            if not math.isfinite(value):
                raise ValueError("nonfinite sample")
            if (
                not epoch_ns(request.start) <= timestamp <= epoch_ns(request.end)
                or timestamp <= last
            ):
                raise ValueError("sample outside ordered observation window")
            last = timestamp
            samples.append(Sample(timestamp, value))
            count += 1
            if count > 5000:
                raise ValueError("sample budget exceeded")
        if not samples:
            raise ValueError("empty metric series")
        result.append(MetricSeries(identity, tuple(samples)))
    return tuple(result), issues


def loki(
    raw: object, profile: QueryProfile, request: QueryRequest
) -> tuple[tuple[LogEntry, ...], list[str]]:
    rows, issues = response_data(raw, "streams")
    result = []
    seen = set()
    for item in rows:
        row = obj(item)
        if set(row) != {"stream", "values"}:
            raise ValueError("invalid log stream")
        identity = scoped_labels(row["stream"], profile, request)
        if identity in seen:
            raise ValueError("duplicate log stream")
        seen.add(identity)
        last = -1
        entries = array(row["values"], request.limit)
        if not entries:
            raise ValueError("empty log stream")
        for entry in entries:
            pair = array(entry, 3)
            if len(pair) not in (2, 3):
                raise ValueError("invalid log entry")
            timestamp = nanoseconds(pair[0])
            if not epoch_ns(request.start) <= timestamp < epoch_ns(request.end) or timestamp < last:
                raise ValueError("log outside ordered observation window")
            last = timestamp
            line = string(pair[1], 16384)
            if len(line.encode("utf-8")) > 16384:
                raise ValueError("log line byte budget exceeded")
            result.append(
                LogEntry(
                    identity,
                    timestamp,
                    line,
                    labels(pair[2]) if len(pair) == 3 else (),
                )
            )
            if len(result) > request.limit:
                raise ValueError("log budget exceeded")
    if len(result) == request.limit:
        issues.append("result_limit")
    return tuple(result), issues


def tempo(raw: object, request: QueryRequest) -> tuple[tuple[TraceSummary, ...], list[str]]:
    top = obj(raw)
    if "error" in top or "status" in top or "message" in top:
        raise TelemetryError("backend_query_error")
    # Protobuf JSON omits an empty repeated field. Require a metrics object to distinguish
    # this valid empty response from {} or an unrelated successful HTTP response.
    obj(top.get("metrics"))
    rows = array(top.get("traces", []), request.limit)
    result = []
    seen = set()
    for item in rows:
        row = obj(item)
        trace_id = text(row.get("traceID"), 32)
        if not re.fullmatch(r"[0-9a-f]{1,32}", trace_id) or int(trace_id, 16) == 0:
            raise ValueError("invalid trace identity")
        trace_id = trace_id.zfill(32)
        if trace_id in seen:
            raise ValueError("duplicate trace")
        seen.add(trace_id)
        # Search matches spans; a trace can start before the query window and have a
        # different root service. A summary is deliberately NOT a scope-verified trace.
        result.append(
            TraceSummary(
                trace_id,
                string(row.get("rootServiceName", "")),
                string(row.get("rootTraceName", "")),
                nanoseconds(row.get("startTimeUnixNano")),
                integer(row.get("durationMs", 0), 0, 2**32 - 1),
            )
        )
    issues = ["search_subset"]
    if len(result) == request.limit:
        issues.append("result_limit")
    return tuple(result), issues


class TelemetryAdapter:
    def __init__(self, profile: QueryProfile, http: ReadTransport) -> None:
        self.profile = profile
        self.http = http

    def query(self, request: QueryRequest) -> QueryResult:
        profile = self.profile
        if profile.id != request.profile:
            raise TelemetryError("profile_mismatch")
        if request.trace_id is not None and not profile.allow_full_traces:
            raise TelemetryError("full_trace_not_authorized")
        query = profile.query(request.scope, request.binding)
        params: dict[str, str | int] = {"query": query, "limit": request.limit}
        if profile.backend == Backend.PROMETHEUS:
            path = "/api/v1/query_range"
            params.update(
                start=request.start.isoformat(),
                end=request.end.isoformat(),
                step=request.step_seconds,
                timeout="15s",
            )
        elif profile.backend == Backend.LOKI:
            path = "/loki/api/v1/query_range"
            params.update(
                start=str(epoch_ns(request.start)),
                end=str(epoch_ns(request.end)),
                direction="forward",
            )
        else:
            if request.start.microsecond or request.end.microsecond:
                raise TelemetryError("tempo_requires_whole_seconds")
            path = "/api/search"
            params = {
                "q": query,
                "start": epoch_ns(request.start) // 1_000_000_000,
                "end": epoch_ns(request.end) // 1_000_000_000,
                "limit": request.limit,
                "spss": 1,
            }
            if request.trace_id is not None:
                path = "/api/v2/traces/" + request.trace_id
                params = {
                    "start": epoch_ns(request.start) // 1_000_000_000,
                    "end": epoch_ns(request.end) // 1_000_000_000,
                    "span_pruning": "false",
                }
                query = canonical({"trace_id": request.trace_id, "scope_selector": query})
        # Encode periods too: the common HTTP client's traversal rejection also covers
        # query strings; a legitimate reviewed regex containing '..' must still work.
        endpoint = path + "?" + urlencode(params).replace(".", "%2E")
        try:
            raw = self.http.call("GET", endpoint)
            payload = canonical(raw).encode()
            if len(payload) > 500_000:
                raise ValueError("telemetry payload budget exceeded")
            decode(payload)  # Apply depth/node/finite limits to injected transports as well.
            metrics: tuple[MetricSeries, ...] = ()
            logs: tuple[LogEntry, ...] = ()
            traces: tuple[TraceSummary, ...] = ()
            complete_trace = None
            if profile.backend == Backend.PROMETHEUS:
                metrics, issues = prometheus(raw, profile, request)
            elif profile.backend == Backend.LOKI:
                logs, issues = loki(raw, profile, request)
            elif request.trace_id is None:
                traces, issues = tempo(raw, request)
            else:
                from .traces import full_trace

                complete_trace, issues = full_trace(raw, profile, request)
            count = len(metrics) + len(logs) + len(traces)
            if complete_trace:
                count += len(complete_trace.spans)
            status = (
                ObservationStatus.PARTIAL
                if issues
                else (ObservationStatus.OBSERVED if count else ObservationStatus.EMPTY)
            )
            return QueryResult(
                profile.backend,
                status,
                tuple(issues),
                hashlib.sha256(query.encode()).hexdigest(),
                hashlib.sha256(payload).hexdigest(),
                metrics,
                logs,
                traces,
                complete_trace,
            )
        except RemoteError as error:
            code = "transport_error" if error.status == 0 else f"http_{error.status}"
            raise TelemetryError(code) from None
        except (ValueError, TypeError, OverflowError, InvalidOperation):
            raise TelemetryError("invalid_response") from None

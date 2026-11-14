"""Validate full Tempo V2 trace responses without confusing retrieval with certification."""

import base64
import binascii
import math
import re

from ..api_wire import text
from ..execution.wire import canonical
from .adapters import TelemetryError, array, nanoseconds, obj, string
from .contracts import FullTrace, QueryProfile, QueryRequest, TraceSpan, epoch_ns, integer


def wire_id(value: object, size: int, *, parent: bool = False) -> str | None:
    value = string(value, 64)
    if parent and not value:
        return None
    if re.fullmatch(r"[0-9a-fA-F]{" + str(size * 2) + "}", value):
        decoded = bytes.fromhex(value)
    else:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("invalid trace/span identity") from None
        if base64.b64encode(decoded).decode() != value:
            raise ValueError("noncanonical identity encoding")
    if len(decoded) != size:
        raise ValueError("invalid trace/span identity length")
    if not any(decoded):
        if parent:
            return None
        raise ValueError("zero trace/span identity")
    return decoded.hex()


def any_value(value: object, depth: int) -> dict[str, object]:
    attribute = obj(value)
    if depth > 4 or len(attribute) != 1:
        raise ValueError("invalid AnyValue shape or depth")
    kind, item = next(iter(attribute.items()))
    if kind == "stringValue":
        string(item, 16384)
    elif kind == "boolValue":
        if type(item) is not bool:
            raise ValueError("invalid boolean attribute")
    elif kind == "intValue":
        if isinstance(item, str) and re.fullmatch(r"-?(?:0|[1-9][0-9]{0,18})", item):
            item = int(item)
        integer(item, -(2**63), 2**63 - 1)
    elif kind == "doubleValue":
        if (
            not isinstance(item, (int, float))
            or isinstance(item, bool)
            or not math.isfinite(float(item))
        ):
            raise ValueError("invalid numeric attribute")
    elif kind == "bytesValue":
        encoded = string(item, 16384)
        decoded = base64.b64decode(encoded, validate=True)
        if base64.b64encode(decoded).decode() != encoded:
            raise ValueError("invalid byte attribute")
    elif kind == "arrayValue":
        for entry in array(obj(item).get("values", []), 100):
            any_value(entry, depth + 1)
    elif kind == "kvlistValue":
        attributes(obj(item).get("values", []), depth + 1)
    else:
        raise ValueError("unsupported AnyValue")
    return attribute


def attributes(value: object, depth: int = 0) -> dict[str, object]:
    result: dict[str, object] = {}
    for entry in array(value, 100):
        row = obj(entry)
        key = text(row.get("key"), 128)
        if key in result:
            raise ValueError("duplicate attribute")
        # Preserve types; never coerce numeric generation attributes into string selectors.
        result[key] = any_value(row.get("value"), depth)
    return result


def alias(raw: dict[str, object], current: str, legacy: str) -> object:
    if current in raw and legacy in raw:
        raise ValueError("ambiguous trace representation")
    return raw.get(current, raw.get(legacy, []))


def full_trace(
    raw: object, profile: QueryProfile, request: QueryRequest
) -> tuple[FullTrace, list[str]]:
    top = obj(raw)
    if "error" in top:
        raise TelemetryError("backend_query_error")
    status = top.get("status", "COMPLETE")  # protobuf's omitted enum zero value
    if type(status) is bool or status not in (0, 1, "COMPLETE", "PARTIAL"):
        raise ValueError("unknown trace completeness status")
    issues = ["backend_partial"] if status in (1, "PARTIAL") else []
    if string(top.get("message", "")):
        issues.append("backend_message")
    pruning = alias(top, "pruneStatus", "prune_status")
    if type(pruning) is bool or pruning not in (
        [],
        0,
        1,
        2,
        "NOT_PRUNED",
        "PRUNED_ON_WRITE",
        "PRUNED_ON_READ",
    ):
        raise ValueError("unknown pruning status")
    if pruning not in ([], 0, "NOT_PRUNED"):
        issues.append("span_pruning")
    trace = obj(top.get("trace"))
    resources = array(alias(trace, "resourceSpans", "batches"), 100)
    spans = []
    identifiers: dict[str, str | None] = {}
    matching = False
    for resource_index, entry in enumerate(resources):
        resource_spans = obj(entry)
        resource = obj(resource_spans.get("resource", {}))
        attrs = attributes(resource.get("attributes", []))
        matches = all(
            attrs.get(k) == {"stringValue": v} for k, v in profile.bindings(request.scope).items()
        )
        groups = array(alias(resource_spans, "scopeSpans", "instrumentationLibrarySpans"), 100)
        for scope_index, group in enumerate(groups):
            for value in array(obj(group).get("spans", []), request.limit):
                row = obj(value)
                if wire_id(row.get("traceId"), 16) != request.trace_id:
                    raise TelemetryError("trace_identity_mismatch")
                identity = wire_id(row.get("spanId"), 8)
                assert identity is not None
                parent = wire_id(row.get("parentSpanId", ""), 8, parent=True)
                if identity in identifiers or identity == parent:
                    raise ValueError("duplicate or self-parented span")
                identifiers[identity] = parent
                start, end = (
                    nanoseconds(row.get("startTimeUnixNano")),
                    nanoseconds(row.get("endTimeUnixNano")),
                )
                if end < start:
                    raise ValueError("invalid span duration")
                attributes(row.get("attributes", []))
                for event in array(row.get("events", []), 100):
                    event_row = obj(event)
                    nanoseconds(event_row.get("timeUnixNano"))
                    text(event_row.get("name"))
                    attributes(event_row.get("attributes", []))
                for link in array(row.get("links", []), 100):
                    link_row = obj(link)
                    wire_id(link_row.get("traceId"), 16)
                    wire_id(link_row.get("spanId"), 8)
                    attributes(link_row.get("attributes", []))
                matching |= (
                    matches and start <= epoch_ns(request.end) and end >= epoch_ns(request.start)
                )
                spans.append(
                    TraceSpan(
                        identity,
                        parent,
                        text(row.get("name")),
                        start,
                        end,
                        resource_index,
                        scope_index,
                        matches,
                    )
                )
                if len(spans) > request.limit:
                    raise ValueError("trace span budget exceeded")
    if not spans or not matching:
        raise TelemetryError("response_scope_mismatch")
    for identity, parent in identifiers.items():
        ancestors = {identity}
        while parent is not None and parent in identifiers:
            if parent in ancestors:
                raise ValueError("cyclic trace graph")
            ancestors.add(parent)
            parent = identifiers[parent]
        if parent is not None and "missing_parent" not in issues:
            issues.append("missing_parent")
    if sum(parent is None for parent in identifiers.values()) != 1:
        issues.append("root_count")
    pending: list[object] = [trace]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"droppedAttributesCount", "droppedEventsCount", "droppedLinksCount"}:
                    if integer(value, 0, 2**32 - 1) and "dropped_data" not in issues:
                        issues.append("dropped_data")
                else:
                    pending.append(value)
        elif isinstance(node, list):
            pending.extend(node)
    assert request.trace_id is not None
    return FullTrace(request.trace_id, tuple(spans), canonical(trace)), issues

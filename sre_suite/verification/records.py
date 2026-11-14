"""Bounded canonical records from captured full traces, structured logs and float matrices."""

import json
from typing import Any, cast

from ..api_wire import decode
from ..telemetry.contracts import QueryRequest, epoch_ns
from ..telemetry.traces import alias, wire_id
from .predicates import number


def attributes(items: list[dict[str, Any]]) -> dict[str, Any]:
    def scalar(value: dict[str, Any]) -> Any:
        key, item = next(iter(value.items()))
        if key == "arrayValue":
            return [scalar(v) for v in item.get("values", [])]
        if key == "kvlistValue":
            return attributes(item.get("values", []))
        if key == "intValue":
            return int(item)
        return item

    return {item["key"]: scalar(item["value"]) for item in items}


def records(result: dict[str, Any], request: QueryRequest) -> list[dict[str, Any]]:
    backend = result["backend"]
    if backend == "prometheus":
        output = []
        for row in result["metrics"]:
            samples = row["samples"]
            previous = -1
            for sample in samples:
                number(sample["value"])
                stamp = sample["timestamp_ns"]
                if (
                    type(stamp) is not int
                    or not epoch_ns(request.start) <= stamp <= epoch_ns(request.end)
                    or stamp <= previous
                ):
                    raise ValueError("invalid ordered metric samples")
                previous = stamp
            output.append({"labels": dict(row["labels"]), "samples": samples, "_in_scope": True})
        return output
    if backend == "loki":
        output = []
        for row in result["logs"]:
            try:
                body = decode(row["line"].encode())
            except (ValueError, UnicodeError):
                body = None
            output.append(
                {
                    **row,
                    "body": body,
                    "labels": dict(row["labels"]),
                    "metadata": dict(row["metadata"]),
                    "_in_scope": True,
                }
            )
        return output
    full = result["full_trace"]
    trace = json.loads(full["otlp_json"])
    attribution = {s["span_id"]: s["matches_scope"] for s in full["spans"]}
    output = []
    for resource in cast(list[dict[str, Any]], alias(trace, "resourceSpans", "batches")):
        attrs = attributes(resource.get("resource", {}).get("attributes", []))
        for group in cast(
            list[dict[str, Any]], alias(resource, "scopeSpans", "instrumentationLibrarySpans")
        ):
            for span in group.get("spans", []):
                span_id = wire_id(span["spanId"], 8)
                start, end = int(span["startTimeUnixNano"]), int(span["endTimeUnixNano"])
                output.append(
                    {
                        "trace_id": wire_id(span["traceId"], 16),
                        "span_id": span_id,
                        "parent_span_id": wire_id(span.get("parentSpanId", ""), 8, parent=True),
                        "name": span["name"],
                        "kind": str(span.get("kind", 0)),
                        "status": str(span.get("status", {}).get("code", 0)),
                        "attributes": attributes(span.get("attributes", [])),
                        "resource": attrs,
                        "start_ns": start,
                        "end_ns": end,
                        "duration_ns": end - start,
                        "events": [
                            {**e, "attributes": attributes(e.get("attributes", []))}
                            for e in span.get("events", [])
                        ],
                        "links": [
                            {
                                "span_id": wire_id(link["spanId"], 8),
                                "trace_id": wire_id(link["traceId"], 16),
                            }
                            for link in span.get("links", [])
                        ],
                        "_in_scope": attribution[span_id]
                        and start <= epoch_ns(request.end)
                        and end >= epoch_ns(request.start),
                    }
                )
    return output

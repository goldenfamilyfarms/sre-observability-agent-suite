"""Finite predicate registry. No eval, shell, unbounded regex, or generated code."""

import math
import re
from collections.abc import Iterator
from datetime import datetime
from typing import Any, cast

from ..api_wire import fields, text
from ..execution.wire import canonical
from ..telemetry.contracts import integer

# Every parameter is required, and unknown parameters are rejected before evidence reads.
PARAMETERS = {
    "present": {"path"},
    "enum": {"path", "values"},
    "bounded": {"path", "values", "max_length"},
    "range": {"path", "minimum", "maximum"},
    "excluded": {"paths", "forbidden"},
    "equals": {"path", "value"},
    "coverage": {"path"},
    "unique": {"path"},
    "hex_id": {"path", "length"},
    "timestamp": {"path"},
    "structured": set(),
    "control_chars": {"path"},
    "unicode": {"path"},
    "latency": {"path", "maximum_seconds"},
    "cardinality": {"path", "maximum"},
    "relation": {"parent", "child"},
    "links": set(),
    "graph": set(),
    "roots": set(),
    "timing": {"maximum_clock_skew_ns"},
    "mapping": {"source_path", "target_path", "pairs"},
    "event": {"name", "attributes"},
    "correlation": {"trace_path", "span_path"},
    "metric_range": {"minimum", "maximum", "minimum_samples"},
    "metric_expected": {"tolerance", "minimum_samples"},
    "available": {"minimum_samples"},
    "sample_age": {"maximum_seconds", "minimum_samples"},
    "monotonic": {"maximum_resets", "minimum_samples"},
    "buckets": {"bounds", "minimum_samples"},
}


def number(value: object) -> float:
    if type(value) not in {int, float} or not math.isfinite(float(cast(int | float, value))):
        raise ValueError("expected finite number")
    return float(cast(int | float, value))


def pointer(value: object) -> str:
    result = text(value, 256)
    if not result.startswith("/") or len(result.split("/")) > 9 or re.search(r"~(?![01])", result):
        raise ValueError("expected bounded JSON pointer")
    return result


def strings(value: object) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise ValueError("expected bounded string list")
    return [text(item, 256) for item in value]


def validate(operator: str, value: object) -> dict[str, Any]:
    if operator not in PARAMETERS:
        raise ValueError("unsupported deterministic predicate")
    result: dict[str, Any] = dict(fields(value, PARAMETERS[operator]))
    for name in ("path", "trace_path", "span_path", "source_path", "target_path"):
        if name in result:
            pointer(result[name])
    for name in ("parent", "child"):
        if name in result:
            selection(result[name])
            if not result[name]:
                raise ValueError("parent/child selectors must be explicit")
    for name in ("values", "paths", "forbidden", "attributes", "bounds"):
        if name in result and not (name == "values" and operator == "enum"):
            strings(result[name])
    if operator == "enum":
        values = result["values"]
        if (
            not isinstance(values, list)
            or not 1 <= len(values) <= 100
            or any(not isinstance(v, (str, int, float, bool)) for v in values)
        ):
            raise ValueError("enum requires a bounded scalar list")
        canonical(values)
    if operator == "mapping":
        pairs = result["pairs"]
        if not isinstance(pairs, list) or not 1 <= len(pairs) <= 100:
            raise ValueError("invalid mapping pairs")
        for pair in pairs:
            fields(pair, {"source", "target"})
            if any(not isinstance(v, (str, int, float, bool)) for v in pair.values()):
                raise ValueError("mapping requires scalar values")
        if len({canonical(pair["source"]) for pair in pairs}) != len(pairs):
            raise ValueError("ambiguous mapping")
    if operator == "timing":
        integer(result["maximum_clock_skew_ns"], 0, 60_000_000_000)
    if "paths" in result:
        for item in result["paths"]:
            pointer(item)
    for name in ("minimum", "maximum", "maximum_seconds", "tolerance"):
        if name in result:
            number(result[name])
    if "minimum" in result and result["minimum"] > result["maximum"]:
        raise ValueError("inverted range")
    if any(result.get(name, 0) < 0 for name in ("tolerance", "maximum_seconds")):
        raise ValueError("negative tolerance or age")
    for name, low in (
        ("maximum_samples", 1),
        ("minimum_samples", 1),
        ("maximum_resets", 0),
        ("max_length", 1),
    ):
        if name in result:
            integer(result[name], low, 5000)
    if operator == "cardinality":
        integer(result["maximum"], 1, 5000)
    if operator == "hex_id" and (
        type(result["length"]) is not int or result["length"] not in (16, 32)
    ):
        raise ValueError("identity must have 16 or 32 hex characters")
    if operator == "monotonic" and result["minimum_samples"] < 2:
        raise ValueError("monotonicity requires at least two samples")
    if operator == "event":
        text(result["name"], 128)
    if "value" in result and not isinstance(result["value"], (str, int, float, bool)):
        raise ValueError("expected scalar comparison value")
    if operator == "buckets":
        bounds = result["bounds"]
        parsed = [math.inf if b == "+Inf" else float(b) for b in bounds]
        if (
            bounds[-1] != "+Inf"
            or len(set(parsed)) != len(parsed)
            or parsed != sorted(parsed)
            or any(not math.isfinite(v) for v in parsed[:-1])
        ):
            raise ValueError("classic histogram requires ordered unique bounds ending in +Inf")
    return result


def selection(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or len(value) > 8:
        raise ValueError("invalid record selection")
    for key, item in value.items():
        pointer(key)
        if not isinstance(item, (str, int, float, bool)) or len(canonical(item)) > 512:
            raise ValueError("selection requires bounded scalar equality")
    return value


MISSING = object()


def get(row: object, path: str) -> Any:
    value = row
    for part in path[1:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, dict) or key not in value:
            return MISSING
        value = value[key]
    return value


def matches(row: dict[str, Any], selector: dict[str, object]) -> bool:
    return all(
        get(row, k) is not MISSING and canonical(get(row, k)) == canonical(v)
        for k, v in selector.items()
    )


def strings_in(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from strings_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings_in(item)


def evaluate(
    operator: str,
    p: dict[str, Any],
    rows: list[dict[str, Any]],
    expected: list[Any],
    start_ns: int,
    end_ns: int,
    context: list[dict[str, Any]],
) -> bool:
    if not rows:
        return False  # Absence is handled as INCONCLUSIVE by the runner, never vacuous success.
    values = [get(row, p["path"]) for row in rows] if "path" in p else []
    if values and any(v is MISSING for v in values):
        return False
    if operator == "present":
        return all(v is not MISSING and v is not None and v != "" for v in values)
    if operator == "enum":
        return all(canonical(v) in {canonical(item) for item in p["values"]} for v in values)
    if operator == "bounded":
        return all(
            isinstance(v, str) and v in p["values"] and len(v) <= p["max_length"] for v in values
        )
    if operator == "mapping":
        return all(
            any(
                get(row, p["source_path"]) is not MISSING
                and get(row, p["target_path"]) is not MISSING
                and canonical(get(row, p["source_path"])) == canonical(pair["source"])
                and canonical(get(row, p["target_path"])) == canonical(pair["target"])
                for pair in p["pairs"]
            )
            for row in rows
        )
    if operator == "equals":
        return all(v is not MISSING and canonical(v) == canonical(p["value"]) for v in values)
    if operator == "range":
        return all(p["minimum"] <= number(v) <= p["maximum"] for v in values)
    if operator == "coverage":
        return (
            bool(expected)
            and all(v is not MISSING for v in values)
            and {canonical(v) for v in values} == {canonical(v) for v in expected}
        )
    if operator == "unique":
        return all(v is not MISSING for v in values) and len({canonical(v) for v in values}) == len(
            values
        )
    if operator == "cardinality":
        return (
            all(v is not MISSING for v in values)
            and len({canonical(v) for v in values}) <= p["maximum"]
        )
    if operator == "hex_id":
        return all(
            isinstance(v, str)
            and re.fullmatch(r"[0-9a-f]{" + str(p["length"]) + "}", v)
            and int(v, 16) != 0
            for v in values
        )
    if operator == "excluded":
        # Missing forbidden fields is allowed, but selected records must be present.
        return all(
            not any(
                secret in item for item in strings_in(get(row, path)) for secret in p["forbidden"]
            )
            for row in rows
            for path in p["paths"]
            if get(row, path) is not MISSING
        )
    if operator == "structured":
        return all(isinstance(row.get("body"), dict) for row in rows)
    if operator == "control_chars":
        return all(
            isinstance(v, str) and not any(ord(c) < 32 or ord(c) == 127 for c in v) for v in values
        )
    if operator == "unicode":
        return all(
            isinstance(v, str)
            and "\ufffd" not in v
            and not any(0xD800 <= ord(c) <= 0xDFFF for c in v)
            for v in values
        )
    if operator in {"timestamp", "latency"}:
        stamps = []
        for v in values:
            if not isinstance(v, str):
                return False
            stamp = datetime.fromisoformat(v)
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                return False
            from ..telemetry.contracts import epoch_ns

            stamps.append(epoch_ns(stamp))
        if operator == "timestamp":
            return all(start_ns <= s <= end_ns for s in stamps)
        return all(
            0 <= row["timestamp_ns"] - s <= p["maximum_seconds"] * 1e9
            for row, s in zip(rows, stamps, strict=True)
        )
    if operator == "event":
        return all(
            any(
                e.get("name") == p["name"]
                and all(
                    k in e.get("attributes", {}) and e["attributes"][k] != ""
                    for k in p["attributes"]
                )
                for e in row["events"]
            )
            for row in rows
        )
    if operator == "correlation":
        pairs = {(r.get("trace_id"), r.get("span_id")) for r in context if r.get("_in_scope")}
        return all((get(row, p["trace_path"]), get(row, p["span_path"])) in pairs for row in rows)
    if operator == "links":
        return bool(expected) and all(
            {canonical(pair) for pair in expected} <= {canonical(link) for link in r["links"]}
            for r in rows
        )
    if operator == "relation":
        parents = [r for r in context if matches(r, p["parent"])]
        children = [r for r in rows if matches(r, p["child"])]
        return bool(parents and children) and all(
            any(
                c["trace_id"] == a["trace_id"] and c["parent_span_id"] == a["span_id"]
                for a in parents
            )
            for c in children
        )
    if operator in {"graph", "roots", "timing"}:
        by_id = {(r["trace_id"], r["span_id"]): r for r in context if "span_id" in r}
        if operator == "roots":
            return all(
                sum(1 for r in by_id.values() if r["trace_id"] == trace and not r["parent_span_id"])
                == 1
                for trace in {r["trace_id"] for r in rows}
            )
        for r in rows:
            parent = (
                by_id.get((r["trace_id"], r["parent_span_id"])) if r["parent_span_id"] else None
            )
            if r["parent_span_id"] and parent is None:
                return False
            if operator == "timing" and (
                r["end_ns"] < r["start_ns"]
                or parent is not None
                and r["start_ns"] < parent["start_ns"] - p["maximum_clock_skew_ns"]
            ):
                return False
        return True
    if operator in {"metric_range", "metric_expected", "available", "sample_age", "monotonic"}:
        for r in rows:
            samples = r["samples"]
            if len(samples) < p["minimum_samples"]:
                return False
            numbers = [number(s["value"]) for s in samples]
            if operator == "metric_range" and not all(
                p["minimum"] <= v <= p["maximum"] for v in numbers
            ):
                return False
            if operator == "available" and any(v != 1 for v in numbers):
                return False
            if operator == "sample_age" and not all(
                0 <= end_ns / 1e9 - v <= p["maximum_seconds"] for v in numbers
            ):
                return False
            if operator == "metric_expected":
                if len(expected) != 1 or not all(
                    abs(v - number(expected[0])) <= p["tolerance"] for v in numbers
                ):
                    return False
            if operator == "monotonic" and (
                any(v < 0 for v in numbers)
                or sum(b < a for a, b in zip(numbers, numbers[1:], strict=False))
                > p["maximum_resets"]
            ):
                return False
        return True
    if operator == "buckets":
        groups: dict[str, dict[str, dict[str, Any]]] = {}
        for r in rows:
            labels = r["labels"]
            bound = labels.get("le")
            if bound not in p["bounds"]:
                return False
            group = canonical({k: v for k, v in labels.items() if k != "le"})
            if bound in groups.setdefault(group, {}):
                return False
            groups[group][bound] = r
        for buckets in groups.values():
            if set(buckets) != set(p["bounds"]):
                return False
            ordered = [buckets[b]["samples"] for b in p["bounds"]]
            if len(ordered[0]) < p["minimum_samples"]:
                return False
            if any(
                [s["timestamp_ns"] for s in values] != [s["timestamp_ns"] for s in ordered[0]]
                for values in ordered
            ):
                return False
            for samples in zip(*ordered, strict=True):
                numbers = [number(s["value"]) for s in samples]
                if any(v < 0 for v in numbers) or numbers != sorted(numbers):
                    return False
        return True
    raise ValueError("predicate is not implemented")

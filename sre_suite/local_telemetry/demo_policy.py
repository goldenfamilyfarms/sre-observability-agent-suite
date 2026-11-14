"""Explicit opt-in mappings and independent finite-journey expectations for catalog 1.1.

These local-demo thresholds are review inputs, never inferred production SLOs.
"""

import copy
from typing import Any

from sre_suite.demo_fixture.scenarios import JOURNEY, matrix, prepare_policy
from sre_suite.execution.wire import digest


def reviewed(managed: dict[str, Any]) -> dict[str, Any]:
    raw = copy.deepcopy(managed)
    fixture = raw["fixture_policy"]
    v = fixture["verification_policy"]
    if len(v["sources"]) != 3:
        raise ValueError("demo mapping requires exactly three base sources")
    sources = {s["profile"]["backend"]: s for s in v["sources"]}
    mappings = {
        "service": "service_name",
        "environment": "deployment_environment",
        "revision": "service_version",
        "environment_id": "preview_id",
        "generation": "preview_generation",
    }
    if sources["prometheus"]["profile"]["scope_labels"] != mappings or sources["prometheus"][
        "profile"
    ]["binding_labels"] != {"artifact_digest": "artifact_digest", "plan_digest": "plan_digest"}:
        raise ValueError("local recording rules require the standard seven metric labels")
    for s in sources.values():
        s.update(step_seconds=1, limit=100)
    metric = sources["prometheus"]
    metric["profile"]["query_template"] = "sre_demo_requests_total${scope}"
    histogram = copy.deepcopy(metric)
    histogram["profile"].update(
        id="histogram", query_template="sre_demo_request_duration_seconds_bucket${scope}"
    )
    v["sources"].append(histogram)
    checks_source = copy.deepcopy(metric)
    checks_source["profile"].update(id="instrumentation", query_template="sre_demo_check${scope}")
    series = copy.deepcopy(metric)
    series.update(limit=500)
    series["profile"].update(
        id="series",
        query_template='label_join(({__name__=~"sre_demo_.*"} and ${scope}), "series", "|", '
        '"__name__", "case", "le", "signal", "unit", "check")',
    )
    v["sources"].extend([checks_source, series])
    expected = {}
    scope = v["scope"]
    cases = [row[0] for row in JOURNEY]
    routes = ["/health", "/error", "/unicode", "unknown", "unsupported"]
    server = {"/attributes/demo.sampled": True}
    error = {"/attributes/demo.case": "error"}
    rules = v["checks"]
    for key, definition in rules.items():
        if definition["applicable"]:
            # An unavailable measurement must not be substituted by another passing number.
            definition["selector"] = {"/unavailable_measurement": key}

    def rule(
        key: str,
        params: dict[str, Any],
        selector: dict[str, Any] | None = None,
        source: str | None = None,
        oracle: list[Any] | None = None,
        minimum: int = 1,
    ) -> None:
        category = key.split("-")[0]
        backend = {"TRACES": "tempo", "LOGS": "loki", "PROMQL": "prometheus"}[category]
        rules[key].update(
            parameters=params,
            selector=selector or {},
            source=source or sources[backend]["profile"]["id"],
            minimum_records=minimum,
        )
        if oracle is not None:
            expected[key] = oracle

    rule("TRACES-001", {"path": "/name"}, {"/kind": "SPAN_KIND_SERVER"})
    rule(
        "TRACES-002", {"path": "/attributes/http.request.method", "values": ["GET", "POST"]}, server
    )
    rule(
        "TRACES-003",
        {
            "path": "/attributes/http.route",
            "values": routes + ["/fixtures/http-journey"],
            "max_length": 80,
        },
    )
    rule(
        "TRACES-004",
        {"path": "/attributes/http.response.status_code", "minimum": 200, "maximum": 500},
    )
    rule("TRACES-005", {"path": "/duration_ns", "minimum": 0, "maximum": 1_000_000_000})
    rule("TRACES-024", {})
    rule("TRACES-026", {})
    rule("TRACES-027", {"path": "/span_id", "length": 16})
    rule("TRACES-028", {"path": "/trace_id", "length": 32})
    for key, path, value in (
        ("029", "service.name", scope["service"]),
        ("030", "service.version", scope["revision"]),
        ("031", "deployment_environment", scope["environment"]),
    ):
        rule("TRACES-" + key, {"path": "/resource/" + path, "value": value})
    rule(
        "TRACES-032",
        {"name": "exception", "attributes": ["exception.type", "exception.message"]},
        error,
    )
    rule("TRACES-036", {"path": "/attributes/demo.sampled"}, oracle=[True])
    rule(
        "TRACES-037",
        {
            "paths": ["/attributes", "/resource"],
            "forbidden": ["DEMO_SECRET_CANARY", "synthetic-model-key"],
        },
    )
    rule("TRACES-038", {"maximum_clock_skew_ns": 0})
    rule(
        "TRACES-040",
        {"path": "/attributes/demo.case"},
        {"/kind": "SPAN_KIND_INTERNAL"},
        oracle=cases,
        minimum=6,
    )
    # Tempo represents enum fields as names; records preserve that wire spelling.
    rule(
        "TRACES-006",
        {
            "source_path": "/attributes/http.response.status_code",
            "target_path": "/status",
            "pairs": [{"source": 500, "target": "STATUS_CODE_ERROR"}],
        },
        error,
    )
    rule("LOGS-001", {})
    rule("LOGS-002", {"path": "/body/timestamp"})
    rule("LOGS-003", {"path": "/body/severity"})
    rule(
        "LOGS-004",
        {
            "source_path": "/body/status",
            "target_path": "/body/severity",
            "pairs": [
                {"source": s, "target": "ERROR" if s == 500 else "INFO"}
                for s in (200, 404, 405, 500)
            ],
        },
    )
    for key, path, value in (
        ("005", "service", scope["service"]),
        ("006", "revision", scope["revision"]),
        ("007", "environment", scope["environment"]),
    ):
        rule("LOGS-" + key, {"path": "/body/" + path, "value": value})
    rule("LOGS-008", {"path": "/body/trace_id", "length": 32})
    rule("LOGS-009", {"path": "/body/span_id", "length": 16})
    rule("LOGS-010", {"trace_path": "/body/trace_id", "span_path": "/body/span_id"})
    rule("LOGS-011", {"path": "/body/method"})
    rule("LOGS-012", {"path": "/body/status", "minimum": 200, "maximum": 500})
    rule("LOGS-013", {"path": "/body/route", "values": routes, "max_length": 80})
    trace_id = sources["tempo"]["trace_id"]
    rule("LOGS-014", {"path": "/body/trace_id"}, oracle=[trace_id])
    rule("LOGS-020", {"path": "/body/exception_type"}, {"/body/case": "error"})
    rule("LOGS-021", {"path": "/body/exception_stack"}, {"/body/case": "error"})
    for key in ("022", "026", "027", "028"):
        rule(
            "LOGS-" + key,
            {"paths": ["/body"], "forbidden": ["DEMO_SECRET_CANARY", "synthetic-model-key"]},
        )
    rule("LOGS-030", {"path": "/body/message"})
    rule("LOGS-031", {})
    rule("LOGS-032", {"path": "/body/message"}, {"/body/case": "unicode"})
    rule("LOGS-033", {"path": "/body/event_id"})
    for key in ("035", "039"):
        rule("LOGS-" + key, {"path": "/body/case"}, oracle=cases, minimum=6)
    rule("LOGS-037", {"path": "/labels/preview_id", "value": scope["environment_id"]})
    rule("LOGS-038", {"path": "/labels/service_name", "maximum": 1})
    rule("LOGS-040", {"path": "/body/severity", "values": ["INFO", "ERROR"]})
    for key in ("003", "014"):
        rule(
            "PROMQL-" + key,
            {"bounds": ["0.001", "0.01", "0.1", "1.0", "+Inf"], "minimum_samples": 2},
            source="histogram",
            minimum=30,
        )
    rule("PROMQL-005", {"path": "/labels/case", "maximum": 6})
    for key, path, value in (
        ("006", "service_name", scope["service"]),
        ("007", "deployment_environment", scope["environment"]),
        ("008", "artifact_digest", "sha256:" + digest(raw["recipe"])),
    ):
        rule("PROMQL-" + key, {"path": "/labels/" + path, "value": value})
    rule("PROMQL-013", {"maximum_resets": 0, "minimum_samples": 2})
    rule("PROMQL-037", {"path": "/labels/case"}, oracle=cases, minimum=6)

    def measured(key: str, params: dict[str, Any], oracle: list[Any] | None = None) -> None:
        rule(key, params, {"/labels/check": key}, source="instrumentation", oracle=oracle)

    for key, low, high in (
        ("PROMQL-001", 0.01, 6000),
        ("PROMQL-002", 0.001, 1000),
        ("PROMQL-011", 0, 0.9),
        ("PROMQL-012", 0, 0.8),
        ("PROMQL-020", 0, 60),
        ("PROMQL-021", 1, 512 * 1024**2),
        ("PROMQL-022", 0, 3600),
        ("PROMQL-023", 1, 256),
        ("PROMQL-034", 0, 2),
        ("PROMQL-035", 0, 0),
        ("TRACES-039", 1, 1),
        ("LOGS-034", 0, 10),
        ("LOGS-036", 86400, 86400),
    ):
        measured(key, {"minimum": low, "maximum": high, "minimum_samples": 2})
    measured("PROMQL-009", {"minimum_samples": 2})
    measured("PROMQL-010", {"maximum_seconds": 3, "minimum_samples": 2})
    measured("PROMQL-015", {"path": "/labels/unit", "value": "seconds"})
    rule("PROMQL-036", {"path": "/labels/series", "maximum": 150}, source="series")
    # The fixed journey independently specifies one server error in six requests and
    # a 1 ms latency objective. A poor availability SLI is still a correct calculation.
    for key, value, tolerance in (
        ("PROMQL-004", 0.0005, 0.0005),
        ("PROMQL-038", 5 / 6, 1e-9),
        ("PROMQL-039", 1, 1e-9),
        ("PROMQL-040", (1 / 6) / 0.01, 1e-9),
    ):
        measured(key, {"tolerance": tolerance, "minimum_samples": 2}, [value])
    if any("/unavailable_measurement" in r["selector"] for r in rules.values() if r["applicable"]):
        raise ValueError("reviewed demo instrumentation mapping is incomplete")
    for key, item in matrix().items():
        if item["expectation_required"] and key not in expected:
            expected[key] = fixture["checks"][key]["expected"]
    rebuilt = prepare_policy(v, expected, fixture["target"], fixture["runner"])
    rebuilt["runner_queue"], rebuilt["assessor_queue"] = (
        fixture["runner_queue"],
        fixture["assessor_queue"],
    )
    raw["fixture_policy"] = rebuilt
    return raw

"""Independent test policy for real demo fields; absent capabilities never acquire fake values.

This is deliberately a non-passing acceptance policy: current demo instrumentation lacks
process/scrape/retention/SLI evidence. Those applicable checks stay inconclusive or fail.
"""

import copy

from sre_suite.demo_fixture.scenarios import JOURNEY, matrix, prepare_policy
from sre_suite.execution.wire import digest


def reviewed(managed):
    raw = copy.deepcopy(managed)
    fixture = raw["fixture_policy"]
    v = fixture["verification_policy"]
    sources = {s["profile"]["backend"]: s for s in v["sources"]}
    for s in sources.values():
        s.update(step_seconds=1, limit=100)
    metric = sources["prometheus"]
    metric["profile"]["query_template"] = "sre_demo_requests_total${scope}"
    histogram = copy.deepcopy(metric)
    histogram["profile"].update(
        id="histogram", query_template="sre_demo_request_duration_seconds_bucket${scope}"
    )
    v["sources"].append(histogram)
    expected = {}
    scope = v["scope"]
    cases = [row[0] for row in JOURNEY]
    routes = ["/health", "/error", "/unicode", "unknown", "unsupported"]
    server = {"/attributes/demo.sampled": True}
    error = {"/attributes/demo.case": "error"}
    rules = v["checks"]
    for key, rule in rules.items():
        if rule["applicable"]:
            # An unavailable measurement must not be substituted by another passing number.
            rule["selector"] = {"/unavailable_measurement": key}

    def rule(key, params, selector=None, source=None, oracle=None, minimum=1):
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

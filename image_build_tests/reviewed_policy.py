"""Fixed reference-only integration oracle. Unmeasured concerns stay inconclusive.

All 120 slots remain applicable here: this intentionally conservative acceptance
profile neither imports the demo's exclusions nor claims absent measurements pass.
"""

from dataclasses import asdict

from sre_suite.certification import load_catalog
from sre_suite.verification.contracts import HEALTH_CHECKS, catalog_digest
from tests.verification_fixture import case


def reviewed(scope, candidate, image, config):
    catalog = load_catalog()
    sources = []
    for backend in ("prometheus", "loki", "tempo"):
        labels = dict(
            service="service.name",
            environment="deployment.environment.name",
            revision="sre.source.revision",
            environment_id="sre.environment.id",
            generation="sre.environment.generation",
        )
        binding = dict(artifact_digest="sre.artifact.digest", plan_digest="sre.plan.digest")
        if backend != "tempo":
            labels = {k: v.replace(".", "_") for k, v in labels.items()}
            binding = {k: v.replace(".", "_") for k, v in binding.items()}
        sources.append(
            {
                "profile": {
                    "id": backend,
                    "backend": backend,
                    "base_url": config.url(backend),
                    "authorization_env": "SRE_TELEMETRY_" + backend.upper() + "_AUTH",
                    "tenant_env": None,
                    "ca_env": "SRE_TELEMETRY_CA_FILE",
                    "query_template": "sre_target_requests_total${scope}"
                    if backend == "prometheus"
                    else "${scope}",
                    "scope_labels": labels,
                    "binding_labels": binding,
                    "allow_full_traces": backend == "tempo",
                },
                "trace_id": "a" * 32 if backend == "tempo" else None,
                "step_seconds": 1,
                "limit": 100,
            }
        )
    checks, oracle = {}, {}
    for c in catalog.checks:
        params, _, expected, _ = case(c)
        backend = (
            "prometheus"
            if c.id in HEALTH_CHECKS
            else {"traces": "tempo", "logs": "loki", "promql": "prometheus"}[c.category.value]
        )
        checks[c.id] = dict(
            applicable=True,
            source=backend,
            selector={"/unavailable_measurement": c.id},
            parameters=params,
            minimum_records=1,
        )
        oracle[c.id] = dict(scenario="health", expected=expected, justification="")
    measured = []

    def rule(key, parameters, selector=None, expected=None, scenario="health"):
        checks[key].update(parameters=parameters, selector=selector or {})
        oracle[key].update(scenario=scenario, expected=expected or [])
        measured.append(key)

    rule("TRACES-001", {"path": "/name"})
    rule("TRACES-002", {"path": "/attributes/http.request.method", "values": ["GET"]})
    rule(
        "TRACES-003",
        {
            "path": "/attributes/http.route",
            "values": ["/health", "/unicode", "/error"],
            "max_length": 20,
        },
    )
    rule("TRACES-005", {"path": "/duration_ns", "minimum": 1, "maximum": 10**9})
    rule("TRACES-027", {"path": "/span_id", "length": 16})
    rule("TRACES-028", {"path": "/trace_id", "length": 32})
    for key, attr, value in (
        ("029", "service.name", scope.service),
        ("030", "service.version", candidate),
        ("031", "deployment.environment.name", str(scope.environment)),
    ):
        rule("TRACES-" + key, {"path": "/resource/" + attr, "value": value})
    rule(
        "TRACES-032",
        {"name": "exception", "attributes": ["exception.type", "exception.message"]},
        {"/attributes/http.route": "/error"},
        scenario="error",
    )
    rule("LOGS-001", {})
    rule("LOGS-003", {"path": "/body/severity"})
    rule(
        "LOGS-004",
        {
            "source_path": "/body/status",
            "target_path": "/body/severity",
            "pairs": [{"source": 200, "target": "INFO"}, {"source": 500, "target": "ERROR"}],
        },
    )
    for key, attr, value in (
        ("005", "service.name", scope.service),
        ("006", "service.version", candidate),
        ("007", "deployment.environment.name", str(scope.environment)),
    ):
        rule("LOGS-" + key, {"path": "/body/resource/" + attr, "value": value})
    rule("LOGS-008", {"path": "/body/trace_id", "length": 32})
    rule("LOGS-009", {"path": "/body/span_id", "length": 16})
    rule("LOGS-010", {"trace_path": "/body/trace_id", "span_path": "/body/span_id"})
    rule("LOGS-012", {"path": "/body/status", "minimum": 200, "maximum": 500})
    rule(
        "LOGS-013",
        {"path": "/body/route", "values": ["/health", "/unicode", "/error"], "max_length": 20},
        scenario="unicode",
    )
    rule("LOGS-014", {"path": "/body/trace_id"}, expected=["a" * 32])
    rule(
        "LOGS-020",
        {"path": "/body/exception/exception.type"},
        {"/body/route": "/error"},
        scenario="error",
    )
    rule("LOGS-031", {})
    rule("LOGS-037", {"path": "/labels/sre_environment_id", "value": scope.environment_id})
    rule("LOGS-038", {"path": "/labels/service_name", "maximum": 1})
    rule("LOGS-040", {"path": "/body/severity", "values": ["INFO", "ERROR"]})
    rule("PROMQL-005", {"path": "/labels/route", "maximum": 3})
    for key, label, val in (
        ("006", "service_name", scope.service),
        ("007", "deployment_environment_name", str(scope.environment)),
        ("008", "sre_artifact_digest", image),
    ):
        rule("PROMQL-" + key, {"path": "/labels/" + label, "value": val})
    rule("PROMQL-013", {"maximum_resets": 0, "minimum_samples": 2})
    rule("PROMQL-037", {"path": "/labels/route"}, expected=["/health", "/unicode", "/error"])
    return (
        dict(
            schema_version=2,
            scope=asdict(scope),
            collector="collector",
            assessor="assessor",
            verifier="verifier",
            catalog_sha256=catalog_digest(catalog),
            sources=sources,
            checks=checks,
        ),
        oracle,
        measured,
    )

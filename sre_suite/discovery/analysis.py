"""Versioned static indicators and conservative assessment; never runtime certification."""

import re
from dataclasses import asdict, dataclass
from typing import Any

from ..execution.wire import digest
from .contracts import AnalysisRequest


@dataclass(frozen=True)
class Indicator:
    id: str
    pattern: str
    kind: str


INDICATORS = (
    Indicator("spring", r"org\.springframework|spring-boot", "framework"),
    Indicator("python-web", r"(?:from|import) (?:flask|fastapi|django)|http\.server", "framework"),
    Indicator(
        "http-server",
        r"@(?:RestController|RequestMapping|GetMapping|PostMapping)|@app\.(?:get|post|route)|BaseHTTPRequestHandler",
        "interaction",
    ),
    Indicator(
        "http-client",
        r"WebClient|RestTemplate|HttpClient|requests\.(?:get|post)|httpx\.",
        "interaction",
    ),
    Indicator(
        "database",
        r"java\.sql|JdbcTemplate|JpaRepository|sqlalchemy|psycopg|sqlite3",
        "interaction",
    ),
    Indicator(
        "kafka", r"KafkaTemplate|KafkaListener|org\.apache\.kafka|confluent_kafka", "interaction"
    ),
    Indicator(
        "pubsub", r"com\.google\.cloud\.pubsub|google\.cloud\.pubsub|PubSubTemplate", "interaction"
    ),
    Indicator(
        "background",
        r"@Scheduled|@Async|ScheduledExecutor|celery|asyncio\.create_task",
        "interaction",
    ),
    Indicator(
        "otel",
        r"io\.opentelemetry|opentelemetry\.(?:api|sdk|trace|metrics)|opentelemetry-javaagent",
        "instrumentation",
    ),
    Indicator(
        "correlation", r"trace_id|traceId|span_id|spanId|LoggingInstrumentor", "instrumentation"
    ),
    Indicator(
        "metrics",
        r"io\.micrometer|prometheus_client|MeterRegistry|OpenTelemetryMeter",
        "instrumentation",
    ),
    Indicator(
        "propagation",
        r"TextMapPropagator|W3CTraceContextPropagator|propagate\.(?:inject|extract)",
        "instrumentation",
    ),
    Indicator("tests", r"@Test|unittest|pytest|(?:it|test|describe)\(", "validation"),
)
POLICY_SHA256 = digest(
    {
        "version": "static-assessment-v1",
        "draft_template": "review-controls-interactions-certification-v1",
        "scoring": "five-controls-equal-weight-unverified-v1",
        "indicators": [asdict(i) for i in INDICATORS],
        "controls": ["traces", "logs", "metrics", "propagation", "tests"],
        "semantics": "static indicators only; absence unknown; runtime verification required",
    }
)


def discover(
    request: AnalysisRequest, inventory: dict[str, Any], contents: dict[str, str]
) -> dict[str, Any]:
    findings = []
    for item in inventory["files"]:
        for rule in INDICATORS:
            for line, content in enumerate(contents[item["path"]].splitlines(), 1):
                if re.search(rule.pattern, content):
                    findings.append(
                        {
                            "indicator": rule.id,
                            "kind": rule.kind,
                            "confidence": "static-indicator",
                            "path": item["path"],
                            "sha256": item["sha256"],
                            "line": line,
                        }
                    )
                    break
    if len(findings) > 400:
        raise ValueError("discovery findings exceed budget; narrow selected source")
    interactions = sorted({f["indicator"] for f in findings if f["kind"] == "interaction"})
    return {
        "schema_version": 2,
        "kind": "architecture-manifest",
        "request": request.wire(),
        "request_sha256": request.fingerprint,
        "policy_sha256": POLICY_SHA256,
        "source": inventory,
        "findings": findings,
        "graph": {
            "nodes": ["service", *interactions],
            "edges": [
                {
                    "from": "service",
                    "to": name,
                    "relation": "static-interaction-indicator",
                    "confidence": "static-indicator",
                }
                for name in interactions
            ],
        },
        "coverage": "explicit-selected-files-only",
        "runtime_verified": False,
    }


def assess(manifest: dict[str, Any]) -> dict[str, Any]:
    observed = {f["indicator"] for f in manifest["findings"]}
    indicators = {
        "traces": "otel",
        "logs": "correlation",
        "metrics": "metrics",
        "propagation": "propagation",
        "tests": "tests",
    }
    controls = [
        {
            "id": name,
            "status": "indicator-present" if rule in observed else "not-observed",
            "action": "validate" if rule in observed else "investigate-and-instrument",
            "evidence": [f for f in manifest["findings"] if f["indicator"] == rule],
        }
        for name, rule in indicators.items()
    ]
    present = sum(c["status"] == "indicator-present" for c in controls)
    return {
        "controls": controls,
        "maturity": {
            "level": "unverified",
            "static_coverage_score": present * 20,
            "observed_controls": present,
            "total_controls": len(controls),
            "runtime_verified_controls": 0,
        },
        "gaps": [c["id"] for c in controls if c["status"] == "not-observed"],
        "interactions": sorted(f["to"] for f in manifest["graph"]["edges"]),
        "frameworks": sorted(
            {f["indicator"] for f in manifest["findings"] if f["kind"] == "framework"}
        ),
        "runtime_verified": False,
        "certified": False,
    }

"""Opt-in export of measured fixture client spans; independent service spans stay untouched."""

from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from ..api_wire import fields, text
from ..demo_fixture.export import acknowledge
from ..execution.config import https_url
from ..execution.http import JsonHttp
from ..execution.journal import Journal
from ..execution.wire import digest


def validate(value: object) -> dict[str, Any]:
    raw = dict(fields(value, {"base_url", "authorization_env", "ca_env"}))
    if raw["authorization_env"] != "SRE_JOURNEY_TRACES_AUTH" or raw["ca_env"] not in {
        None,
        "SRE_JOURNEY_TRACE_CA_FILE",
    }:
        raise ValueError("dedicated journey export credentials required")
    url = https_url(text(raw["base_url"]), allow_loopback=bool(raw["ca_env"]))
    if urlsplit(url).path:
        raise ValueError("exact trace export origin required")
    return raw


def publish(
    configuration: dict[str, Any],
    env: Mapping[str, str],
    resource: dict[str, str],
    responses: list[dict[str, Any]],
    journal: Journal,
    guard: Callable[[], None],
) -> None:
    guard()
    measurements = [step["client_trace"] for row in responses for step in row["result"]["steps"]]
    if (
        not measurements
        or len(measurements) > 40
        or len({r["trace_id"] for r in measurements}) != 1
    ):
        raise ValueError("bounded single trace journey required")
    trace = measurements[0]["trace_id"]
    root = digest([responses[0]["binding"], "fixture-client-root", trace])[:16]
    spans = [
        {
            "traceId": trace,
            "spanId": root,
            "name": "fixture journey",
            "kind": 1,
            "startTimeUnixNano": str(min(r["start_ns"] for r in measurements)),
            "endTimeUnixNano": str(max(r["end_ns"] for r in measurements)),
        }
    ]
    for row in measurements:
        spans.append(
            {
                "traceId": trace,
                "spanId": row["span_id"],
                "parentSpanId": root,
                "name": row["method"] + " " + row["path"],
                "kind": 3,
                "startTimeUnixNano": str(row["start_ns"]),
                "endTimeUnixNano": str(row["end_ns"]),
                "attributes": [
                    {"key": "http.response.status_code", "value": {"intValue": str(row["status"])}}
                ],
            }
        )
    identity = resource | {"service.name": "sre-fixture-runner"}
    payload = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": k, "value": {"stringValue": v}} for k, v in sorted(identity.items())
                    ]
                },
                "scopeSpans": [
                    {"scope": {"name": "sre-fixture-client", "version": "1"}, "spans": spans}
                ],
            }
        ]
    }
    token = env[configuration["authorization_env"]]
    if not token or any(ord(c) < 32 or ord(c) > 126 for c in token):
        raise ValueError("invalid journey export token")
    http = JsonHttp(
        configuration["base_url"],
        token,
        timeout=5,
        expected_status=200,
        local_ca_file=env[configuration["ca_env"]] if configuration["ca_env"] else None,
    )

    def send() -> dict[str, bool]:
        guard()
        acknowledge("traces", http.call("POST", "/v1/traces", payload))
        guard()
        return {"exported": True}

    journal.compute("journey-trace-" + digest(responses[0]["binding"]), payload, send, guard)
    guard()

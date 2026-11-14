"""Export a standalone reference and assemble fixture policy from independently reviewed checks."""

import copy
from pathlib import Path
from typing import Any

from ..api_wire import fields
from ..control.contracts import wire
from ..fixtures.contracts import FixturePolicy, FixtureRequest
from . import reference


def export_reference(output: Path) -> None:
    # Never overwrite a checkout or introduce unreviewed template dependencies.
    output.mkdir(mode=0o700)
    source = Path(reference.__file__).read_text()
    (output / "reference_service.py").write_text(source)
    (output / "README.md").write_text(
        "# Python target service\n\nStandard library only. Build reference_service.py "
        "with a pinned "
        'Python 3.11+ base and entrypoint ["python","/app/reference_service.py"]. '
        "Use /ready for Kubernetes probes. TLS must terminate at the private ingress. "
        "Supply the eight bound OTEL_RESOURCE_ATTRIBUTES, distinct SRE_TARGET_TOKEN and "
        "SRE_TARGET_METRICS_TOKEN, and SRE_TARGET_TRACES_URL/AUTH and SRE_TARGET_LOGS_URL/AUTH. "
        "The state file retains a bounded durable outbox across process restarts; mount persistent "
        "storage and pass --state for persistence across pod replacement. This reference refuses "
        "production. See the suite image-build-and-target-fixtures guide.\n"
    )


def prepare(value: object) -> dict[str, Any]:
    raw = fields(
        value,
        {
            "plan",
            "verification_policy",
            "checks",
            "runner",
            "runner_queue",
            "assessor_queue",
            "origin",
            "target_service",
        }
        | ({"journey_export"} if isinstance(value, dict) and "journey_export" in value else set()),
    )
    plan = wire(raw["plan"])
    if plan.get("artifact_kind") != "container-image-v1":
        raise ValueError("image verification input required")
    origin = dict(fields(raw["origin"], {"base_url", "authorization_env", "ca_env"}))
    scenarios, routes = [], {}
    for name, path, assertions in (
        ("health", "/health", {"/steps/0/status": 200, "/steps/0/body/status": "ok"}),
        (
            "unicode",
            "/unicode",
            {"/steps/0/status": 200, "/steps/0/body/message": "Hello, 世界 🌾"},
        ),
        ("error", "/error", {"/steps/0/status": 500}),
    ):
        route = "/reference/" + name
        scenarios.append({"id": name, "path": route, "payload": {}, "assertions": assertions})
        routes[route] = [
            {
                "origin": "service",
                "method": "GET",
                "path": path,
                "payload": None,
                "success_status": 200,
            }
        ]
    policy_raw = {
        "schema_version": 2,
        "verification_policy": copy.deepcopy(raw["verification_policy"]),
        "runner": raw["runner"],
        "runner_queue": raw["runner_queue"],
        "assessor_queue": raw["assessor_queue"],
        "target": {
            "base_url": origin["base_url"],
            "authorization_env": "SRE_FIXTURE_TARGET_AUTH",
            "ca_env": "SRE_FIXTURE_TARGET_CA_FILE" if origin["ca_env"] else None,
        },
        "scenarios": scenarios,
        "checks": copy.deepcopy(raw["checks"]),
        "journeys": {"origins": {"service": origin}, "routes": routes},
        "target_service": copy.deepcopy(raw["target_service"]),
    }
    if "journey_export" in raw:
        policy_raw["journey_export"] = copy.deepcopy(raw["journey_export"])
    # All 120 oracle/applicability entries must be reviewed independently of target output.
    policy = FixturePolicy.parse(policy_raw)
    request = {"plan": plan, "dispatch": policy.dispatch}
    parsed = FixtureRequest.parse(request)
    parsed.authorize(policy)
    return {"fixture_policy": policy_raw, "fixture_request": request}

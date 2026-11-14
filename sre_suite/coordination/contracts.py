"""A frozen dependency graph with explicit approval and finite correction budgets."""

from dataclasses import dataclass
from typing import Any

from ..api_wire import envelope, fields
from ..control.contracts import Binding, identifier, sha, timestamp, wire
from ..execution.wire import digest
from ..fixtures.contracts import queue
from ..telemetry.contracts import integer

# Workflows retain their own input contracts and authority checks.
CHILDREN = {
    "engineering": "TicketExecution",
    "discovery": "ArchitectureAssessmentPlanning",
    "telemetry": "TelemetryCollection",
    "certification": "ServiceCertification",
    "fixtures": "FixtureAssessment",
    "delivery": "ReleaseDelivery",
}
LOCAL = {
    "epic",
    "publish",
    "handoff",
    "steward",
    "shipping",
    "analysis",
    "image-build",
    "image-publish",
}


@dataclass(frozen=True)
class SuitePlan:
    raw: dict[str, Any]
    binding: Binding
    nodes: tuple[dict[str, Any], ...]

    @classmethod
    def parse(cls, value: object) -> "SuitePlan":
        raw = envelope(value, {"binding", "expires_at", "nodes", "task_queue", "source"})
        binding = Binding.parse(raw["binding"])
        if binding.action not in {"execute", "remediate"}:
            raise ValueError("suite execution authorization required")
        queue(raw["task_queue"])
        timestamp(raw["expires_at"])
        source = fields(raw["source"], {"repository", "base_branch"})
        # Reuse ShippingAgent's stricter repository/ref syntax without accepting a URL.
        import re

        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", str(source["repository"])):
            raise ValueError("source repository required")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_/-]{0,99}", str(source["base_branch"])):
            raise ValueError("source branch required")
        items = raw["nodes"]
        if not isinstance(items, list) or not 1 <= len(items) <= 40:
            raise ValueError("suite requires 1..40 bounded nodes")
        nodes = []
        seen: set[str] = set()
        for item in items:
            n = wire(
                fields(
                    item,
                    {
                        "id",
                        "kind",
                        "depends_on",
                        "task_queue",
                        "request_sha256",
                        "corrections",
                        "checkpoint_after",
                    },
                )
            )
            identifier(n["id"])
            queue(n["task_queue"])
            sha(n["request_sha256"])
            if n["id"] in seen or n["kind"] not in CHILDREN.keys() | LOCAL:
                raise ValueError("duplicate node or unsupported agent")
            if not isinstance(n["depends_on"], list) or len(set(n["depends_on"])) != len(
                n["depends_on"]
            ):
                raise ValueError("unique dependency list required")
            if not set(n["depends_on"]) <= seen:
                raise ValueError("nodes must be in topological order")
            if type(n["checkpoint_after"]) is not bool:
                raise ValueError("explicit checkpoint flag required")
            corrections = n["corrections"]
            if not isinstance(corrections, list) or len(corrections) > 2:
                raise ValueError("at most two separately reviewed correction attempts")
            if corrections and (binding.action != "remediate" or n["kind"] != "engineering"):
                raise ValueError("corrections require explicit remediation authorization")
            for attempt in corrections:
                sha(attempt)
            if len(set([n["request_sha256"], *corrections])) != 1 + len(corrections):
                raise ValueError("correction attempts must be distinct")
            seen.add(n["id"])
            nodes.append(n)
        expected = digest({k: raw[k] for k in ("nodes", "source", "task_queue", "expires_at")})
        if binding.target != {
            "environment": str(binding.scope.environment),
            "graph_sha256": expected,
        }:
            raise ValueError("approval must bind the complete dependency graph")
        integer(binding.context_version, 0, 2**63 - 1)
        return cls(wire(raw), binding, tuple(nodes))

    @property
    def identity(self) -> str:
        return "sre-suite-" + self.binding.identity


def passed(kind: str, result: dict[str, Any]) -> bool:
    """A completed process is not an accepted gate. Unknown results fail closed."""
    if kind == "image-build":
        return result.get("outcome") == "built" and isinstance(result.get("image_digest"), str)
    if kind == "image-publish":
        return result.get("outcome") == "attested" and isinstance(result.get("build"), dict)
    if kind == "engineering":
        return result.get("outcome") == "ready_for_review" and result.get("jira_reported") is True
    if kind == "discovery":
        return result.get("outcome") == "plan_proposed"
    if kind == "telemetry":
        rows = result.get("collections")
        return (
            result.get("outcome") == "collected"
            and isinstance(rows, list)
            and bool(rows)
            and all(r.get("status") == "observed" for r in rows)
        )
    if kind == "certification":
        return result.get("verdict") == "PASSED" and result.get("evidence") is not None
    if kind == "fixtures":
        return result.get("outcome") == "assessed" and result.get("evidence") is not None
    if kind == "delivery":
        return (
            result.get("accepted") is True
            or result.get("complete") is True
            or (result.get("ready") is True and result.get("outcome") == "ready_for_verification")
        )
    if kind == "epic":
        return result.get("outcome") == "epic_prepared"
    if kind == "publish":
        return result.get("outcome") == "tickets_prepared"
    if kind == "handoff":
        return result.get("preflight_exit_code") == 0 and result.get("execution_started") is False
    if kind == "shipping":
        return result.get("merged") is True
    if kind == "steward":
        return result.get("remaining") == 0 and all(
            r.get("state") == "verified" for r in result.get("results", [])
        )
    return kind == "analysis" and result.get("passed") is True

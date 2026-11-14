"""Failed certification becomes linked reviewable Jira work, never an approval bypass."""

from dataclasses import asdict
from typing import Any

from ..contracts import Environment, Evidence, Scope
from ..execution.wire import digest
from ..jira_planning.contracts import Plan
from .runtime import regression


def propose(
    report: dict[str, Any],
    evidence: Evidence,
    template: object,
    scope: Scope,
    artifact: str,
    plan_digest: str,
) -> dict[str, Any]:
    plan = Plan.parse(template)
    if scope.environment == Environment.PRODUCTION or plan.scope != scope:
        raise ValueError("remediation must target the exact authorized nonproduction scope")
    validation = regression(
        {"baseline": report, "candidate": report},
        scope,
        artifact,
        plan_digest,
        {"baseline_artifact_digest": artifact, "baseline_plan_digest": plan_digest},
    )
    failures = validation["unresolved"]
    if not failures:
        raise ValueError("no failed or inconclusive checks to remediate")
    prototype = plan.json()["nodes"][0]["requirements"]
    nodes = []
    for signal in ("TRACES", "LOGS", "PROMQL"):
        checks = [check for check in failures if check.startswith(signal + "-")]
        if not checks:
            continue
        name = "repair-" + signal.lower()
        nodes.append(
            {
                "id": name,
                "summary": "Remediate " + signal.lower() + " certification gaps",
                "depends_on": [],
                "requirements": {
                    **prototype,
                    "current_state": "Failed/inconclusive checks: " + ", ".join(checks),
                    "desired_state": (
                        "Independent assertions pass without weakening applicability or thresholds"
                    ),
                    "objective": "Diagnose and repair evidence-backed " + signal.lower() + " gaps",
                    "acceptance_criteria": [
                        "Resolve " + c + " with positive and negative evidence" for c in checks
                    ],
                    "evidence": [asdict(evidence)],
                },
            }
        )
    nodes.append(
        {
            "id": "regression-certification",
            "summary": "Run full regression certification",
            "depends_on": [n["id"] for n in nodes],
            "requirements": {
                **prototype,
                "current_state": "Remediation changes require independent regression testing",
                "desired_state": (
                    "Complete 120-check report for the new candidate with no hidden regressions"
                ),
                "objective": "Verify remediation and all previously passing checks",
                "acceptance_criteria": [
                    "Run all 120 checks; no removed checks or weakened assertions",
                    "Retain exact candidate, plan, environment ID and generation provenance",
                ],
                "evidence": [asdict(evidence)],
            },
        }
    )
    raw = {
        "schema_version": 2,
        "id": "remediation-" + digest([plan.fingerprint, evidence.sha256])[:40],
        "project": plan.project,
        "repository": plan.repository,
        "scope": asdict(scope),
        "nodes": nodes,
    }
    from ..control.contracts import wire

    proposed = Plan.parse(wire(raw))
    return {
        "passed": False,
        "outcome": "remediation_proposed",
        "diagnosis": "independent_certification_failures",
        "failed_checks": failures,
        "source_report": evidence.id,
        "plan": proposed.json(),
        "plan_sha256": proposed.fingerprint,
        "jira_published": False,
        "execution_started": False,
    }

"""Revalidate authenticated provenance and deterministically regenerate the proposed plan."""

import json
import re
from dataclasses import asdict
from datetime import datetime
from typing import Any

from ..api import evidence_id
from ..api_wire import decode, fields
from ..contracts import Evidence
from ..execution.evidence_client import EvidenceClient
from ..execution.wire import canonical, digest
from .analysis import INDICATORS, POLICY_SHA256, assess
from .contracts import AnalysisRequest, DiscoveryOrigin


def read_reference(
    api: EvidenceClient, ref: Evidence, request: AnalysisRequest, role: str, now: datetime
) -> dict[str, Any]:
    producer = request.intake_subject if role == "intake" else request.assessor_subject
    if (
        not ref.valid_for(request.scope, now)
        or now >= request.expires_at
        or ref.expires_at != request.expires_at
        or ref.producer != producer
        or ref.id != evidence_id(producer, request.scope, role + "-" + request.fingerprint)
    ):
        raise ValueError("analysis reference is stale or outside its immutable execution")
    api.context(request.scope, request.context_version)
    doc = decode(api.read(ref))
    if not isinstance(doc, dict):
        raise ValueError("invalid analysis evidence")
    api.context(request.scope, request.context_version)
    return doc


def validate_manifest(doc: dict[str, Any], request: AnalysisRequest) -> None:
    fields(
        doc,
        {
            "schema_version",
            "kind",
            "request",
            "request_sha256",
            "policy_sha256",
            "source",
            "findings",
            "graph",
            "coverage",
            "runtime_verified",
        },
    )
    if (
        doc["schema_version"] != 2
        or doc["kind"] != "architecture-manifest"
        or canonical(doc["request"]) != canonical(request.wire())
        or doc["request_sha256"] != request.fingerprint
        or doc["policy_sha256"] != POLICY_SHA256
        or doc["coverage"] != "explicit-selected-files-only"
        or doc["runtime_verified"] is not False
    ):
        raise ValueError("architecture manifest binding mismatch")
    source = fields(doc["source"], {"kind", "repository", "revision", "files", "artifact_digest"})
    inventory = {k: v for k, v in source.items() if k != "artifact_digest"}
    if (
        source["kind"] != "source-snapshot-v1"
        or source["repository"] != request.repository
        or source["revision"] != request.scope.revision
        or source["artifact_digest"] != request.source_artifact_digest
        or "sha256:" + digest(inventory) != request.source_artifact_digest
    ):
        raise ValueError("source artifact mismatch")
    files = source["files"]
    if not isinstance(files, list) or len(files) != len(request.files):
        raise ValueError("incomplete source inventory")
    hashes: dict[str, str] = {}
    total = 0
    for name, value in zip(request.files, files, strict=True):
        item = fields(value, {"path", "sha256", "blob_sha", "mode", "size"})
        if (
            item["path"] != name
            or item["mode"] not in {"100644", "100755"}
            or not isinstance(item["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
            or not isinstance(item["blob_sha"], str)
            or not re.fullmatch(r"[0-9a-f]{40}", item["blob_sha"])
            or type(item["size"]) is not int
            or not 0 <= item["size"] <= 65536
        ):
            raise ValueError("invalid source inventory")
        total += item["size"]
        hashes[name] = item["sha256"]
    if total > 262144:
        raise ValueError("inventory exceeds budget")
    findings = doc["findings"]
    if not isinstance(findings, list) or len(findings) > 400:
        raise ValueError("findings exceed budget")
    known = {r.id: r.kind for r in INDICATORS}
    seen = set()
    for f in findings:
        row = fields(f, {"indicator", "kind", "confidence", "path", "sha256", "line"})
        if (
            not isinstance(row["indicator"], str)
            or row["indicator"] not in known
            or row["kind"] != known[row["indicator"]]
            or row["confidence"] != "static-indicator"
            or not isinstance(row["path"], str)
            or row["path"] not in hashes
            or row["sha256"] != hashes[row["path"]]
            or type(row["line"]) is not int
            or not 1 <= row["line"] <= 65536
        ):
            raise ValueError("invalid static finding")
        key = (row["indicator"], row["path"])
        if key in seen:
            raise ValueError("duplicate static finding")
        seen.add(key)
    interactions = sorted({f["indicator"] for f in findings if f["kind"] == "interaction"})
    expected = {
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
    }
    if doc["graph"] != expected:
        raise ValueError("graph differs from source findings")


def assessment_document(
    request: AnalysisRequest, intake: Evidence, manifest: dict[str, Any]
) -> dict[str, Any]:
    validate_manifest(manifest, request)
    return {
        "schema_version": 2,
        "kind": "service-assessment",
        "request_sha256": request.fingerprint,
        "scope": asdict(request.scope),
        "repository": request.repository,
        "source_artifact_digest": request.source_artifact_digest,
        "context_version": request.context_version,
        "policy_sha256": POLICY_SHA256,
        "intake": asdict(intake),
        "assessment": assess(manifest),
    }


def validate_origin(origin: DiscoveryOrigin, api: EvidenceClient, now: datetime) -> dict[str, Any]:
    request = origin.request
    manifest = read_reference(api, origin.intake, request, "intake", now)
    assessment = read_reference(api, origin.assessment, request, "assessment", now)
    expected = assessment_document(request, origin.intake, manifest)
    if canonical(assessment) != canonical(expected):
        raise ValueError("assessment does not match authenticated source evidence")
    return dict(expected["assessment"])  # deterministic scoring; never trust a supplied score


def draft(origin: DiscoveryOrigin, assessment: dict[str, Any]) -> dict[str, Any]:
    request = origin.request
    nodes: list[dict[str, Any]] = []

    def node(
        name: str,
        summary: str,
        dependencies: list[str],
        current: str,
        objective: str,
        acceptance: list[str],
    ) -> None:
        nodes.append(
            {
                "id": name,
                "summary": summary,
                "depends_on": dependencies,
                "requirements": {
                    "workstream": request.workstream,
                    "current_state": current,
                    "desired_state": "Reviewed instrumentation with measured evidence",
                    "objective": objective,
                    "implementation_scope": list(request.files),
                    "acceptance_criteria": acceptance,
                    "qa_requirements": [
                        "Run regression tests, including negative and missing-context cases"
                    ],
                    "telemetry_requirements": [
                        "Bind evidence to exact artifact, plan, preview ID and generation"
                    ],
                    "delivery_requirements": [
                        "Draft PR and CI evidence only; no merge or deployment authorization"
                    ],
                    "rollback_strategy": request.rollback_strategy,
                    "evidence": [asdict(origin.intake), asdict(origin.assessment)],
                },
            }
        )

    node(
        "architecture-review",
        "Review discovered architecture and coverage",
        [],
        "Static indicators from selected source; runtime topology and environment are unverified",
        "Confirm architecture, interaction graph and omitted coverage before changes",
        [
            "Validate each inferred interaction against authoritative service/runtime evidence",
            "Record unknowns and approved OTel strategy; never infer runtime absence",
        ],
    )
    for control in assessment["controls"]:
        name = control["id"]
        strategy = (
            "Prefer standard OpenTelemetry Java agent or Spring integration; "
            "review versions and avoid duplicate instrumentation"
            if "spring" in assessment["frameworks"]
            else "Prefer supported OpenTelemetry framework/SDK integration; "
            "review runtime and versions before changes"
        )
        node(
            name,
            f"{control['action'].capitalize()} {name}",
            ["architecture-review"],
            f"{name}: {control['status']} in the selected source; runtime behavior unknown",
            strategy,
            [
                f"Validate {name} with positive and negative runtime evidence",
                "Preserve application semantics and exclude secrets and unbounded metric labels",
            ],
        )
    for interaction in assessment["interactions"]:
        node(
            "flow-" + interaction,
            f"Validate {interaction} instrumentation",
            ["traces", "logs", "metrics", "propagation", "tests"],
            "Heuristic source interaction; destination and runtime behavior unverified",
            f"Confirm {interaction} propagation while preserving processing semantics",
            [
                "Exercise success, error and missing-context paths with correlated telemetry",
                "For asynchronous flows verify acknowledgements, retries and dead-letter handling",
            ],
        )
    deps = [n["id"] for n in nodes if n["id"].startswith("flow-")] or [
        c["id"] for c in assessment["controls"]
    ]
    node(
        "certification",
        "Verify the complete service telemetry catalog",
        deps,
        "No runtime certification established by static analysis",
        "Run independent 120-slot certification for the exact candidate and fixture window",
        ["All applicable mandatory checks pass; capability exclusions require independent proof"],
    )
    result = {
        "schema_version": 2,
        "id": request.execution_id,
        "project": request.project,
        "repository": request.repository,
        "scope": asdict(request.scope),
        "nodes": nodes,
        "discovery": origin.wire(),
    }
    return dict(json.loads(canonical(result)))

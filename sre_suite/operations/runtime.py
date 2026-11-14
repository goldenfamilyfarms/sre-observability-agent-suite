"""Authenticated analyses retain exact provenance; reports cannot substitute for certification."""

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..api_wire import envelope, evidence_from_wire, fields, text
from ..contracts import Scope
from ..control.contracts import sha, timestamp, wire
from ..execution.evidence_client import EvidenceClient
from ..execution.journal import Journal
from ..execution.wire import canonical, digest
from ..serialization import scope_from_dict
from ..telemetry.contracts import CandidateBinding, QueryPolicy, QueryRequest, epoch_ns
from ..verification.predicates import evaluate, matches, selection, validate
from ..verification.records import records


class AnalysisAgent:
    def __init__(self, evidence: EvidenceClient, journal: Journal) -> None:
        self.evidence, self.journal = evidence, journal

    def run(self, value: object) -> dict[str, Any]:
        raw = wire(
            envelope(
                value,
                {
                    "role",
                    "scope",
                    "context_version",
                    "artifact_digest",
                    "plan_digest",
                    "producer",
                    "inputs",
                    "parameters",
                    "expires_at",
                },
            )
        )
        scope = scope_from_dict(raw["scope"])
        artifact, plan = sha(raw["artifact_digest"]), sha(raw["plan_digest"])
        producer = text(raw["producer"], 128)
        expires = timestamp(raw["expires_at"])
        from ..telemetry.contracts import integer

        integer(raw["context_version"], 0, 2**63 - 1)

        def guard() -> None:
            if not datetime.now(UTC) < expires <= datetime.now(UTC) + timedelta(hours=24):
                raise ValueError("analysis deadline expired")
            self.evidence.context(scope, raw["context_version"])

        guard()
        inputs = raw["inputs"]
        if not isinstance(inputs, dict) or len(inputs) > 40:
            raise ValueError("bounded explicit inputs required")
        docs = {}
        refs = {}
        for name, item in inputs.items():
            row = fields(item, {"evidence", "producer"})
            ref = evidence_from_wire(row["evidence"])
            if ref.scope != scope or ref.producer != row["producer"] or ref.producer == producer:
                raise ValueError("independent scoped evidence required")
            docs[name] = wire(json.loads(self.evidence.read(ref)))
            refs[name] = ref
        role, parameters = raw["role"], wire(raw["parameters"])
        if role == "standards":
            from ..discovery.source import snapshot
            from .standards import POLICY, inspect

            fields(
                parameters,
                {"repository_dir", "repository", "files", "policy_sha256"}
                | ({"candidate_revision"} if docs else set()),
            )
            if parameters["policy_sha256"] != POLICY or set(docs) not in (set(), {"build"}):
                raise ValueError("standards source policy differs")
            revision = scope.revision
            if docs:
                build = docs["build"]
                if (
                    build.get("schema_version") != 2
                    or build.get("kind") != "image-build"
                    or build.get("image_digest") != artifact
                    or build.get("plan_digest") != plan
                    or build.get("source_revision") != scope.revision
                    or build.get("source_repository") != parameters["repository"]
                    or build.get("candidate_revision") != parameters["candidate_revision"]
                ):
                    raise ValueError("source assessment build provenance differs")
                revision = text(parameters["candidate_revision"], 40)
            inventory, contents = snapshot(
                Path(parameters["repository_dir"]),
                parameters["repository"],
                revision,
                tuple(parameters["files"]),
                guard,
            )
            if not docs and inventory["artifact_digest"] != artifact:
                raise ValueError("standards source artifact differs")
            result = inspect(contents, inventory)
            result["source_snapshot"] = inventory
            result["passed"] = not result["parse_errors"] and all(
                c["status"] == "observed_static" for c in result["controls"].values()
            )
        elif role in {"topology", "pipeline-health"}:
            fields(parameters, {"policy", "rules"})
            policy = QueryPolicy.parse(parameters["policy"])
            if policy.scope != scope:
                raise ValueError("telemetry policy scope differs")
            data = {}
            requests = {}
            for name, doc in docs.items():
                request = QueryRequest.parse(doc["request"])
                profile = policy.resolve(request)
                if (
                    request.binding != CandidateBinding(artifact, plan)
                    or not profile.binding_labels
                ):
                    raise ValueError("telemetry artifact or plan differs")
                from ..execution.telemetry import validate_observed
                from ..telemetry.contracts import profile_document

                if (
                    doc.get("kind") != "telemetry-observation"
                    or doc.get("schema_version") != 2
                    or doc.get("profile") != profile_document(profile)
                    or doc.get("rendered_query") != profile.query(scope, request.binding)
                    or doc.get("context_version") != raw["context_version"]
                    or doc.get("observed_at") != refs[name].observed_at.isoformat()
                ):
                    raise ValueError("observation provenance differs")
                if doc["result"].get("status") != "observed" or doc["result"].get("issues") != []:
                    raise ValueError("partial or empty telemetry is not a health pass")
                validate_observed(doc["result"], profile, request)
                data[name], requests[name] = records(doc["result"], request), request
            if role == "topology":
                result = topology(data, parameters["rules"])
            else:
                rules = parameters["rules"]
                if not isinstance(rules, list) or not 1 <= len(rules) <= 40:
                    raise ValueError("explicit health rules required")
                outcomes = []
                for item in rules:
                    rule = fields(
                        item, {"id", "source", "selector", "operator", "parameters", "expected"}
                    )
                    source = text(rule["source"])
                    selector = selection(rule["selector"])
                    operator = text(rule["operator"])
                    args = validate(operator, rule["parameters"])
                    rows = [r for r in data[source] if r["_in_scope"] and matches(r, selector)]
                    request = requests[source]
                    expected = rule["expected"]
                    if not isinstance(expected, list) or len(expected) > 500:
                        raise ValueError("bounded oracle required")
                    ok = evaluate(
                        operator,
                        args,
                        rows,
                        expected,
                        epoch_ns(request.start),
                        epoch_ns(request.end),
                        data[source],
                    )
                    outcomes.append(
                        {
                            "id": text(rule["id"], 80),
                            "outcome": "PASS" if ok else "FAIL",
                            "records": len(rows),
                        }
                    )
                if len({o["id"] for o in outcomes}) != len(outcomes):
                    raise ValueError("duplicate health rule")
                result = {
                    "passed": all(o["outcome"] == "PASS" for o in outcomes),
                    "checks": outcomes,
                }
        elif role == "assessment":
            fields(parameters, {"required_controls"})
            required = parameters["required_controls"]
            if (
                not isinstance(required, list)
                or not required
                or len(set(required)) != len(required)
            ):
                raise ValueError("explicit assessment controls required")
            if set(docs) != {"standards", "topology", "pipeline-health"}:
                raise ValueError("independent source and runtime assessments required")
            for name, doc in docs.items():
                if (
                    doc.get("kind") != name + "-analysis"
                    or scope_from_dict(doc.get("scope")) != scope
                    or doc.get("artifact_digest") != artifact
                    or doc.get("plan_digest") != plan
                ):
                    raise ValueError("assessment input binding differs")
            controls = docs["standards"]["result"]["controls"]
            if not set(required) <= controls.keys():
                raise ValueError("unknown assessment control")
            static_gaps = [
                name for name in required if controls[name]["status"] != "observed_static"
            ]
            runtime_gaps = [
                name
                for name in ("topology", "pipeline-health")
                if docs[name]["result"].get("passed") is not True
            ]
            # Separate static indicators from observed runtime evidence. This score is a planning
            # priority signal, not a service certificate or proof of universal framework coverage.
            score = (1 if not static_gaps else 0) + (1 if not runtime_gaps else 0)
            result = {
                "passed": not static_gaps and not runtime_gaps,
                "maturity_score": score,
                "maximum_score": 2,
                "static_gaps": static_gaps,
                "runtime_gaps": runtime_gaps,
                "certified": False,
            }
        elif role == "remediation":
            fields(parameters, {"template_plan"})
            if set(docs) != {"candidate"}:
                raise ValueError("one independently produced failed certification required")
            from .remediation import propose

            result = propose(
                docs["candidate"],
                refs["candidate"],
                parameters["template_plan"],
                scope,
                artifact,
                plan,
            )
        elif role == "regression":
            fields(parameters, {"baseline_artifact_digest", "baseline_plan_digest"})
            if set(docs) != {"baseline", "candidate"}:
                raise ValueError("two authenticated verifier reports required")
            result = regression(docs, scope, artifact, plan, parameters)
        else:
            raise ValueError("unknown analysis role")
        guard()
        document = {
            "schema_version": 2,
            "kind": role + "-analysis",
            "scope": asdict(scope),
            "artifact_digest": artifact,
            "plan_digest": plan,
            "request_sha256": digest(raw),
            "inputs": inputs,
            "result": result,
            "certification_verdict": None,
        }
        key = "analysis-" + digest(raw)
        saved = self.journal.compute(
            key,
            raw,
            lambda: {"document": wire(document), "observed_at": datetime.now(UTC).isoformat()},
            guard,
        )
        if saved["document"] != wire(document):
            raise ValueError("analysis changed on retry")
        ref = self.evidence.upload(
            key, scope, canonical(document).encode(), timestamp(saved["observed_at"]), expires
        )
        if ref.producer != producer:
            raise ValueError("analysis worker identity differs")
        guard()
        return {"passed": result["passed"], "evidence": wire(asdict(ref)), "result": result}


def topology(data: dict[str, list[dict[str, Any]]], expected: object) -> dict[str, Any]:
    if not isinstance(expected, list) or not 1 <= len(expected) <= 100:
        raise ValueError("reviewed expected service edges required")
    wanted = set()
    for item in expected:
        edge = fields(item, {"source", "target"})
        wanted.add((text(edge["source"], 128), text(edge["target"], 128)))
    spans: dict[tuple[str, str], dict[str, Any]] = {}
    for rows in data.values():
        for row in rows:
            if "span_id" not in row:
                raise ValueError("full trace evidence required")
            key = (row["trace_id"], row["span_id"])
            if key in spans and spans[key] != row:
                raise ValueError("conflicting trace span")
            spans[key] = row
    edges: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in spans.values():
        parent = spans.get((row["trace_id"], row["parent_span_id"]))
        if parent is None or not (row["_in_scope"] or parent["_in_scope"]):
            continue
        source, target = parent["resource"].get("service.name"), row["resource"].get("service.name")
        if not isinstance(source, str) or not isinstance(target, str) or source == target:
            continue
        edges.setdefault((source, target), []).append(
            {
                "trace_id": row["trace_id"],
                "parent_span_id": parent["span_id"],
                "span_id": row["span_id"],
            }
        )
    return {
        "passed": wanted <= edges.keys(),
        "edges": [
            {"source": a, "target": b, "citations": v} for (a, b), v in sorted(edges.items())
        ],
        "missing": [{"source": a, "target": b} for a, b in sorted(wanted - edges.keys())],
        "absence_proves_no_dependency": False,
    }


def regression(
    docs: dict[str, dict[str, Any]],
    scope: Scope,
    artifact: str,
    plan: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    from ..certification import load_catalog
    from ..verification.contracts import catalog_digest

    catalog = load_catalog()
    expected_ids = {c.id for c in catalog.checks}
    results = {}
    for name, doc in docs.items():
        expected_artifact = (
            artifact if name == "candidate" else sha(parameters["baseline_artifact_digest"])
        )
        expected_plan = plan if name == "candidate" else sha(parameters["baseline_plan_digest"])
        release = doc.get("release", {})
        if (
            doc.get("kind") != "telemetry-certification"
            or doc.get("catalog_sha256") != catalog_digest(catalog)
            or doc.get("artifact_kind") not in {"source-candidate-v1", "container-image-v1"}
        ):
            raise ValueError("invalid certificate report")
        if (
            scope_from_dict(release.get("scope")) != scope
            or release.get("artifact_digest") != expected_artifact
            or release.get("plan_digest") != expected_plan
            or doc.get("schema_version") != 2
        ):
            raise ValueError("regression report release differs")
        rows = doc.get("results")
        if (
            not isinstance(rows, list)
            or len(rows) != 120
            or {r.get("check_id") for r in rows} != expected_ids
        ):
            raise ValueError("complete 120-check report required")
        outcomes = {r["check_id"]: r["outcome"] for r in rows}
        if any(
            o not in {"PASS", "FAIL", "ERROR", "INCONCLUSIVE", "NOT_APPLICABLE"}
            for o in outcomes.values()
        ):
            raise ValueError("unknown certification outcome")
        results[name] = outcomes
    if docs["baseline"].get("policy_sha256") != docs["candidate"].get("policy_sha256") or docs[
        "baseline"
    ].get("artifact_kind") != docs["candidate"].get("artifact_kind"):
        raise ValueError("regression catalogs differ")
    regressions = [
        k
        for k in sorted(expected_ids)
        if results["baseline"][k] == "PASS" and results["candidate"][k] != "PASS"
    ]
    unresolved = [
        k for k in sorted(expected_ids) if results["candidate"][k] not in {"PASS", "NOT_APPLICABLE"}
    ]
    return {
        "passed": not regressions and not unresolved,
        "regressions": regressions,
        "unresolved": unresolved,
        "remediation_required": bool(regressions or unresolved),
    }

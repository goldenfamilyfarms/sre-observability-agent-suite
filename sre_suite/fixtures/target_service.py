"""Independent build/readiness evidence fences for deployed target-service journeys."""

import json
from datetime import datetime, timedelta
from typing import Any

from ..api_wire import evidence_from_wire, fields, text
from ..control.contracts import Binding, wire
from ..execution.evidence_client import EvidenceClient
from ..serialization import scope_from_dict
from ..telemetry.contracts import integer


def validate(
    value: object, runner: str, assessor: str, collector: str, verifier: str
) -> dict[str, Any]:
    result = wire(
        fields(value, {"build", "readiness", "builder", "delivery", "maximum_age_seconds"})
    )
    builder, delivery = text(result["builder"], 128), text(result["delivery"], 128)
    if len({builder, delivery, runner, assessor, collector, verifier}) != 6:
        raise ValueError("independent build, delivery and fixture principals required")
    for name, producer in (("build", builder), ("readiness", delivery)):
        if evidence_from_wire(result[name]).producer != producer:
            raise ValueError("target proof producer differs")
    integer(result["maximum_age_seconds"], 1, 300)
    return result


def verify(
    config: dict[str, Any], binding: dict[str, Any], evidence: EvidenceClient, now: datetime
) -> dict[str, str]:
    scope = scope_from_dict(binding["release"]["scope"])
    if binding["artifact_kind"] != "container-image-v1":
        raise ValueError("deployed fixtures require image certification")
    refs = {name: evidence_from_wire(config[name]) for name in ("build", "readiness")}
    for ref in refs.values():
        if not ref.valid_for(scope, now):
            raise ValueError("target evidence expired or scope differs")
    ready_ref = refs["readiness"]
    if now - ready_ref.observed_at > timedelta(seconds=config["maximum_age_seconds"]):
        raise ValueError("refresh independent deployment readiness before running fixtures")
    build, readiness = (json.loads(evidence.read(refs[n])) for n in ("build", "readiness"))
    release = binding["release"]
    ready = Binding.parse(readiness["binding"])
    if (
        build.get("schema_version") != 2
        or build.get("kind") != "image-build"
        or build.get("image_digest") != release["artifact_digest"]
        or build.get("plan_digest") != release["plan_digest"]
        or build.get("source_revision") != scope.revision
        or readiness.get("kind") != "deployment-readiness"
        or ready.scope != scope
        or ready.context_version != binding["context_version"]
        or ready.artifact_digest != release["artifact_digest"]
        or ready.plan_digest != release["plan_digest"]
        or ready.target.get("candidate_revision") != build.get("candidate_revision")
        or ready.target.get("source_repository") != build.get("source_repository")
        or ready.target.get("image", "").split("@")[-1] != release["artifact_digest"]
        or scope_from_dict(readiness["target_scope"]) != scope
        or readiness.get("observation", {}).get("ready") is not True
        or readiness.get("observed_at") != ready_ref.observed_at.isoformat()
    ):
        raise ValueError("target proof does not bind the exact deployed candidate and plan")
    return {
        "service.name": scope.service,
        "deployment.environment.name": str(scope.environment),
        "service.version": build["candidate_revision"],
        "sre.source.revision": scope.revision,
        "sre.environment.id": scope.environment_id,
        "sre.environment.generation": str(scope.generation),
        "sre.artifact.digest": release["artifact_digest"],
        "sre.plan.digest": release["plan_digest"],
    }

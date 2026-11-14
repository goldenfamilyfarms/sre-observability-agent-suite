"""Delivery/readiness agents with independent provenance and live authorization."""

import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from ..api_wire import fields
from ..control.client import ControlClient
from ..execution.evidence_client import EvidenceClient
from ..execution.journal import Journal
from ..execution.wire import canonical, digest
from ..verification.runtime import CertificationEngine
from .contracts import DeliveryRequest
from .helm import CHART_DIGEST, HelmRenderer, application, resources
from .providers import GkeInventory, KubernetesArgo


class DeliveryRuntime:
    def __init__(
        self,
        request: DeliveryRequest,
        control: ControlClient,
        evidence: EvidenceClient,
        journal: Journal,
        gke: GkeInventory,
        kubernetes: KubernetesArgo,
        helm: HelmRenderer,
        builder: str,
        roles: dict[str, str],
        verifier: CertificationEngine | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        heartbeat: Callable[[], None] = lambda: None,
    ) -> None:
        if (
            set(roles) != {"implement", "review", "test", "qa"}
            or len(set(roles.values()) | {builder}) != 5
        ):
            raise ValueError("independent build and engineering subjects required")
        self.request, self.control, self.evidence = request, control, evidence
        self.journal, self.gke, self.kubernetes, self.helm = journal, gke, kubernetes, helm
        self.builder, self.roles, self.verifier, self.clock, self.heartbeat = (
            builder,
            roles,
            verifier,
            clock,
            heartbeat,
        )
        self.kubernetes.guard = self.guard

    def guard(self) -> None:
        self.heartbeat()
        request, now = self.request, self.clock()
        if not now < request.expires_at <= now + timedelta(hours=24):
            raise ValueError("delivery authorization expired or exceeds budget")
        self.evidence.context(request.binding.scope, request.binding.context_version)
        approval = self.control.require_approval(request.binding)
        if approval.get("approver") in {*self.roles.values(), self.builder}:
            raise ValueError(
                "release approver must be independent of engineering and build workers"
            )
        if request.binding.action != "cleanup":
            self.evidence.context(request.target.scope, request.target.raw["context_version"])
            self.provenance()

    def provenance(self) -> None:
        request = self.request
        if request.build is None or request.build.producer != self.builder:
            raise ValueError("build evidence producer differs")
        build = fields(
            json.loads(self.evidence.read(request.build)),
            {
                "schema_version",
                "kind",
                "source_repository",
                "source_revision",
                "candidate_revision",
                "source_artifact_digest",
                "plan_digest",
                "image_digest",
                "stage_artifacts",
                "chart_digest",
                "chart_revision",
            },
        )
        if (
            build["schema_version"] != 2
            or build["kind"] != "image-build"
            or build["source_repository"] != request.target.raw["source_repository"]
            or build["source_revision"] != request.binding.scope.revision
            or build["candidate_revision"] != request.target.raw["candidate_revision"]
            or build["image_digest"] != request.binding.artifact_digest
            or build["plan_digest"] != request.binding.plan_digest
            or build["chart_revision"] != request.target.raw["git_revision"]
            or build["chart_digest"] != CHART_DIGEST
            or request.target.raw["chart_digest"] != CHART_DIGEST
        ):
            raise ValueError("build provenance differs from reviewed release")
        if build["stage_artifacts"] != {r: e.sha256 for r, e in request.stages.items()}:
            raise ValueError("build does not bind all engineering gates")
        if build["source_artifact_digest"] != "sha256:" + request.stages["implement"].sha256:
            raise ValueError("build source artifact differs")
        manifest = None
        predecessors: list[str] = []
        for role in ("implement", "review", "test", "qa"):
            ref = request.stages[role]
            if ref.producer != self.roles[role] or not ref.valid_for(
                request.binding.scope, self.clock()
            ):
                raise ValueError("engineering attestation identity/freshness differs")
            stage = json.loads(self.evidence.read(ref))
            if stage.get("prior_evidence_sha256") != predecessors:
                raise ValueError("engineering attestations do not form one artifact chain")
            predecessors.append(ref.sha256)
            if stage.get("role") != role or stage.get("verdict") != "passed":
                raise ValueError("engineering gate did not pass")
            if manifest is None:
                manifest = stage.get("manifest")
            if not manifest or stage.get("manifest") != manifest:
                raise ValueError("engineering gate manifest differs")
        if manifest != request.binding.plan_digest.removeprefix("sha256:"):
            raise ValueError("engineering manifest differs from release plan")
        if request.verification is not None:
            if request.verification["input"].get("artifact_kind") != "container-image-v1":
                raise ValueError("image delivery requires deployed image certification")
            if self.verifier is None:
                raise ValueError("configured independent certification engine required")
            gate = self.verifier.gate(
                request.verification["input"], request.verification["receipt"]
            )
            if (
                not gate.passed
                or gate.release.scope != request.binding.scope
                or gate.release.artifact_digest != request.binding.artifact_digest
                or gate.release.plan_digest != request.binding.plan_digest
            ):
                raise ValueError("certificate is not for this exact image and plan")

    def prepare(self) -> dict[str, Any]:
        self.guard()
        inventory = self.gke.verify(self.request.target, self.kubernetes.http.base)
        rendered = self.helm.render(self.request, self.guard)
        self.guard()
        return {
            "inventory": inventory,
            "rendered": rendered,
            "application_sha256": digest(application(self.request)),
            "writes": False,
        }

    def deploy(self) -> dict[str, Any]:
        if self.request.binding.action not in {"promote", "rollback"}:
            raise ValueError("deployment action required")
        prepared = self.prepare()
        self.kubernetes.namespace(self.request.target)
        app = self.kubernetes.apply(self.request.target, application(self.request))
        self.kubernetes.sync(self.request.target, app)
        self.guard()
        return {**prepared, "outcome": "sync_requested", "accepted": False}

    def readiness(self) -> dict[str, Any]:
        self.guard()
        self.gke.verify(self.request.target, self.kubernetes.http.base)
        target = self.request.target
        namespace = self.kubernetes.read("/api/v1/namespaces/" + target.raw["namespace"])
        app = self.kubernetes.read(self.kubernetes.application_path(target))
        if namespace is None or app is None:
            return {"ready": False, "reason": "target_missing"}
        self.kubernetes.own(target, namespace)
        if app.get("spec") != application(self.request)["spec"]:
            raise ValueError("application changed after authorization")
        rendered = resources(self.request)
        status = self.kubernetes.readiness(target, rendered[0]["spec"]["template"], rendered)
        if not status["ready"]:
            return status
        # Saved observations never substitute for live deployment reads.
        # A live observation must be refreshable even when resource versions did not change.
        key = (
            self.request.identity
            + "-readiness-"
            + digest([status, int(self.clock().timestamp()) // 15])
        )
        value = self.journal.compute(
            key,
            self.request.binding.wire(),
            lambda: {
                "kind": "deployment-readiness",
                "binding": self.request.binding.wire(),
                "target_scope": asdict(self.request.target.scope),
                "observation": status,
                "observed_at": self.clock().isoformat(),
            },
            self.guard,
        )
        ref = self.evidence.upload(
            digest(key),
            self.request.binding.scope,
            canonical(value).encode(),
            datetime.fromisoformat(value["observed_at"]),
            self.request.expires_at,
        )
        self.guard()
        return {
            "ready": True,
            "evidence": json.loads(canonical(asdict(ref))),
            "outcome": "ready_for_verification"
            if self.request.target.raw["phase"] == "preview"
            else "release_ready",
            "accepted": self.request.verification is not None,
        }

    def cleanup(self) -> dict[str, Any]:
        if self.request.binding.action != "cleanup":
            raise ValueError("separate cleanup authorization required")
        self.guard()
        self.gke.verify(self.request.target, self.kubernetes.http.base)
        result = self.kubernetes.cleanup(self.request.target)
        self.guard()
        return result

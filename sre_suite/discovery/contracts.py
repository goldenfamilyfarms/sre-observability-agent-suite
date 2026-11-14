"""Operator-pinned analysis scope; a baseline source snapshot is not a deployed artifact."""

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from ..api_wire import envelope, text
from ..contracts import Environment, Evidence, Scope, aware
from ..execution.config import safe_path
from ..execution.wire import canonical, digest
from ..fixtures.contracts import queue
from ..serialization import scope_from_dict
from ..telemetry.contracts import integer


def paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise ValueError("select 1..100 explicit source files")
    result = tuple(safe_path(text(p, 240)) for p in value)
    if len(set(result)) != len(result) or list(result) != sorted(result):
        raise ValueError("source files must be unique and sorted")
    forbidden = {"credentials", "secrets", "id_rsa", "id_ed25519"}
    if any(
        any(part.lower().split(".")[0] in forbidden for part in p.split("/"))
        or p.lower().endswith((".pem", ".key", ".p12", ".pfx"))
        for p in result
    ):
        raise ValueError("credential files cannot be discovery inputs")
    return result


@dataclass(frozen=True)
class AnalysisRequest:
    execution_id: str
    repository: str
    scope: Scope
    context_version: int
    files: tuple[str, ...]
    source_artifact_digest: str
    intake_subject: str
    assessor_subject: str
    project: str
    workstream: str
    rollback_strategy: str
    expires_at: datetime
    intake_queue: str
    assessment_queue: str

    @classmethod
    def parse(cls, value: object) -> "AnalysisRequest":
        raw = envelope(
            value,
            {
                "execution_id",
                "repository",
                "scope",
                "context_version",
                "files",
                "source_artifact_digest",
                "intake_subject",
                "assessor_subject",
                "project",
                "workstream",
                "rollback_strategy",
                "expires_at",
                "intake_queue",
                "assessment_queue",
            },
        )
        identity, repo, project = (
            text(raw[n], 128) for n in ("execution_id", "repository", "project")
        )
        scope = scope_from_dict(raw["scope"])
        if (
            not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", identity)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
            or not re.fullmatch(r"[A-Z][A-Z0-9]{1,30}", project)
            or scope.environment == Environment.PRODUCTION
            or not re.fullmatch(r"preview-r[1-9][0-9]*-pr[1-9][0-9]*", scope.environment_id)
        ):
            raise ValueError("analysis requires explicit repository and non-production preview")
        artifact = text(raw["source_artifact_digest"], 71)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact):
            raise ValueError("pin the selected Git snapshot artifact before analysis")
        intake, assessor = (text(raw[n], 128) for n in ("intake_subject", "assessor_subject"))
        iq, aq = (queue(raw[n]) for n in ("intake_queue", "assessment_queue"))
        if intake == assessor or iq == aq:
            raise ValueError("intake and assessment require separate identities and queues")
        expiry = datetime.fromisoformat(text(raw["expires_at"], 64))
        aware(expiry)
        return cls(
            identity,
            repo,
            scope,
            integer(raw["context_version"], 0, 2**63 - 1),
            paths(raw["files"]),
            artifact,
            intake,
            assessor,
            project,
            text(raw["workstream"], 128),
            text(raw["rollback_strategy"], 1000),
            expiry,
            iq,
            aq,
        )

    def wire(self) -> dict[str, Any]:
        import json

        return dict(json.loads(canonical({"schema_version": 2, **asdict(self)})))

    @property
    def fingerprint(self) -> str:
        return digest(self.wire())

    @property
    def workflow_id(self) -> str:
        return "sre-discovery-" + self.fingerprint


@dataclass(frozen=True)
class DiscoveryOrigin:
    request: AnalysisRequest
    intake: Evidence
    assessment: Evidence
    policy_sha256: str

    @classmethod
    def parse(cls, value: object) -> "DiscoveryOrigin":
        from ..api_wire import evidence_from_wire, fields
        from .analysis import POLICY_SHA256

        raw = fields(value, {"request", "intake", "assessment", "policy_sha256"})
        request = AnalysisRequest.parse(raw["request"])
        intake, assessment = (evidence_from_wire(raw[n]) for n in ("intake", "assessment"))
        if (
            raw["policy_sha256"] != POLICY_SHA256
            or intake.scope != request.scope
            or assessment.scope != request.scope
            or intake.producer != request.intake_subject
            or assessment.producer != request.assessor_subject
        ):
            raise ValueError("discovery origin identity mismatch")
        return cls(request, intake, assessment, POLICY_SHA256)

    def wire(self) -> dict[str, Any]:
        import json

        return dict(
            json.loads(
                canonical(
                    {
                        "request": self.request.wire(),
                        "intake": asdict(self.intake),
                        "assessment": asdict(self.assessment),
                        "policy_sha256": self.policy_sha256,
                    }
                )
            )
        )

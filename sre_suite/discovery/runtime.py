"""Evidence-producing intake and assessment with immutable recovery and current-scope fences."""

import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..api_wire import evidence_from_wire
from ..contracts import Evidence
from ..execution.evidence_client import EvidenceClient
from ..execution.journal import Journal
from ..execution.wire import canonical
from .analysis import POLICY_SHA256, discover
from .contracts import AnalysisRequest, DiscoveryOrigin
from .provenance import (
    assessment_document,
    draft,
    read_reference,
    validate_manifest,
    validate_origin,
)
from .source import snapshot


class DiscoveryRuntime:
    def __init__(
        self,
        request: AnalysisRequest,
        evidence: EvidenceClient,
        journal: Journal,
        role: str,
        repository_dir: Path | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        heartbeat: Callable[[], None] = lambda: None,
    ) -> None:
        if role not in {"intake", "assessment"} or (role == "intake") != (
            repository_dir is not None
        ):
            raise ValueError("intake alone requires repository access")
        from ..telemetry.schedule_config import private_path

        private_path(journal.root, directory=True)
        self.request, self.evidence, self.journal = request, evidence, journal
        self.role, self.repository_dir, self.clock, self.heartbeat = (
            role,
            repository_dir,
            clock,
            heartbeat,
        )

    def guard(self) -> None:
        self.heartbeat()
        now = self.clock()
        if not now < self.request.expires_at <= now + timedelta(hours=24):
            raise ValueError("analysis authorization expired or exceeds one day")
        self.evidence.context(self.request.scope, self.request.context_version)

    def publish(
        self, role: str, inputs: object, build: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
        self.guard()
        key = role + "-" + self.request.fingerprint

        def operation() -> dict[str, Any]:
            self.guard()
            document = build()
            self.guard()
            return {"document": document, "observed_at": self.clock().isoformat()}

        result = self.journal.compute(
            key, {"inputs": inputs, "policy": POLICY_SHA256}, operation, self.guard
        )
        document = result["document"]
        if role == "intake":
            validate_manifest(document, self.request)
        elif canonical(document) != canonical(build()):
            raise ValueError("cached assessment differs from current authenticated evidence")
        self.guard()
        ref = self.evidence.upload(
            key,
            self.request.scope,
            canonical(document).encode(),
            datetime.fromisoformat(result["observed_at"]),
            self.request.expires_at,
        )
        self.guard()
        actual = read_reference(self.evidence, ref, self.request, role, self.clock())
        if canonical(actual) != canonical(document):
            raise ValueError("published analysis differs")
        return dict(json.loads(canonical(asdict(ref))))

    def intake(self) -> dict[str, Any]:
        if self.role != "intake" or self.repository_dir is None:
            raise ValueError("intake role required")
        self.guard()

        def build() -> dict[str, Any]:
            assert self.repository_dir is not None
            inventory, contents = snapshot(
                self.repository_dir,
                self.request.repository,
                self.request.scope.revision,
                self.request.files,
                self.guard,
            )
            if inventory["artifact_digest"] != self.request.source_artifact_digest:
                raise ValueError("source artifact differs from authorization")
            return discover(self.request, inventory, contents)

        return self.publish("intake", self.request.wire(), build)

    def assess(self, intake: Evidence) -> dict[str, Any]:
        if self.role != "assessment":
            raise ValueError("assessment role required")
        self.guard()
        manifest = read_reference(self.evidence, intake, self.request, "intake", self.clock())
        expected = assessment_document(self.request, intake, manifest)
        ref = self.publish(
            "assessment",
            {"request": self.request.wire(), "intake": asdict(intake)},
            lambda: expected,
        )
        origin = DiscoveryOrigin(self.request, intake, evidence_from_wire(ref), POLICY_SHA256)
        validate_origin(origin, self.evidence, self.clock())
        self.guard()
        return ref

    def plan(self, intake: Evidence, assessment: Evidence) -> dict[str, Any]:
        if self.role != "assessment":
            raise ValueError("assessment role required")
        from ..jira_planning.contracts import Plan

        self.guard()
        origin = DiscoveryOrigin.parse(
            DiscoveryOrigin(self.request, intake, assessment, POLICY_SHA256).wire()
        )
        report = validate_origin(origin, self.evidence, self.clock())
        proposed = Plan.parse(draft(origin, report))
        self.guard()
        return {
            "plan": proposed.json(),
            "plan_sha256": proposed.fingerprint,
            "execution_started": False,
            "jira_published": False,
        }

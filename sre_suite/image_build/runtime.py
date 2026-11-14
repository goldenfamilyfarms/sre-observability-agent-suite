"""Fresh authority, reviewed source, durable OCI output and authenticated build evidence."""

import json
import os
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from ..control.client import ControlClient
from ..execution.evidence_client import EvidenceClient
from ..execution.journal import Journal
from ..execution.wire import canonical, digest
from . import source
from .contracts import BuildRequest
from .oci import inspect
from .providers import Tools, labels


class BuildRuntime:
    def __init__(
        self,
        request: BuildRequest,
        roles: dict[str, str],
        producer: str,
        repository: Path,
        evidence: EvidenceClient,
        control: ControlClient,
        journal: Journal,
        tools: Tools,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        policy_guard: Callable[[], None] = lambda: None,
    ) -> None:
        if set(roles) != set(request.stages) or len({producer, *roles.values()}) != 5:
            raise ValueError("builder must be independent of all four engineering subjects")
        self.request, self.roles, self.producer, self.repository = (
            request,
            roles,
            producer,
            repository,
        )
        self.evidence, self.control, self.journal, self.tools = evidence, control, journal, tools
        self.clock, self.policy_guard = clock, policy_guard

    def guard(self) -> None:
        self.policy_guard()
        r = self.request
        if not self.clock() < r.expires <= self.clock() + timedelta(hours=24):
            raise ValueError("build request expired or exceeds budget")
        self.evidence.context(r.binding.scope, r.binding.context_version)
        approval = self.control.require_approval(r.binding)
        if approval.get("approver") in {self.producer, *self.roles.values()}:
            raise ValueError("build approval requires an independent principal")

    def stages(self) -> dict[str, Any]:
        self.guard()
        previous: list[str] = []
        implementation: dict[str, Any] = {}
        for role in ("implement", "review", "test", "qa"):
            ref = self.request.stages[role]
            if ref.producer != self.roles[role] or not ref.valid_for(
                self.request.binding.scope, self.clock()
            ):
                raise ValueError("engineering evidence expired or producer differs")
            row = json.loads(self.evidence.read(ref))
            if (
                row.get("role") != role
                or row.get("verdict") != "passed"
                or row.get("manifest") != self.request.binding.plan_digest.removeprefix("sha256:")
                or row.get("prior_evidence_sha256") != previous
            ):
                raise ValueError("engineering evidence does not attest the exact candidate chain")
            previous.append(ref.sha256)
            if role == "implement":
                implementation = row
        return implementation

    @property
    def archive(self) -> Path:
        return self.journal.root / (self.request.identity + ".oci.tar")

    def build(self) -> dict[str, Any]:
        implementation = self.stages()
        r = self.request

        def operation() -> dict[str, Any]:
            with TemporaryDirectory(prefix="build-", dir=self.journal.root) as directory:
                root = Path(directory)
                context = root / "context"
                context.mkdir()
                hashes = source.export(self.repository, r, implementation, context, self.guard)
                archive = root / "image.tar"
                result = self.tools.build(r, context, hashes, archive, self.guard)
                self.guard()
                with archive.open("rb") as stream:
                    os.fsync(stream.fileno())
                os.replace(archive, self.archive)
                descriptor = os.open(self.journal.root, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                return {**result, "context": hashes, "observed_at": self.clock().isoformat()}

        result = self.journal.compute(
            r.identity + "-built", r.binding.wire(), operation, self.guard
        )
        self.validate_archive(result)
        self.stages()
        return {"outcome": "built", "build_id": r.identity, **result}

    def validate_archive(self, result: dict[str, Any]) -> None:
        recipe = self.request.binding.target["recipe"]
        actual = inspect(
            self.archive,
            recipe["platform"],
            recipe["entrypoint"],
            labels(self.request, result["context"]),
        )
        if any(actual[k] != result[k] for k in actual):
            raise ValueError("saved OCI archive was changed")

    def publish(self) -> dict[str, Any]:
        # Publishing cannot implicitly run arbitrary source/build steps.
        self.stages()
        key = self.request.identity + "-built"
        path = self.journal.root / (digest(key) + ".json")
        saved = json.loads(path.read_text())
        result = self.journal.read(key, saved["checksum"])
        self.validate_archive(result)
        r, image = self.request, result["image_digest"]

        def push() -> dict[str, Any]:
            self.journal.compute(
                r.identity + "-push-intent", {"image": image}, lambda: {"image": image}, self.guard
            )
            self.tools.publish(r, self.archive, image, self.guard)
            self.guard()
            return {"image": image}

        self.journal.compute(r.identity + "-pushed", {"image": image}, push, self.guard)
        self.tools.verify(r, image, self.guard)
        self.stages()
        target = r.binding.target
        document = {
            "schema_version": 2,
            "kind": "image-build",
            "source_repository": target["source_repository"],
            "source_revision": r.binding.scope.revision,
            "candidate_revision": target["candidate_revision"],
            "source_artifact_digest": r.binding.artifact_digest,
            "plan_digest": r.binding.plan_digest,
            "image_digest": image,
            "stage_artifacts": {role: e.sha256 for role, e in r.stages.items()},
            "chart_digest": target["chart_digest"],
            "chart_revision": target["chart_revision"],
        }
        evidence = self.evidence.upload(
            r.identity,
            r.binding.scope,
            canonical(document).encode(),
            datetime.fromisoformat(result["observed_at"]),
            r.expires,
        )
        if evidence.producer != self.producer:
            raise ValueError("wrong builder credential")
        self.stages()
        return {
            "outcome": "attested",
            "build_id": r.identity,
            "image": target["registry_repository"] + "@" + image,
            "build": json.loads(canonical(asdict(evidence))),
            "stages": json.loads(canonical({k: asdict(v) for k, v in r.stages.items()})),
        }

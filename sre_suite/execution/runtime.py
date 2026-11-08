"""Side-effect activities for the durable engine, with scope/context revalidation."""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..api_wire import envelope, evidence_from_wire, text
from ..contracts import Environment
from ..serialization import scope_from_dict
from .claude import ClaudeCodeWorker
from .config import Settings
from .evidence_client import EvidenceClient
from .github import GitHubClient
from .jira import JiraClient
from .journal import Journal
from .wire import canonical, digest, ticket_from_wire, ticket_json
from .workspace import ContainerTests, make_patch, selected_source

STAGES = ("implement", "review", "test", "qa")


def execution_key(spec: dict[str, Any]) -> str:
    raw = envelope(spec, {"ticket_key", "scope", "run_id"})
    if not re.fullmatch(r"[A-Z][A-Z0-9]*-[1-9][0-9]*", text(raw["ticket_key"], 80)):
        raise ValueError("invalid ticket key")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", text(raw["run_id"], 80)):
        raise ValueError("invalid execution identifier")
    if scope_from_dict(raw["scope"]).environment is Environment.PRODUCTION:
        raise ValueError("production execution is not supported")
    return "sre-" + digest(spec)


class ExecutionRuntime:
    def __init__(
        self,
        settings: Settings,
        *,
        jira: JiraClient | None = None,
        github: GitHubClient | None = None,
        evidence: EvidenceClient | None = None,
        worker: ClaudeCodeWorker | None = None,
        tests: ContainerTests | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings
        self.jira = jira or JiraClient(settings)
        self.github = github or GitHubClient(settings)
        self.evidence = evidence or EvidenceClient(
            settings.evidence_url, settings.evidence_token, local_ca_file=settings.evidence_ca_file
        )
        self.worker = worker or ClaudeCodeWorker(settings)
        self.tests = tests or ContainerTests(settings)
        self.journal = Journal(settings.work_root)
        self.clock = clock
        self.policy = digest(
            {
                **(
                    {
                        "evidence_ca_sha256": hashlib.sha256(
                            Path(settings.evidence_ca_file).read_bytes()
                        ).hexdigest()
                    }
                    if settings.evidence_ca_file
                    else {}
                ),
                "repository": settings.repository,
                "jira_url": settings.jira_url,
                "evidence_url": settings.evidence_url,
                "jira_project": settings.jira_project,
                "contract_field": settings.jira_contract_field,
                "dependency_statuses": settings.jira_done_statuses,
                "allowed_files": settings.allowed_files,
                "test_image": settings.test_image,
                "test_command": settings.test_command,
                "model": settings.claude_model,
                "timeout": settings.timeout,
                "budget": settings.budget_usd,
                "publication_version": 1,
                "github_prs": settings.github_prs,
                "github_base": settings.github_base,
                "jira_writes": settings.jira_writes,
                "jira_transitions": [asdict(rule) for rule in settings.jira_transitions],
            }
        )

    def prepare(
        self, spec: dict[str, Any], heartbeat: Callable[[], None] = lambda: None
    ) -> dict[str, Any]:
        key = execution_key(spec)

        def load() -> dict[str, Any]:
            ticket = self.jira.get_ticket(spec["ticket_key"], heartbeat)
            if ticket.scope != scope_from_dict(spec["scope"]):
                raise ValueError("requested scope differs from Jira contract")
            context = self.evidence.context(ticket.scope)
            for evidence in ticket.evidence:
                heartbeat()
                self.evidence.verify(evidence)
            source = selected_source(self.settings, ticket.scope.revision, heartbeat)
            return {
                "ticket": ticket_json(ticket),
                "context": context,
                "source": source,
                "spec": spec,
                "policy": self.policy,
            }

        manifest = self.journal.compute(key + ":manifest", spec, load, heartbeat)
        reference = {"key": key, "manifest": digest(manifest)}
        self._guard(reference, heartbeat)
        if self.settings.jira_transitions:

            def guard() -> None:
                self._guard(reference, heartbeat)

            with self.journal.locked(key + ":publication", heartbeat):
                self.jira.transition(
                    spec["ticket_key"],
                    key,
                    reference["manifest"],
                    digest(spec["scope"]),
                    "in_progress",
                    guard,
                    heartbeat,
                )
        return reference

    def _guard(self, reference: dict[str, Any], heartbeat: Callable[[], None]) -> dict[str, Any]:
        manifest = self.journal.read(reference["key"] + ":manifest", reference["manifest"])
        if manifest["policy"] != self.policy or execution_key(manifest["spec"]) != reference["key"]:
            raise ValueError("execution policy or identity changed")
        ticket = ticket_from_wire(manifest["ticket"])
        heartbeat()
        if self.jira.get_ticket(ticket.key, heartbeat) != ticket:
            raise ValueError("Jira ticket or dependencies changed; start a reviewed execution")
        context = self.evidence.context(ticket.scope, manifest["context"]["version"])
        if digest(context) != digest(manifest["context"]):
            raise ValueError("canonical context changed or expired")
        for evidence in ticket.evidence:
            heartbeat()
            self.evidence.verify(evidence)
        return manifest

    def _results(
        self,
        reference: dict[str, Any],
        results: list[dict[str, Any]],
        heartbeat: Callable[[], None] = lambda: None,
    ) -> list[dict[str, Any]]:
        loaded = []
        for index, result in enumerate(results):
            heartbeat()
            if index >= len(STAGES) or result["role"] != STAGES[index]:
                raise ValueError("stage order mismatch")
            key = reference["key"] + ":" + result["role"]
            value = self.journal.read(key, result["artifact"])
            if value["manifest"] != reference["manifest"] or result["verdict"] != value["verdict"]:
                raise ValueError("stage result mismatch")
            evidence = evidence_from_wire(result["evidence"])
            manifest = self.journal.read(reference["key"] + ":manifest", reference["manifest"])
            scope = scope_from_dict(manifest["ticket"]["scope"])
            if (
                evidence.scope != scope
                or evidence.sha256 != hashlib.sha256(canonical(value).encode()).hexdigest()
                or evidence.observed_at != datetime.fromisoformat(value["observed_at"])
                or evidence.expires_at != datetime.fromisoformat(value["expires_at"])
            ):
                raise ValueError("stage evidence does not attest the saved artifact")
            self.evidence.verify(evidence)
            loaded.append(value)
        return loaded

    def stage(
        self, arguments: dict[str, Any], heartbeat: Callable[[], None] = lambda: None
    ) -> dict[str, Any]:
        reference, role, results = arguments["reference"], arguments["role"], arguments["results"]
        manifest = self._guard(reference, heartbeat)
        prior = self._results(reference, results, heartbeat)
        if (
            len(prior) >= len(STAGES)
            or role != STAGES[len(prior)]
            or any(r["verdict"] != "passed" for r in prior)
        ):
            raise ValueError("stage not authorized by preceding gates")
        ticket = ticket_from_wire(manifest["ticket"])

        def execute() -> dict[str, Any]:
            if role == "test":
                test_result = self.tests.execute(
                    ticket.scope.revision, prior[0]["edits"], heartbeat
                )
                result: dict[str, Any] = {
                    "verdict": "passed" if test_result["passed"] else "blocked",
                    "summary": "configured isolated test command completed",
                    "edits": [],
                    "test_result": test_result,
                }
            else:
                result = self.worker.execute(
                    role,
                    {
                        "ticket": manifest["ticket"],
                        "context": manifest["context"],
                        "source": manifest["source"],
                        "allowed_files": list(self.settings.allowed_files),
                        "prior_results": prior,
                    },
                    heartbeat,
                )
            if role == "implement" and result["verdict"] == "passed":
                result["patch"] = make_patch(manifest["source"], result["edits"])
            now = self.clock()
            result.update(
                role=role,
                manifest=reference["manifest"],
                observed_at=now.isoformat(),
                expires_at=min(
                    now + timedelta(hours=1), *(e.expires_at for e in ticket.evidence)
                ).isoformat(),
            )
            return result

        key = reference["key"] + ":" + role
        result = self.journal.compute(
            key, {"manifest": reference["manifest"], "prior": results}, execute, heartbeat
        )
        self._guard(reference, heartbeat)
        evidence = self.evidence.upload(
            digest(key),
            ticket.scope,
            canonical(result).encode(),
            datetime.fromisoformat(result["observed_at"]),
            datetime.fromisoformat(result["expires_at"]),
        )
        return {
            "role": role,
            "verdict": result["verdict"],
            "artifact": digest(result),
            "evidence": json.loads(canonical(asdict(evidence))),
        }

    def finish(
        self, arguments: dict[str, Any], heartbeat: Callable[[], None] = lambda: None
    ) -> dict[str, Any]:
        with self.journal.locked(arguments["reference"]["key"] + ":publication", heartbeat):
            return self._finish(arguments, heartbeat)

    def _finish(self, arguments: dict[str, Any], heartbeat: Callable[[], None]) -> dict[str, Any]:
        reference, results = arguments["reference"], arguments["results"]
        manifest = self._guard(reference, heartbeat)
        loaded = self._results(reference, results, heartbeat)
        ready = len(loaded) == 4 and all(r["verdict"] == "passed" for r in loaded)
        ticket = ticket_from_wire(manifest["ticket"])

        def guard() -> None:
            self._guard(reference, heartbeat)
            self._results(reference, results, heartbeat)
            if pull_request is not None:
                self.github.verify(pull_request, heartbeat)

        pull_request: dict[str, Any] | None = None
        if ready and self.settings.github_prs:
            pull_request = self.github.publish(
                reference["key"],
                reference["manifest"],
                results[0]["artifact"],
                ticket.scope,
                loaded[0],
                guard,
                heartbeat,
            )
        transitioned = self.jira.transition(
            ticket.key,
            reference["key"],
            reference["manifest"],
            digest(asdict(ticket.scope)),
            "in_review" if ready else "blocked",
            guard,
            heartbeat,
        )
        report = {
            "schema_version": 2,
            "execution_id": reference["key"],
            "ticket_key": ticket.key,
            "scope": asdict(ticket.scope),
            "outcome": "ready_for_review" if ready else "blocked",
            "manifest": reference["manifest"],
            "stages": results,
            "patch_sha256": hashlib.sha256(loaded[0].get("patch", "").encode()).hexdigest()
            if loaded
            else None,
            "pull_request": pull_request,
            "jira_transitioned": transitioned,
            "merged": False,
            "deployed": False,
        }
        # Normalize datetimes before idempotent remote comparison.
        report = json.loads(canonical(report))
        reported = self.jira.publish(ticket.key, reference["key"], report, guard)
        guard()
        self.journal.compute(reference["key"] + ":report", report, lambda: report, heartbeat)
        return {
            "execution_id": reference["key"],
            "outcome": report["outcome"],
            "jira_reported": reported,
            "jira_transitioned": transitioned,
            "pull_request": pull_request,
            "manifest": reference["manifest"],
            "patch_artifact": results[0]["artifact"] if results else None,
        }

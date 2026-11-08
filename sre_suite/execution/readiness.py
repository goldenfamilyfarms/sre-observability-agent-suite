"""Read-only preflight. Findings are observations, never execution authorization."""

import base64
import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal
from urllib.parse import quote, urlencode

from ..contracts import JiraExecutionTicket
from ..serialization import scope_from_dict
from .config import Settings, flag, temporal_address
from .evidence_client import EvidenceClient
from .http import JsonHttp, RemoteError
from .jira import JiraClient
from .process import minimal_environment, run_process
from .runtime import execution_key
from .wire import digest
from .workspace import selected_source

Status = Literal["pass", "fail", "unverified", "skipped"]


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str
    error: str | None = None


def report(checks: list[Check], identity: str | None = None) -> dict[str, Any]:
    status = (
        "blocked"
        if any(c.status == "fail" for c in checks)
        else ("needs_validation" if any(c.status == "unverified" for c in checks) else "passed")
    )
    return {
        "schema_version": 1,
        "status": status,
        "read_only": True,
        "execution_id": identity,
        "checked_at": datetime.now(UTC).isoformat(),
        "checks": [asdict(c) for c in checks],
    }


def exit_code(result: dict[str, Any]) -> int:
    return {"passed": 0, "blocked": 1, "needs_validation": 2}[result["status"]]


def failure(name: str, detail: str, error: Exception) -> Check:
    # Do not include exception strings, HTTP bodies, credentials, paths or subprocess output.
    reason = "invalid_or_unexpected_response"
    if isinstance(error, TimeoutError):
        reason = "timeout"
    elif isinstance(error, RemoteError):
        reason = "access_denied" if error.status in {401, 403} else "remote_unavailable"
    elif isinstance(error, (OSError, ImportError)):
        reason = "local_dependency_unavailable"
    return Check(name, "fail", detail, reason)


def require(condition: bool) -> None:
    if not condition:
        raise ValueError("preflight condition not satisfied")


class ReadOnlyHttp(JsonHttp):
    """Defense in depth: reject any attempt to reuse a writing adapter operation."""

    def __init__(self, delegate: JsonHttp, pulse: Callable[[], None], *, evidence: bool = False):
        self.delegate = delegate
        self.pulse = pulse
        self.evidence = evidence

    def call(self, method: str, path: str, payload: object = None) -> object:
        self.pulse()
        if method != "GET" and not (
            self.evidence and method == "POST" and path in {"/v1/context", "/v1/evidence/read"}
        ):
            raise ValueError("preflight may not mutate external state")
        value = self.delegate.call(method, path, payload)
        self.pulse()
        return value


def temporal_config(env: Mapping[str, str]) -> tuple[str, str, str, bool]:
    tls = flag(env, "SRE_TEMPORAL_TLS")
    address = temporal_address(env.get("SRE_TEMPORAL_ADDRESS", "127.0.0.1:7233"), tls)
    namespace = env.get("SRE_TEMPORAL_NAMESPACE", "default")
    queue = env.get("SRE_TEMPORAL_TASK_QUEUE", "sre-engineering-v1")
    for value in (namespace, queue):
        require(
            0 < len(value) <= 200 and value == value.strip() and not any(ord(c) < 32 for c in value)
        )
    return address, namespace, queue, tls


class Readiness:
    def __init__(
        self,
        settings: Settings,
        spec: dict[str, Any],
        env: Mapping[str, str],
        *,
        transports: Mapping[str, JsonHttp] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings, self.spec, self.env = settings, spec, env
        self.identity = execution_key(spec)  # Includes preview ID/generation; rejects production.
        self.scope = scope_from_dict(spec["scope"])
        self.monotonic, self.deadline = monotonic, monotonic() + 90
        self.checks: list[Check] = [
            Check("configuration", "pass", "Runtime configuration is valid."),
            Check("execution_scope", "pass", "Versioned non-production execution scope is valid."),
        ]
        self.ticket: JiraExecutionTicket | None = None
        self.context: dict[str, Any] | None = None
        supplied = transports or {}
        auth = base64.b64encode(f"{settings.jira_email}:{settings.jira_token}".encode()).decode()
        delegates = {
            "jira": supplied.get("jira") or JsonHttp(settings.jira_url, "Basic " + auth, timeout=5),
            "evidence": supplied.get("evidence")
            or JsonHttp(settings.evidence_url, "Bearer " + settings.evidence_token, timeout=5),
            "github": supplied.get("github")
            or JsonHttp("https://api.github.com", "Bearer " + settings.github_token, timeout=5),
            "model": supplied.get("model")
            or JsonHttp(
                "https://api.anthropic.com",
                "Bearer " + settings.claude_key,
                headers={"anthropic-version": "2026-10-21"},
                timeout=5,
            ),
        }
        self.http = {
            k: ReadOnlyHttp(v, self.pulse, evidence=k == "evidence") for k, v in delegates.items()
        }
        self.jira = JiraClient(settings, self.http["jira"])
        self.evidence = EvidenceClient(
            settings.evidence_url, settings.evidence_token, self.http["evidence"]
        )

    def pulse(self) -> None:
        if self.monotonic() >= self.deadline:
            raise TimeoutError

    def check(self, name: str, detail: str, operation: Callable[[], object]) -> bool:
        try:
            self.pulse()
            operation()
            self.pulse()
        except Exception as error:
            self.checks.append(failure(name, detail, error))
            return False
        self.checks.append(Check(name, "pass", detail))
        return True

    def inspect(self) -> None:
        self.check(
            "temporal_configuration",
            "Review Temporal address, TLS, namespace and queue settings.",
            lambda: temporal_config(self.env),
        )
        self.check(
            "execution_enabled",
            "Enable Claude execution to run an engineering ticket.",
            lambda: require(self.settings.claude_enabled),
        )
        self.check(
            "local_source",
            "The exact source commit can be exported within the worker's path/size limits.",
            lambda: selected_source(self.settings, self.scope.revision, self.pulse),
        )
        self.check(
            "journal_directory",
            "Provide an existing owner-only writable journal directory outside the checkout.",
            self._journal,
        )
        self.check(
            "execution_journal",
            "This run ID has no saved execution manifest; reconcile previous work before reuse.",
            lambda: require(
                not (
                    self.settings.work_root / (digest(self.identity + ":manifest") + ".json")
                ).exists()
            ),
        )
        self.checks.append(
            Check(
                "journal_durability",
                "unverified",
                "Read-only inspection cannot prove shared filesystem "
                "locking, durability or backups.",
            )
        )
        self.check(
            "claude_cli",
            "Install a Claude CLI supporting the worker's required restricted execution flags.",
            self._cli,
        )
        self.check(
            "test_image",
            "Docker can inspect the configured immutable local image "
            "without pulling or running it.",
            self._image,
        )
        self.checks.append(
            Check(
                "test_execution",
                "unverified",
                "The configured test command and container isolation "
                "require a separate execution test.",
            )
        )
        self.check(
            "model_access",
            "The configured API key can read the requested model or resolve its alias.",
            self._model,
        )
        self.checks.append(
            Check(
                "model_inference",
                "unverified",
                "Model metadata access does not prove inference permission, "
                "quota, billing or CLI completion.",
            )
        )
        if self.check(
            "jira_identity", "Jira credentials identify an active account.", self._jira_identity
        ):
            if self.check(
                "jira_ticket",
                "Jira ticket, repository, dependencies and complete execution scope match.",
                self._ticket,
            ):
                self.check(
                    "jira_permissions",
                    "Jira issue permissions cover the enabled operations.",
                    self._permissions,
                )
                self._transition_checks()
        else:
            self.checks.append(Check("jira_ticket", "skipped", "Jira identity check failed."))
        if self.settings.jira_writes:
            self.checks.append(
                Check(
                    "jira_write_authorization",
                    "unverified",
                    "Issue permissions do not prove token write scopes or "
                    "transition/property side effects.",
                )
            )
        if self.ticket is not None:
            self.check(
                "preview_context_evidence",
                "The current preview generation authorizes context and "
                "digest-verified baseline evidence reads.",
                self._context,
            )
        else:
            self.checks.append(
                Check("preview_context_evidence", "skipped", "A validated ticket is required.")
            )
        self.checks.append(
            Check(
                "evidence_upload_authorization",
                "unverified",
                "Read grants do not prove an evidence-upload grant; no upload probe is performed.",
            )
        )
        if self.settings.github_prs:
            self.check(
                "github_repository",
                "The configured GitHub repository is accessible, writable "
                "by reported role, and not archived or disabled.",
                self._github,
            )
            self.check(
                "github_base",
                "The GitHub base branch still equals the scoped source revision.",
                self._base,
            )
            self.checks.append(
                Check(
                    "github_write_authorization",
                    "unverified",
                    "Repository role/read access cannot prove token "
                    "Contents/PR write scopes or repository rules.",
                )
            )
        else:
            self.checks.append(
                Check(
                    "github_publication",
                    "skipped",
                    "GitHub PR publication is disabled; patch-only mode is configured.",
                )
            )

    def _journal(self) -> None:
        root = self.settings.work_root
        require(
            root.is_dir()
            and not root.is_symlink()
            and root.stat().st_mode & 0o077 == 0
            and root.stat().st_uid == os.getuid()
            and os.access(root, os.R_OK | os.W_OK | os.X_OK)
        )

    def _cli(self) -> None:
        with TemporaryDirectory(prefix="sre-preflight-") as directory:
            env = {**minimal_environment(), "CLAUDE_CONFIG_DIR": str(Path(directory) / "config")}
            result = run_process(
                [self.settings.claude_binary, "--bare", "--help"],
                Path(directory),
                env,
                timeout=10,
                max_output=100000,
                heartbeat=self.pulse,
            )
        require(result.code == 0)
        for option in (
            b"--restricted",
            b"--tools",
            b"--strict-mcp-config",
            b"--json-schema",
            b"--max-budget-usd",
            b"--setting-sources",
            b"--no-session-persistence",
        ):
            require(option in result.output)

    def _image(self) -> None:
        result = run_process(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .RepoDigests}}",
                self.settings.test_image,
            ],
            self.settings.repository_path,
            minimal_environment(),
            timeout=10,
            max_output=100000,
            heartbeat=self.pulse,
        )
        require(result.code == 0)
        entries = json.loads(result.output)
        expected = self.settings.test_image.split("@", 1)[1]
        require(
            isinstance(entries, list)
            and any(isinstance(v, str) and v.endswith("@" + expected) for v in entries)
        )

    def _model(self) -> None:
        value = self.http["model"].call(
            "GET", "/v1/models/" + quote(self.settings.claude_model, safe="")
        )
        require(
            isinstance(value, dict)
            and value.get("type") == "model"
            and isinstance(value.get("id"), str)
            and bool(value["id"])
            and value.get("lifecycle") in {None, "active", "deprecated"}
        )

    def _jira_identity(self) -> None:
        value = self.http["jira"].call("GET", "/rest/api/3/myself")
        require(
            isinstance(value, dict) and value.get("active") is True and bool(value.get("accountId"))
        )

    def _ticket(self) -> None:
        ticket = self.jira.get_ticket(self.spec["ticket_key"], self.pulse)
        require(ticket.scope == self.scope)
        self.ticket = ticket

    def _permissions(self) -> None:
        names = ["BROWSE_PROJECTS"]
        if self.settings.jira_writes:
            names.append("EDIT_ISSUES")
        if self.settings.jira_transitions:
            names.append("TRANSITION_ISSUES")
        query = urlencode({"issueKey": self.spec["ticket_key"], "permissions": ",".join(names)})
        value = self.http["jira"].call("GET", "/rest/api/3/mypermissions?" + query)
        require(isinstance(value, dict))
        assert isinstance(value, dict)
        permissions = value.get("permissions", {})
        require(
            isinstance(permissions, dict)
            and all(
                isinstance(permissions.get(k), dict)
                and permissions[k].get("havePermission") is True
                for k in names
            )
        )

    def _transition_checks(self) -> None:
        if not self.settings.jira_transitions:
            self.checks.append(
                Check("jira_transitions", "skipped", "Jira transitions are disabled.")
            )
            return
        self.check(
            "jira_transition_targets",
            "Configured transition destinations are nonterminal and the "
            "milestone source IDs form a usable sequence.",
            self._targets,
        )
        self.check(
            "jira_initial_transition",
            "The configured in-progress transition is available from the "
            "issue's current status without missing fields.",
            self._initial_transition,
        )
        self.checks.append(
            Check(
                "jira_future_transitions",
                "unverified",
                "Review/blocked transitions cannot be proven available from "
                "a future status without changing the issue.",
            )
        )

    def _targets(self) -> None:
        rules = {r.milestone: r for r in self.settings.jira_transitions}
        for rule in rules.values():
            value = self.http["jira"].call("GET", "/rest/api/3/status/" + rule.status_id)
            require(
                isinstance(value, dict)
                and value.get("id") == rule.status_id
                and value.get("statusCategory", {}).get("key") in {"new", "indeterminate"}
            )
        require(
            all(
                rules["in_progress"].status_id in rules[k].from_status_ids
                for k in ("in_review", "blocked")
            )
        )

    def _initial_transition(self) -> None:
        rule = next(r for r in self.settings.jira_transitions if r.milestone == "in_progress")
        path = "/rest/api/3/issue/" + self.spec["ticket_key"]
        issue = self.http["jira"].call("GET", path + "?fields=status")
        require(isinstance(issue, dict))
        assert isinstance(issue, dict)
        status = issue.get("fields", {}).get("status", {})
        require(
            issue.get("key") == self.spec["ticket_key"]
            and status.get("id") in rule.from_status_ids
            and status.get("statusCategory", {}).get("key") in {"new", "indeterminate"}
        )
        value = self.http["jira"].call("GET", path + "/transitions?expand=transitions.fields")
        require(isinstance(value, dict))
        assert isinstance(value, dict)
        transitions = value.get("transitions")
        require(isinstance(transitions, list))
        assert isinstance(transitions, list)
        matches = [
            t for t in transitions if isinstance(t, dict) and t.get("id") == rule.transition_id
        ]
        require(len(matches) == 1)
        chosen = matches[0]
        target = chosen.get("to", {})
        require(
            chosen.get("isAvailable") is not False
            and target.get("id") == rule.status_id
            and target.get("statusCategory", {}).get("key") in {"new", "indeterminate"}
            and not any(
                f.get("required") and not f.get("hasDefaultValue")
                for f in chosen.get("fields", {}).values()
            )
        )

    def _context(self) -> None:
        assert self.ticket is not None
        context = self.evidence.context(self.scope)
        for evidence in self.ticket.evidence:
            self.pulse()
            self.evidence.verify(evidence)
        self.context = context

    def _github(self) -> None:
        value = self.http["github"].call("GET", "/repos/" + self.settings.repository)
        require(isinstance(value, dict))
        assert isinstance(value, dict)
        require(
            value.get("full_name") == self.settings.repository
            and value.get("archived") is False
            and value.get("disabled") is False
        )
        permissions = value.get("permissions")
        if isinstance(permissions, dict) and "push" in permissions:
            require(permissions["push"] is True)
        else:
            self.checks.append(
                Check(
                    "github_repository_role",
                    "unverified",
                    "GitHub did not report repository-role permissions for this credential type.",
                )
            )

    def _base(self) -> None:
        value = self.http["github"].call(
            "GET",
            "/repos/"
            + self.settings.repository
            + "/git/ref/heads/"
            + quote(self.settings.github_base, safe=""),
        )
        require(
            isinstance(value, dict)
            and value.get("ref") == "refs/heads/" + self.settings.github_base
            and value.get("object", {}).get("type") == "commit"
            and value["object"].get("sha") == self.scope.revision
        )

    def finish(self) -> dict[str, Any]:
        if self.ticket is not None and self.context is not None:

            def revalidate() -> None:
                require(self.jira.get_ticket(self.spec["ticket_key"], self.pulse) == self.ticket)
                assert self.context is not None and self.ticket is not None
                context = self.evidence.context(self.scope, self.context["version"])
                require(digest(context) == digest(self.context))
                for evidence in self.ticket.evidence:
                    self.evidence.verify(evidence)

            self.check(
                "final_scope_fence",
                "Ticket, preview generation, context and baseline evidence "
                "remain valid at the end of preflight.",
                revalidate,
            )
        else:
            self.checks.append(
                Check(
                    "final_scope_fence",
                    "skipped",
                    "Initial scoped context/evidence checks did not pass.",
                )
            )
        return report(self.checks, self.identity)


async def run_preflight(env: Mapping[str, str], spec_path: Path) -> dict[str, Any]:
    from ..api_wire import decode

    try:
        settings = Settings.from_env(env)
    except Exception as error:
        required_names: tuple[str, ...] = (
            "SRE_JIRA_BASE_URL",
            "SRE_JIRA_EMAIL",
            "SRE_JIRA_API_TOKEN",
            "SRE_JIRA_PROJECT",
            "SRE_JIRA_CONTRACT_FIELD",
            "SRE_JIRA_DONE_STATUSES",
            "SRE_EVIDENCE_API_URL",
            "SRE_EVIDENCE_API_TOKEN",
            "SRE_REPOSITORY",
            "SRE_REPO_DIR",
            "SRE_WORK_ROOT",
            "SRE_ALLOWED_FILES_JSON",
            "SRE_TEST_IMAGE",
            "SRE_TEST_COMMAND_JSON",
            "SRE_CLAUDE_BINARY",
            "SRE_CLAUDE_MODEL",
            "ANTHROPIC_API_KEY",
        )
        if env.get("SRE_GITHUB_PRS_ENABLED") == "true":
            required_names += ("SRE_GITHUB_TOKEN",)
        if env.get("SRE_JIRA_TRANSITIONS_ENABLED") == "true":
            required_names += ("SRE_JIRA_TRANSITIONS_JSON",)
        missing = [name for name in required_names if not env.get(name, "").strip()]
        detail = "Review runtime environment values and strict enable flags."
        if missing:
            detail = "Missing required variables: " + ", ".join(missing) + "."
        return report([failure("configuration", detail, error)])
    try:
        with spec_path.open("rb") as source:
            raw = source.read(65537)
        require(len(raw) <= 65536)
        spec = decode(raw)
        require(isinstance(spec, dict))
        assert isinstance(spec, dict)
        readiness = Readiness(settings, spec, env)
    except Exception as error:
        return report(
            [
                failure(
                    "execution_scope",
                    "Provide a bounded version 2 non-production execution specification.",
                    error,
                )
            ]
        )
    readiness.inspect()
    try:
        readiness.pulse()
        from .readiness_temporal import probe

        readiness.checks.extend(await probe(env, readiness.identity))
    except Exception as error:
        readiness.checks.append(
            failure(
                "temporal_connectivity",
                "Install the orchestration extra and check Temporal configuration.",
                error,
            )
        )
    return readiness.finish()

"""Explicit private runtime configuration. No discovery of account credentials."""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, urlsplit


def https_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.port not in (None, 443)
    ):
        raise ValueError("expected a credential-free HTTPS URL")
    return value.rstrip("/")


def safe_path(value: str) -> str:
    parts = value.split("/")
    if (
        not re.fullmatch(r"[A-Za-z0-9_./-]{1,240}", value)
        or value.startswith("/")
        or any(p in {"", ".", "..", ".git", ".github", ".claude"} for p in parts)
        or any(p == ".env" or p.startswith(".env.") for p in parts)
    ):
        raise ValueError("unsafe or protected repository path")
    return value


def flag(env: Mapping[str, str], name: str) -> bool:
    value = env.get(name, "false")
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


def required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value.strip() or "\n" in value or "\r" in value:
        raise ValueError(f"missing or invalid {name}")
    return value


@dataclass(frozen=True)
class TransitionRule:
    milestone: str
    transition_id: str
    status_id: str
    from_status_ids: tuple[str, ...]


def transition_rules(env: Mapping[str, str]) -> tuple[TransitionRule, ...]:
    from ..api_wire import decode, fields

    raw = fields(
        decode(required(env, "SRE_JIRA_TRANSITIONS_JSON").encode()),
        {"in_progress", "in_review", "blocked"},
    )
    result = []
    for milestone in sorted(raw):
        rule = fields(raw[milestone], {"transition_id", "status_id", "from_status_ids"})
        sources = rule["from_status_ids"]
        if (
            not isinstance(sources, list)
            or not 1 <= len(sources) <= 20
            or any(
                not isinstance(x, str) or not re.fullmatch(r"[1-9][0-9]*", x)
                for x in [rule["transition_id"], rule["status_id"], *sources]
            )
            or rule["status_id"] in sources
            or len(set(sources)) != len(sources)
        ):
            raise ValueError("invalid Jira transition IDs or source statuses")
        result.append(
            TransitionRule(
                milestone, str(rule["transition_id"]), str(rule["status_id"]), tuple(sources)
            )
        )
    if len({r.status_id for r in result}) != 3:
        raise ValueError("milestones must have distinct target statuses")
    return tuple(result)


@dataclass(frozen=True)
class Settings:
    jira_url: str
    jira_email: str
    jira_token: str = field(repr=False)
    jira_project: str
    jira_contract_field: str
    jira_done_statuses: tuple[str, ...]
    jira_writes: bool
    evidence_url: str
    evidence_token: str = field(repr=False)
    repository: str
    repository_path: Path
    work_root: Path
    allowed_files: tuple[str, ...]
    test_image: str
    test_command: tuple[str, ...]
    claude_binary: str
    claude_model: str
    claude_key: str = field(repr=False)
    claude_enabled: bool
    timeout: int = 300
    budget_usd: float = 2.0
    github_prs: bool = False
    github_token: str = field(default="", repr=False)
    github_base: str = "main"
    jira_transitions: tuple[TransitionRule, ...] = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        project = required(env, "SRE_JIRA_PROJECT")
        field_name = required(env, "SRE_JIRA_CONTRACT_FIELD")
        repository = required(env, "SRE_REPOSITORY")
        image = required(env, "SRE_TEST_IMAGE")
        if not re.fullmatch(r"[A-Z][A-Z0-9]*", project):
            raise ValueError("invalid SRE_JIRA_PROJECT")
        if not re.fullmatch(r"customfield_[0-9]+", field_name):
            raise ValueError("SRE_JIRA_CONTRACT_FIELD must name a text custom field")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("invalid SRE_REPOSITORY")
        if not re.fullmatch(r"[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64}", image):
            raise ValueError("SRE_TEST_IMAGE must be pinned by digest")
        allowed = json.loads(required(env, "SRE_ALLOWED_FILES_JSON"))
        command = json.loads(required(env, "SRE_TEST_COMMAND_JSON"))
        if (
            not isinstance(allowed, list)
            or not 1 <= len(allowed) <= 50
            or any(not isinstance(p, str) for p in allowed)
            or len(set(allowed)) != len(allowed)
        ):
            raise ValueError("SRE_ALLOWED_FILES_JSON requires 1..50 unique paths")
        if (
            not isinstance(command, list)
            or not 1 <= len(command) <= 30
            or any(not isinstance(p, str) or not p or len(p) > 1000 for p in command)
        ):
            raise ValueError("SRE_TEST_COMMAND_JSON requires a bounded argv array")
        repo_path = Path(required(env, "SRE_REPO_DIR"))
        root = Path(required(env, "SRE_WORK_ROOT"))
        if not repo_path.is_absolute() or not root.is_absolute():
            raise ValueError("repository and durable work directory must be absolute paths")
        if root.resolve().is_relative_to(repo_path.resolve()):
            raise ValueError("SRE_WORK_ROOT must be outside the source repository")
        timeout = int(env.get("SRE_WORKER_TIMEOUT_SECONDS", "300"))
        budget = float(env.get("SRE_CLAUDE_BUDGET_USD", "2"))
        if not 10 <= timeout <= 1800 or not 0 < budget <= 20:
            raise ValueError("invalid worker timeout or cost budget")
        statuses = tuple(required(env, "SRE_JIRA_DONE_STATUSES").split(","))
        if any(not status.strip() for status in statuses):
            raise ValueError("invalid Jira dependency statuses")
        github_prs = flag(env, "SRE_GITHUB_PRS_ENABLED")
        github_token = required(env, "SRE_GITHUB_TOKEN") if github_prs else ""
        github_base = env.get("SRE_GITHUB_BASE_BRANCH", "main")
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_/-]{0,150}", github_base)
            or "//" in github_base
            or github_base.endswith("/")
            or github_base.startswith("sre/run-")
        ):
            raise ValueError("invalid or reserved GitHub base branch")
        transitions_enabled = flag(env, "SRE_JIRA_TRANSITIONS_ENABLED")
        if transitions_enabled and (not github_prs or not flag(env, "SRE_JIRA_WRITES_ENABLED")):
            raise ValueError("Jira transitions require PR publication and Jira writes enabled")
        rules = transition_rules(env) if transitions_enabled else ()
        return cls(
            https_url(required(env, "SRE_JIRA_BASE_URL")),
            required(env, "SRE_JIRA_EMAIL"),
            required(env, "SRE_JIRA_API_TOKEN"),
            project,
            field_name,
            statuses,
            flag(env, "SRE_JIRA_WRITES_ENABLED"),
            https_url(required(env, "SRE_EVIDENCE_API_URL")),
            required(env, "SRE_EVIDENCE_API_TOKEN"),
            repository,
            repo_path,
            root,
            tuple(safe_path(p) for p in allowed),
            image,
            tuple(command),
            required(env, "SRE_CLAUDE_BINARY"),
            required(env, "SRE_CLAUDE_MODEL"),
            required(env, "ANTHROPIC_API_KEY"),
            flag(env, "SRE_CLAUDE_ENABLED"),
            timeout,
            budget,
            github_prs,
            github_token,
            github_base,
            rules,
        )


def temporal_address(value: str, tls: bool) -> str:
    endpoint = urlsplit("//" + value)
    if (
        not endpoint.hostname
        or not endpoint.port
        or endpoint.username
        or endpoint.password
        or endpoint.path
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("Temporal address must be host:port")
    if not tls and endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("remote Temporal connections require TLS")
    return value

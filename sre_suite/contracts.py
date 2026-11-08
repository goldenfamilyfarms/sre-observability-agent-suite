"""Immutable domain contracts; untrusted adapters must validate before construction."""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlparse


class Environment(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


def nonempty(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty")


def aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must have a timezone")


@dataclass(frozen=True)
class Scope:
    service: str
    environment: Environment
    revision: str

    def __post_init__(self) -> None:
        nonempty(self.service, "service")
        if not isinstance(self.environment, Environment):
            raise ValueError("environment must be an explicit Environment")
        if not re.fullmatch(r"[0-9a-f]{40}", self.revision):
            raise ValueError("revision must be a full lowercase Git SHA")


@dataclass(frozen=True)
class Evidence:
    id: str
    scope: Scope
    uri: str
    sha256: str
    observed_at: datetime
    expires_at: datetime
    producer: str

    def __post_init__(self) -> None:
        nonempty(self.id, "evidence id")
        nonempty(self.producer, "producer")
        parsed = urlparse(self.uri)
        if parsed.scheme not in {"https", "urn"} or not parsed.path:
            raise ValueError("evidence URI must be https or urn")
        if parsed.scheme == "https" and (
            not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("use a credential-free evidence URI without query or fragment")
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValueError("sha256 must be a full content digest")
        aware(self.observed_at)
        aware(self.expires_at)
        if self.expires_at <= self.observed_at:
            raise ValueError("evidence expiry must follow observation")

    def valid_for(self, scope: Scope, now: datetime) -> bool:
        aware(now)
        return self.scope == scope and self.observed_at <= now < self.expires_at


@dataclass(frozen=True)
class JiraExecutionTicket:
    key: str
    workstream: str
    scope: Scope
    repository: str
    current_state: str
    desired_state: str
    objective: str
    implementation_scope: tuple[str, ...]
    dependencies: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    qa_requirements: tuple[str, ...]
    telemetry_requirements: tuple[str, ...]
    delivery_requirements: tuple[str, ...]
    rollback_strategy: str
    evidence: tuple[Evidence, ...]

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Z][A-Z0-9]*-[1-9][0-9]*", self.key):
            raise ValueError("invalid Jira key")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
            raise ValueError("repository must be owner/name")
        for name in (
            "workstream",
            "current_state",
            "desired_state",
            "objective",
            "rollback_strategy",
        ):
            nonempty(getattr(self, name), name)
        for name in (
            "implementation_scope",
            "acceptance_criteria",
            "qa_requirements",
            "telemetry_requirements",
            "delivery_requirements",
        ):
            values = getattr(self, name)
            if not isinstance(values, tuple) or not values:
                raise ValueError(f"{name} must be a non-empty tuple")
            for value in values:
                nonempty(value, name)
        if (
            not isinstance(self.dependencies, tuple)
            or len(set(self.dependencies)) != len(self.dependencies)
            or self.key in self.dependencies
        ):
            raise ValueError("invalid or duplicate dependencies")
        if not isinstance(self.evidence, tuple) or not self.evidence:
            raise ValueError("ticket requires evidence")
        if any(item.scope != self.scope for item in self.evidence):
            raise ValueError("ticket evidence scope mismatch")


@dataclass(frozen=True)
class Release:
    scope: Scope
    artifact_digest: str
    plan_digest: str
    required_gates: tuple[str, ...]

    def __post_init__(self) -> None:
        for digest in (self.artifact_digest, self.plan_digest):
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ValueError("release requires immutable SHA-256 digests")
        if not self.required_gates or len(set(self.required_gates)) != len(self.required_gates):
            raise ValueError("release must specify unique required gates")
        for gate in self.required_gates:
            nonempty(gate, "gate")


@dataclass(frozen=True)
class GateResult:
    gate: str
    release: Release
    passed: bool
    evidence: Evidence


@dataclass(frozen=True)
class Approval:
    id: str
    release: Release
    approver: str
    issued_at: datetime
    expires_at: datetime
    revoked: bool = False

    def __post_init__(self) -> None:
        nonempty(self.id, "approval id")
        nonempty(self.approver, "approver")
        aware(self.issued_at)
        aware(self.expires_at)
        if self.expires_at <= self.issued_at:
            raise ValueError("approval must have a positive validity window")

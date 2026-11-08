"""Integration ports. Production adapters must authenticate actors and validate outputs."""

from dataclasses import dataclass
from typing import Protocol

from .contracts import Evidence, JiraExecutionTicket, Scope
from .memory import ContextSnapshot, Finding


@dataclass(frozen=True)
class AgentRequest:
    execution_id: str
    ticket: JiraExecutionTicket
    context: ContextSnapshot
    max_attempts: int = 3
    timeout_seconds: int = 900

    def __post_init__(self) -> None:
        if not self.execution_id.strip() or self.ticket.scope != self.context.scope:
            raise ValueError("invalid execution or context scope")
        if not 1 <= self.max_attempts <= 3 or not 1 <= self.timeout_seconds <= 3600:
            raise ValueError("worker execution must be bounded")


@dataclass(frozen=True)
class AgentResult:
    execution_id: str
    scope: Scope
    context_version: int
    outcome: str
    findings: tuple[Finding, ...]
    evidence: tuple[Evidence, ...]


class JiraPort(Protocol):
    def get_ticket(self, key: str) -> JiraExecutionTicket: ...

    def create_ticket(self, ticket: JiraExecutionTicket, idempotency_key: str) -> str: ...


class EngineeringWorker(Protocol):
    def execute(self, request: AgentRequest) -> AgentResult: ...


def validate_result(request: AgentRequest, result: AgentResult) -> None:
    if (
        result.execution_id != request.execution_id
        or result.scope != request.ticket.scope
        or result.context_version != request.context.version
        or result.outcome not in {"succeeded", "failed", "blocked"}
    ):
        raise ValueError("worker result does not match its execution")
    if not result.evidence or any(item.scope != result.scope for item in result.evidence):
        raise ValueError("worker result requires scoped evidence")
    if any(finding.scope != result.scope for finding in result.findings):
        raise ValueError("worker findings scope mismatch")

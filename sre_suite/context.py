"""Trusted local boundary for preparing and validating preview worker executions."""

from datetime import datetime

from .agents import AgentRequest, AgentResult, validate_result
from .contracts import Approval, GateResult, JiraExecutionTicket, Release
from .evidence import EvidenceVerifier
from .memory import KnowledgeStore
from .policy import PromotionPolicy, promotion_denials
from .previews import PreviewRegistry


class PreviewCoordinator:
    """Validates local state; does not authenticate workers, deploy, or publish GitOps."""

    def __init__(
        self, previews: PreviewRegistry, knowledge: KnowledgeStore, evidence: EvidenceVerifier
    ) -> None:
        self._previews = previews
        self._knowledge = knowledge
        self._evidence = evidence

    def prepare(
        self, execution_id: str, ticket: JiraExecutionTicket, now: datetime
    ) -> AgentRequest:
        self._previews.require_current(ticket.scope, now)
        snapshot = self._knowledge.get_ticket_context(ticket, now)
        if len(snapshot.findings) > 200:
            raise ValueError("context exceeds local worker budget; narrow the task")
        return AgentRequest(execution_id, ticket, snapshot)

    def validate_completion(
        self, request: AgentRequest, result: AgentResult, now: datetime
    ) -> None:
        self._previews.require_current(request.ticket.scope, now)
        current = self._knowledge.get_ticket_context(request.ticket, now)
        if current != request.context:
            raise ValueError("knowledge changed or evidence expired; rebuild worker context")
        validate_result(request, result)
        evidence = result.evidence + tuple(finding.evidence for finding in result.findings)
        if not all(self._evidence.verify(item, result.scope, now) for item in evidence):
            raise ValueError("worker evidence content could not be verified")

    def promotion_denials(
        self,
        release: Release,
        gates: tuple[GateResult, ...],
        policy: PromotionPolicy,
        now: datetime,
        approval: Approval | None = None,
    ) -> tuple[str, ...]:
        reasons = list(promotion_denials(release, gates, policy, now, approval))
        try:
            self._previews.require_current(release.scope, now)
        except ValueError as error:
            reasons.append(str(error))
        if any(not self._evidence.verify(gate.evidence, release.scope, now) for gate in gates):
            reasons.append("gate evidence content could not be verified")
        return tuple(reasons)

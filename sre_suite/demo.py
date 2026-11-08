"""Synthetic worker/context/restart demonstration; never calls an external system."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from .agents import AgentRequest, AgentResult, validate_result
from .contracts import Environment, Evidence, JiraExecutionTicket, Scope
from .memory import Finding, KnowledgeStore, MemoryKind
from .workflow import Checkpoints, TicketState, Transition, advance


def example_ticket(now: datetime) -> JiraExecutionTicket:
    scope = Scope("demo-service", Environment.DEVELOPMENT, "a" * 40)
    evidence = Evidence(
        "fixture-evidence",
        scope,
        "urn:synthetic:baseline",
        "b" * 64,
        now - timedelta(minutes=1),
        now + timedelta(hours=1),
        "fixture",
    )
    return JiraExecutionTicket(
        "DEMO-1",
        "demo",
        scope,
        "example/demo-service",
        "missing correlation",
        "correlated logs",
        "add correlation to the synthetic service",
        ("request logging",),
        (),
        ("request logs include trace identifiers",),
        ("test a synthetic request",),
        ("verify correlation",),
        ("open a reviewable PR",),
        "revert the synthetic change",
        (evidence,),
    )


class FakeWorker:
    def execute(self, request: AgentRequest) -> AgentResult:
        return AgentResult(
            request.execution_id,
            request.ticket.scope,
            request.context.version,
            "succeeded",
            (),
            request.ticket.evidence,
        )


def run_demo() -> dict[str, object]:
    now = datetime.now(UTC)
    ticket = example_ticket(now)
    with TemporaryDirectory() as directory:
        database = str(Path(directory) / "demo.db")
        memory = KnowledgeStore(database)
        memory.propose_finding(
            Finding(
                "finding-1",
                ticket.scope,
                MemoryKind.SEMANTIC,
                "demo-service",
                "uses",
                "synthetic logging",
                ticket.evidence[0],
            )
        )
        memory.reconcile_finding("finding-1", 0, now)
        context = memory.get_context_snapshot(ticket.scope, now)
        request = AgentRequest("demo-run", ticket, context)
        result = FakeWorker().execute(request)
        validate_result(request, result)
        state = advance(
            ticket,
            TicketState.PLANNED,
            Transition(TicketState.READY, "planner-1", "planner", ticket.evidence[0], True),
            frozenset(),
            "worker-1",
            now,
        )
        checkpoints = Checkpoints(database)
        checkpoints.save(request.execution_id, "ready", 0, state.value)
        checkpoints.close()
        memory.close()
        # Reopen both adapters, modeling process-local state loss.
        resumed = Checkpoints(database)
        restored_memory = KnowledgeStore(database)
        checkpoint = resumed.load(request.execution_id)
        restored = restored_memory.get_context_snapshot(ticket.scope, now)
        resumed.close()
        restored_memory.close()
        return {
            "mode": "synthetic; no external calls",
            "worker_outcome": result.outcome,
            "checkpoint": checkpoint,
            "knowledge_version": restored.version,
            "restored_findings": len(restored.findings),
            "deployed": False,
        }


if __name__ == "__main__":
    print(json.dumps(run_demo(), indent=2))

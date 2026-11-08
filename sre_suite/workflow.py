"""Pure ticket transition guards plus a local optimistic checkpoint adapter."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .contracts import Evidence, JiraExecutionTicket, Scope, nonempty
from .serialization import decode_scope, encode_scope
from .storage import prepare_schema


class TicketState(StrEnum):
    PLANNED = "planned"
    READY = "ready"
    IMPLEMENTING = "implementing"
    IN_REVIEW = "in_review"
    QA_READY = "qa_ready"
    QA_PASSED = "qa_passed"
    SHIPPING = "shipping"
    VERIFYING = "verifying"
    ACCEPTED = "accepted"
    BLOCKED = "blocked"
    CANCELED = "canceled"


@dataclass(frozen=True)
class Transition:
    target: TicketState
    actor: str
    role: str
    evidence: Evidence
    successful: bool = False


def advance(
    ticket: JiraExecutionTicket,
    current: TicketState,
    transition: Transition,
    completed_dependencies: frozenset[str],
    implementer: str,
    now: datetime,
) -> TicketState:
    target = transition.target
    if current in {TicketState.ACCEPTED, TicketState.CANCELED}:
        raise ValueError("terminal workflow")
    if not transition.evidence.valid_for(ticket.scope, now):
        raise ValueError("transition evidence is stale or mismatched")
    nonempty(transition.actor, "actor")
    if target in {TicketState.BLOCKED, TicketState.CANCELED}:
        if transition.role != "orchestrator":
            raise ValueError("only orchestrator may block or cancel")
        return target
    if transition.successful is not True:
        raise ValueError("transition requires a successful independently validated result")
    edges = {
        TicketState.PLANNED: (TicketState.READY, "planner"),
        TicketState.READY: (TicketState.IMPLEMENTING, "implementer"),
        TicketState.IMPLEMENTING: (TicketState.IN_REVIEW, "implementer"),
        TicketState.IN_REVIEW: (TicketState.QA_READY, "reviewer"),
        TicketState.QA_READY: (TicketState.QA_PASSED, "qa"),
        TicketState.QA_PASSED: (TicketState.SHIPPING, "shipper"),
        TicketState.SHIPPING: (TicketState.VERIFYING, "shipper"),
        TicketState.VERIFYING: (TicketState.ACCEPTED, "verifier"),
    }
    if edges.get(current) != (target, transition.role):
        raise ValueError("invalid transition or actor role")
    if target in {TicketState.READY, TicketState.IMPLEMENTING} and not set(
        ticket.dependencies
    ).issubset(completed_dependencies):
        raise ValueError("dependencies not accepted")
    if transition.role == "implementer" and transition.actor != implementer:
        raise ValueError("wrong implementation worker")
    if transition.role in {"reviewer", "qa", "verifier"} and transition.actor == implementer:
        raise ValueError("implementation cannot approve, QA, or certify itself")
    return target


class Checkpoints:
    """Single-machine CAS and idempotency exercise; not a durable orchestrator."""

    def __init__(self, path: str, scope: Scope) -> None:
        self._db = sqlite3.connect(path)
        self._scope = scope
        try:
            prepare_schema(
                self._db,
                "checkpoints",
                (
                    "CREATE TABLE IF NOT EXISTS checkpoints (run_id TEXT PRIMARY KEY, "
                    "version INTEGER NOT NULL, payload TEXT NOT NULL, scope TEXT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS operations (run_id TEXT, operation_id TEXT, "
                    "payload TEXT NOT NULL, expected_version INTEGER NOT NULL, "
                    "result_version INTEGER NOT NULL, PRIMARY KEY(run_id, operation_id))",
                ),
            )
        except Exception:
            self._db.close()
            raise

    def close(self) -> None:
        self._db.close()

    def load(self, run_id: str) -> tuple[int, str] | None:
        row = self._db.execute(
            "SELECT version, payload, scope FROM checkpoints WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is not None and decode_scope(row[2]) != self._scope:
            raise ValueError("checkpoint belongs to another scope or generation")
        return None if row is None else (int(row[0]), str(row[1]))

    def save(self, run_id: str, operation_id: str, expected_version: int, payload: str) -> int:
        for value in (run_id, operation_id, payload):
            nonempty(value, "checkpoint field")
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            current = self.load(run_id)
            old = self._db.execute(
                "SELECT payload, expected_version, result_version FROM operations "
                "WHERE run_id = ? AND operation_id = ?",
                (run_id, operation_id),
            ).fetchone()
            if old:
                if old[0] != payload or old[1] != expected_version:
                    raise ValueError("idempotency key reused with different input")
                return int(old[2])
            version = current[0] if current else 0
            if version != expected_version:
                raise ValueError("checkpoint version conflict")
            next_version = version + 1
            self._db.execute(
                "INSERT INTO checkpoints VALUES (?, ?, ?, ?) ON CONFLICT(run_id) DO UPDATE "
                "SET version = excluded.version, payload = excluded.payload",
                (run_id, next_version, payload, encode_scope(self._scope)),
            )
            self._db.execute(
                "INSERT INTO operations VALUES (?, ?, ?, ?, ?)",
                (run_id, operation_id, payload, expected_version, next_version),
            )
            return next_version

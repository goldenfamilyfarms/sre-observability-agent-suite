"""Transactional shared records and append-only audit/outbox on SQLite or PostgreSQL."""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from typing import Any

from ..api_storage import sqlite_backend
from ..contracts import Scope
from ..database import Database
from ..execution.wire import canonical, digest
from ..serialization import encode_scope
from .contracts import identifier, wire

STATEMENTS = (
    "CREATE TABLE IF NOT EXISTS control_environments (id TEXT PRIMARY KEY, "
    "payload TEXT NOT NULL, closed INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS control_records (id TEXT PRIMARY KEY, scope TEXT NOT NULL, "
    "kind TEXT NOT NULL, payload TEXT NOT NULL, version BIGINT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS control_events (sequence BIGINT PRIMARY KEY, scope TEXT NOT NULL, "
    "kind TEXT NOT NULL, subject TEXT NOT NULL, payload TEXT NOT NULL, at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS control_operations (id TEXT PRIMARY KEY, scope TEXT NOT NULL, "
    "request_sha TEXT NOT NULL, payload TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS control_events_scope ON control_events(scope, sequence)",
)


def prepare_sqlite(path: str) -> Callable[[], Database]:
    backend = sqlite_backend(path)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        for statement in STATEMENTS:
            db.execute(statement)
    return backend


class ControlStore:
    def __init__(self, db: Database, scope: Scope) -> None:
        self.db, self.scope = db, scope
        self.encoded = encode_scope(scope)

    def key(self, kind: str, identity: str) -> str:
        return digest([self.encoded, identifier(kind), identifier(identity)])

    def get(self, kind: str, identity: str) -> tuple[int, dict[str, Any]] | None:
        import json

        row = self.db.execute(
            "SELECT version, payload, scope, kind FROM control_records WHERE id=?",
            (self.key(kind, identity),),
        ).fetchone()
        if row is None:
            return None
        if row[2] != self.encoded or row[3] != kind:
            raise ValueError("record scope mismatch")
        return int(row[0]), wire(json.loads(row[1]))

    def put(self, kind: str, identity: str, expected: int, payload: dict[str, Any]) -> int:
        current = self.get(kind, identity)
        if (current[0] if current else 0) != expected:
            raise ValueError("record version changed")
        if len(canonical(payload).encode()) > 500000:
            raise ValueError("record exceeds budget")
        version = expected + 1
        self.db.execute(
            "INSERT INTO control_records VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE "
            "SET payload=excluded.payload, version=excluded.version",
            (self.key(kind, identity), self.encoded, kind, canonical(payload), version),
        )
        return version

    def event(self, kind: str, subject: str, payload: dict[str, Any], now: datetime) -> int:
        row = self.db.execute("SELECT COALESCE(MAX(sequence), 0) FROM control_events").fetchone()
        assert row is not None
        sequence = int(row[0]) + 1  # serialized by the database's write transaction
        self.db.execute(
            "INSERT INTO control_events VALUES (?, ?, ?, ?, ?, ?)",
            (
                sequence,
                self.encoded,
                identifier(kind),
                identifier(subject),
                canonical(payload),
                now.isoformat(),
            ),
        )
        return sequence

    def events(self, after: int, limit: int = 100) -> list[dict[str, Any]]:
        import json

        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid event cursor")
        rows = self.db.execute(
            "SELECT sequence, kind, subject, payload, at FROM control_events "
            "WHERE scope=? AND sequence>? ORDER BY sequence LIMIT ?",
            (self.encoded, after, limit),
        ).fetchall()
        return [
            dict(sequence=int(r[0]), kind=r[1], subject=r[2], payload=json.loads(r[3]), at=r[4])
            for r in rows
        ]

    def once(
        self, subject: str, operation_id: str, request: object, apply: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
        import json

        key = self.key("operation", digest([subject, identifier(operation_id)]))
        checksum = digest(request)
        row = self.db.execute(
            "SELECT request_sha, payload FROM control_operations WHERE id=?", (key,)
        ).fetchone()
        if row:
            if row[0] != checksum:
                raise ValueError("operation id reused with changed input")
            return wire(json.loads(row[1]))
        result = apply()
        self.db.execute(
            "INSERT INTO control_operations VALUES (?, ?, ?, ?)",
            (key, self.encoded, checksum, canonical(result)),
        )
        return result

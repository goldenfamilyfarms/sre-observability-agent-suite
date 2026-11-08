"""Bounded local evidence storage with byte-level digest and scope verification."""

import hashlib
import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from typing import Protocol

from .contracts import Evidence, Scope
from .database import Database, SQLiteDatabase
from .storage import prepare_schema


class EvidenceVerifier(Protocol):
    def verify(self, evidence: Evidence, scope: Scope, now: datetime) -> bool: ...


def metadata(evidence: Evidence) -> str:
    return json.dumps(
        {"schema_version": 2, "evidence": asdict(evidence)}, default=str, sort_keys=True
    )


class EvidenceStore:
    """Trusted local adapter; content integrity is not producer authentication."""

    MAX_BYTES = 1_048_576

    def __init__(self, path: str) -> None:
        connection = sqlite3.connect(path)
        try:
            prepare_schema(
                connection,
                "evidence",
                (
                    "CREATE TABLE IF NOT EXISTS evidence_objects "
                    "(id TEXT PRIMARY KEY, metadata TEXT NOT NULL, content BLOB NOT NULL)",
                ),
            )
        except Exception:
            connection.close()
            raise
        self._db: Database = SQLiteDatabase(connection)

    def close(self) -> None:
        self._db.close()

    def put(self, evidence: Evidence, content: bytes, now: datetime) -> None:
        if not isinstance(content, bytes) or len(content) > self.MAX_BYTES:
            raise ValueError("evidence must be bytes within the 1 MiB limit")
        if not evidence.valid_for(evidence.scope, now):
            raise ValueError("evidence is expired or future-dated")
        if hashlib.sha256(content).hexdigest() != evidence.sha256:
            raise ValueError("evidence content digest mismatch")
        encoded = metadata(evidence)
        with self._db.transaction(write=True):
            old = self._db.execute(
                "SELECT metadata, content FROM evidence_objects WHERE id=?", (evidence.id,)
            ).fetchone()
            if old:
                if old != (encoded, content):
                    raise ValueError("evidence ID reused with different metadata or bytes")
                return
            self._db.execute(
                "INSERT INTO evidence_objects VALUES (?, ?, ?)", (evidence.id, encoded, content)
            )

    def verify(self, evidence: Evidence, scope: Scope, now: datetime) -> bool:
        if not evidence.valid_for(scope, now):
            return False
        row = self._db.execute(
            "SELECT metadata, content FROM evidence_objects WHERE id=?", (evidence.id,)
        ).fetchone()
        return bool(
            row
            and row[0] == metadata(evidence)
            and isinstance(row[1], bytes)
            and len(row[1]) <= self.MAX_BYTES
            and hashlib.sha256(row[1]).hexdigest() == evidence.sha256
        )

    def read(self, evidence: Evidence, scope: Scope, now: datetime) -> bytes:
        """Read only bytes matching the complete supplied metadata, scope and digest."""
        with self._db.transaction(write=False):
            if not self.verify(evidence, scope, now):
                raise ValueError("evidence unavailable")
            row = self._db.execute(
                "SELECT content FROM evidence_objects WHERE id=?", (evidence.id,)
            ).fetchone()
            if row is None or not isinstance(row[0], bytes):
                raise ValueError("evidence unavailable")
            return row[0]

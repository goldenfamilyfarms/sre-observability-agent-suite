"""SQLite reference adapter for evidence-backed facts and graph edges.

Local trusted-process prototype. Authentication, evidence fetching, and distributed
delivery belong to future adapters; evidence references are not cryptographic proof.
"""

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum

from .contracts import Environment, Evidence, Scope, aware, nonempty


class MemoryKind(StrEnum):
    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    PROCEDURAL = "procedural"
    WORKING = "working"
    GRAPH = "graph"


class FindingState(StrEnum):
    PROPOSED = "proposed"
    VERIFIED = "verified"
    DISPUTED = "disputed"


@dataclass(frozen=True)
class Finding:
    id: str
    scope: Scope
    kind: MemoryKind
    subject: str
    predicate: str
    value: str
    evidence: Evidence

    def __post_init__(self) -> None:
        for name in ("id", "subject", "predicate", "value"):
            nonempty(getattr(self, name), name)
        if not isinstance(self.kind, MemoryKind) or self.evidence.scope != self.scope:
            raise ValueError("invalid finding kind or evidence scope")


@dataclass(frozen=True)
class ContextSnapshot:
    version: int
    scope: Scope
    findings: tuple[Finding, ...]


def _encode(finding: Finding) -> str:
    return json.dumps(asdict(finding), default=str, sort_keys=True)


def _decode(payload: str) -> Finding:
    data = json.loads(payload)
    scope = Scope(**{**data["scope"], "environment": Environment(data["scope"]["environment"])})
    raw = data["evidence"]
    evidence = Evidence(
        id=raw["id"],
        scope=scope,
        uri=raw["uri"],
        sha256=raw["sha256"],
        observed_at=datetime.fromisoformat(raw["observed_at"]),
        expires_at=datetime.fromisoformat(raw["expires_at"]),
        producer=raw["producer"],
    )
    return Finding(
        id=data["id"],
        scope=scope,
        kind=MemoryKind(data["kind"]),
        subject=data["subject"],
        predicate=data["predicate"],
        value=data["value"],
        evidence=evidence,
    )


class KnowledgeStore:
    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path)
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (id INTEGER PRIMARY KEY, version INTEGER NOT NULL);
            INSERT OR IGNORE INTO metadata VALUES (1, 0);
            CREATE TABLE IF NOT EXISTS findings (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                version INTEGER PRIMARY KEY, finding_id TEXT NOT NULL, state TEXT NOT NULL
            );
        """)

    def close(self) -> None:
        self._db.close()

    @property
    def version(self) -> int:
        return int(self._db.execute("SELECT version FROM metadata WHERE id = 1").fetchone()[0])

    def propose_finding(self, finding: Finding) -> None:
        payload = _encode(finding)
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            existing = self._db.execute(
                "SELECT payload FROM findings WHERE id = ?", (finding.id,)
            ).fetchone()
            if existing:
                if existing[0] != payload:
                    raise ValueError("finding id reused with different payload")
                return
            self._db.execute(
                "INSERT INTO findings VALUES (?, ?, ?)",
                (finding.id, payload, FindingState.PROPOSED),
            )

    def reconcile_finding(
        self, finding_id: str, expected_version: int, now: datetime
    ) -> FindingState:
        aware(now)
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            if self.version != expected_version:
                raise ValueError("knowledge version changed; rebuild context")
            row = self._db.execute(
                "SELECT payload, state FROM findings WHERE id = ?", (finding_id,)
            ).fetchone()
            if row is None:
                raise ValueError("unknown finding")
            finding = _decode(row[0])
            state = FindingState(row[1])
            if state is not FindingState.PROPOSED:
                return state
            if not finding.evidence.valid_for(finding.scope, now):
                raise ValueError("stale or future evidence")
            verified = self._db.execute(
                "SELECT payload FROM findings WHERE state = ?", (FindingState.VERIFIED,)
            ).fetchall()
            conflict = any(
                old.scope == finding.scope
                and old.kind == finding.kind
                and old.subject == finding.subject
                and old.predicate == finding.predicate
                and old.value != finding.value
                for old in (_decode(item[0]) for item in verified)
            )
            state = FindingState.DISPUTED if conflict else FindingState.VERIFIED
            self._db.execute("UPDATE findings SET state = ? WHERE id = ?", (state, finding_id))
            self._db.execute("UPDATE metadata SET version = version + 1 WHERE id = 1")
            self._db.execute(
                "INSERT INTO events VALUES (?, ?, ?)", (self.version, finding_id, state)
            )
            return state

    def get_context_snapshot(self, scope: Scope, now: datetime) -> ContextSnapshot:
        aware(now)
        # One read transaction keeps the version and facts consistent across connections.
        with self._db:
            self._db.execute("BEGIN")
            version = self.version
            rows = self._db.execute(
                "SELECT payload FROM findings WHERE state = ? ORDER BY id",
                (FindingState.VERIFIED,),
            ).fetchall()
            findings = tuple(
                finding
                for finding in (_decode(row[0]) for row in rows)
                if finding.scope == scope and finding.evidence.valid_for(scope, now)
            )
            return ContextSnapshot(version, scope, findings)

    def get_dependency_graph(self, scope: Scope, now: datetime) -> tuple[Finding, ...]:
        return tuple(
            finding
            for finding in self.get_context_snapshot(scope, now).findings
            if finding.kind is MemoryKind.GRAPH
        )

    def events(self) -> tuple[tuple[int, str, str], ...]:
        return tuple(
            (int(row[0]), str(row[1]), str(row[2]))
            for row in self._db.execute("SELECT * FROM events ORDER BY version")
        )

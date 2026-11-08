"""Request-local repositories sharing one transaction and preview generation fence."""

import sqlite3
from collections.abc import Callable

from .database import Database, SQLiteDatabase
from .evidence import EvidenceStore
from .memory import KnowledgeStore
from .previews import PreviewRegistry


class _Evidence(EvidenceStore):
    def __init__(self, database: Database) -> None:
        self._db = database


class _Knowledge(KnowledgeStore):
    def __init__(self, database: Database, evidence: EvidenceStore) -> None:
        self._db = database
        self._evidence = evidence
        self._single_valued_predicates = frozenset()


class _Previews(PreviewRegistry):
    def __init__(self, database: Database) -> None:
        self._db = database


class ApiRepositories:
    """Borrow one database; the request owner closes it, not individual repositories."""

    def __init__(self, database: Database) -> None:
        self.evidence = _Evidence(database)
        self.knowledge = _Knowledge(database, self.evidence)
        self.previews = _Previews(database)


def sqlite_backend(path: str) -> Callable[[], Database]:
    """Explicit local startup preparation. PostgreSQL never migrates implicitly."""
    evidence = EvidenceStore(path)
    try:
        KnowledgeStore(path, evidence).close()
        PreviewRegistry(path).close()
    finally:
        evidence.close()
    return lambda: SQLiteDatabase(sqlite3.connect(path))


def postgres_backend(dsn: str, schema: str = "sre_suite") -> Callable[[], Database]:
    # Keep the default installation free of the optional PostgreSQL dependency.
    from .postgres import PostgresDatabase

    return lambda: PostgresDatabase(dsn, schema)

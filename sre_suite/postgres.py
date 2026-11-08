"""Optional PostgreSQL persistence; explicit migrations and shared domain guards.

Use one adapter per thread, close it after use, and supply a dedicated schema.
No connection pool, API authentication, cloud provisioning or deployment here.
"""

import hashlib
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

import psycopg
from psycopg import sql

from .contracts import Scope
from .database import Rows
from .evidence import EvidenceStore, EvidenceVerifier
from .memory import KnowledgeStore
from .previews import PreviewRegistry
from .workflow import Checkpoints


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    source: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.source.encode()).hexdigest()


def migrations() -> tuple[Migration, ...]:
    directory = files("sre_suite").joinpath("migrations")
    return tuple(
        Migration(int(name[:3]), name, directory.joinpath(name).read_text(encoding="utf-8"))
        for name in ("001_foundation.sql", "002_scope_guards.sql")
    )


def _schema_name(schema: str) -> None:
    if not re.fullmatch(r"sre_[a-z0-9_]{1,58}", schema):
        raise ValueError("use a dedicated lowercase sre_ schema, at most 62 characters")


def _lock_key(schema: str) -> int:
    return int.from_bytes(hashlib.sha256(f"sre-suite:{schema}".encode()).digest()[:8], signed=True)


def _connect(dsn: str, schema: str) -> psycopg.Connection[tuple[Any, ...]]:
    _schema_name(schema)
    db = psycopg.connect(dsn, autocommit=True, connect_timeout=5)
    try:
        # Explicit timeouts bound lock waits and abandoned transactions. Credentials
        # and transport settings come from the caller; never log the connection URI.
        db.execute("SELECT set_config('search_path', %s, false)", (f"{schema},pg_catalog",))
        db.execute("SET lock_timeout = '5s'")
        db.execute("SET statement_timeout = '15s'")
        db.execute("SET idle_in_transaction_session_timeout = '15s'")
    except Exception:
        db.close()
        raise
    return db


def _history(db: psycopg.Connection[tuple[Any, ...]], available: tuple[Migration, ...]) -> int:
    rows = db.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    expected = [(m.version, m.name, m.checksum) for m in available]
    if rows != expected[: len(rows)] or len(rows) > len(expected):
        raise ValueError("unknown, noncontiguous, or modified PostgreSQL migration history")
    return len(rows)


def migrate(dsn: str, schema: str = "sre_suite") -> tuple[int, ...]:
    """Explicitly apply packaged upgrades atomically. Never adopt untracked tables.

    No downgrade or SQLite import is performed. Back up and stop application
    writers before upgrades. SQL and its checksummed history commit together.
    """
    available = migrations()
    applied: list[int] = []
    with _connect(dsn, schema) as db, db.transaction():
        db.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        db.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_key(schema),))
        exists = db.execute("SELECT to_regnamespace(%s)", (schema,)).fetchone()
        if exists and exists[0] is not None:
            tracked = db.execute(
                "SELECT to_regclass(%s)", (f"{schema}.schema_migrations",)
            ).fetchone()
            if not tracked or tracked[0] is None:
                raise ValueError("existing schema is unmanaged; choose a fresh dedicated schema")
        else:
            db.execute(sql.SQL("CREATE SCHEMA {} ").format(sql.Identifier(schema)))
            db.execute(
                "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                "checksum TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
        count = _history(db, available)
        for migration in available[count:]:
            db.execute(sql.SQL(migration.source))
            db.execute(
                "INSERT INTO schema_migrations(version, name, checksum) VALUES (%s, %s, %s)",
                (migration.version, migration.name, migration.checksum),
            )
            applied.append(migration.version)
    return tuple(applied)


class PostgresDatabase:
    """Fixed-query adapter; serialize pilot writes per schema, snapshot reads.

    The coarse transaction lock also fences absent-row inserts and migrations.
    This deliberately prioritizes correctness over write throughput. READ COMMITTED
    writers read state only after acquiring the lock; snapshot readers use
    REPEATABLE READ so version and findings cannot come from different commits.
    """

    def __init__(self, dsn: str, schema: str) -> None:
        self.connection = _connect(dsn, schema)
        self._lock = _lock_key(schema)
        try:
            with self.transaction(write=False):
                if _history(self.connection, migrations()) != len(migrations()):
                    raise ValueError("PostgreSQL migrations are pending; explicitly migrate first")
        except Exception:
            self.connection.close()
            raise

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> Rows:
        # Only static repository queries use this port. No interpolation of values.
        return self.connection.execute(sql.SQL(query.replace("?", "%s")), parameters)

    @contextmanager
    def transaction(self, *, write: bool) -> Iterator[None]:
        with self.connection.transaction():
            if write:
                self.connection.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                self.connection.execute("SELECT pg_advisory_xact_lock(%s)", (self._lock,))
            else:
                self.connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            yield

    def close(self) -> None:
        self.connection.close()


class PostgresEvidenceStore(EvidenceStore):
    def __init__(self, dsn: str, schema: str = "sre_suite") -> None:
        self._db = PostgresDatabase(dsn, schema)


class PostgresKnowledgeStore(KnowledgeStore):
    def __init__(
        self,
        dsn: str,
        evidence: EvidenceVerifier,
        single_valued_predicates: frozenset[str] = frozenset(),
        *,
        schema: str = "sre_suite",
    ) -> None:
        self._db = PostgresDatabase(dsn, schema)
        self._evidence = evidence
        self._single_valued_predicates = single_valued_predicates


class PostgresPreviewRegistry(PreviewRegistry):
    def __init__(self, dsn: str, schema: str = "sre_suite") -> None:
        self._db = PostgresDatabase(dsn, schema)


class PostgresCheckpoints(Checkpoints):
    def __init__(self, dsn: str, scope: Scope, schema: str = "sre_suite") -> None:
        self._db = PostgresDatabase(dsn, schema)
        self._scope = scope

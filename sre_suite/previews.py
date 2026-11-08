"""Local preview identity/generation registry. No resource provisioning or authorization."""

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime

from .contracts import Environment, Scope, aware, nonempty
from .database import Database, SQLiteDatabase
from .serialization import scope_from_dict
from .storage import prepare_schema


def preview_environment_id(repository_id: int, pr_number: int) -> str:
    for value in (repository_id, pr_number):
        if type(value) is not int or not 1 <= value <= 2**63 - 1:
            raise ValueError("repository and PR IDs must be positive signed 64-bit integers")
    return f"preview-r{repository_id}-pr{pr_number}"


@dataclass(frozen=True)
class PreviewRequest:
    repository_id: int
    pr_number: int
    scope: Scope
    requested_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if self.scope.environment_id != preview_environment_id(self.repository_id, self.pr_number):
            raise ValueError("preview scope must match repository and PR identity")
        if self.scope.environment is Environment.PRODUCTION:
            raise ValueError("production is not a preview destination")
        aware(self.requested_at)
        aware(self.expires_at)
        if self.expires_at <= self.requested_at:
            raise ValueError("preview requires a positive validity window")


def _encode(request: PreviewRequest) -> str:
    return json.dumps({"schema_version": 2, **asdict(request)}, default=str, sort_keys=True)


def _decode(payload: str) -> PreviewRequest:
    raw = json.loads(payload)
    if raw.get("schema_version") != 2:
        raise ValueError("unsupported preview schema version")
    return PreviewRequest(
        raw["repository_id"],
        raw["pr_number"],
        scope_from_dict(raw["scope"]),
        datetime.fromisoformat(raw["requested_at"]),
        datetime.fromisoformat(raw["expires_at"]),
    )


class PreviewRegistry:
    """Single-service pilot registry; a trusted controller supplies time and requests."""

    def __init__(self, path: str) -> None:
        connection = sqlite3.connect(path)
        try:
            prepare_schema(
                connection,
                "previews",
                (
                    "CREATE TABLE IF NOT EXISTS previews (environment_id TEXT PRIMARY KEY, "
                    "payload TEXT NOT NULL, closed INTEGER NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS preview_events (sequence INTEGER PRIMARY KEY, "
                    "environment_id TEXT NOT NULL, generation INTEGER NOT NULL, "
                    "kind TEXT NOT NULL, at TEXT NOT NULL, detail TEXT NOT NULL)",
                ),
            )
        except Exception:
            connection.close()
            raise
        self._db: Database = SQLiteDatabase(connection)

    def close(self) -> None:
        self._db.close()

    def start(self, request: PreviewRequest, expected_generation: int, now: datetime) -> None:
        aware(now)
        if (
            type(expected_generation) is not int
            or expected_generation != request.scope.generation - 1
        ):
            raise ValueError("new generation must be exactly the expected generation plus one")
        if not request.requested_at <= now < request.expires_at:
            raise ValueError("preview request is expired or future-dated")
        payload = _encode(request)
        with self._db.transaction(write=True):
            old = self._db.execute(
                "SELECT payload, closed FROM previews WHERE environment_id=?",
                (request.scope.environment_id,),
            ).fetchone()
            if old and old[0] == payload and not old[1]:
                return  # Retry of the current generation only, never an older one.
            previous = _decode(old[0]) if old else None
            if (previous.scope.generation if previous else 0) != expected_generation:
                raise ValueError("stale preview generation")
            if previous and (
                previous.scope.service != request.scope.service
                or previous.scope.environment != request.scope.environment
            ):
                raise ValueError("preview service and environment class cannot change")
            self._db.execute(
                "INSERT INTO previews VALUES (?, ?, 0) ON CONFLICT(environment_id) DO UPDATE "
                "SET payload=excluded.payload, closed=0",
                (request.scope.environment_id, payload),
            )
            self._event(request.scope, "requested", now, payload)

    def require_current(self, scope: Scope, now: datetime) -> PreviewRequest:
        aware(now)
        row = self._db.execute(
            "SELECT payload, closed FROM previews WHERE environment_id=?", (scope.environment_id,)
        ).fetchone()
        if not row or row[1]:
            raise ValueError("preview is missing or closed")
        request = _decode(row[0])
        if request.scope != scope or not request.requested_at <= now < request.expires_at:
            raise ValueError("preview scope is superseded, expired, or future-dated")
        return request

    def terminate(self, scope: Scope, reason: str, now: datetime) -> None:
        """Tombstone even an expired generation. This does not claim resource deletion."""
        aware(now)
        nonempty(reason, "termination reason")
        with self._db.transaction(write=True):
            row = self._db.execute(
                "SELECT payload, closed FROM previews WHERE environment_id=?",
                (scope.environment_id,),
            ).fetchone()
            if not row or _decode(row[0]).scope != scope:
                raise ValueError("cannot close a missing or different preview generation")
            if row[1]:
                return
            self._db.execute(
                "UPDATE previews SET closed=1 WHERE environment_id=?", (scope.environment_id,)
            )
            self._event(scope, "closed", now, reason)

    def _event(self, scope: Scope, kind: str, now: datetime, detail: str) -> None:
        self._db.execute(
            "INSERT INTO preview_events(environment_id, generation, kind, at, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (scope.environment_id, scope.generation, kind, now.isoformat(), detail),
        )

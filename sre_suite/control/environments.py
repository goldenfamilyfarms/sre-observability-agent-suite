"""Operator-registered production observation scopes with monotonic generation fencing."""

import json
from datetime import datetime, timedelta
from typing import Any

from ..api_wire import fields
from ..contracts import Environment, Scope
from ..database import Database
from ..execution.wire import canonical
from ..serialization import scope_from_dict
from ..telemetry.contracts import integer
from .contracts import timestamp, wire


class Environments:
    def __init__(self, db: Database) -> None:
        self.db = db

    def current(self, scope: Scope, now: datetime) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT payload, closed FROM control_environments WHERE id=?", (scope.environment_id,)
        ).fetchone()
        if not row or row[1]:
            raise ValueError("environment not registered or closed")
        value = wire(json.loads(row[0]))
        if scope_from_dict(value["scope"]) != scope or not timestamp(
            value["requested_at"]
        ) <= now < timestamp(value["expires_at"]):
            raise ValueError("environment scope is stale")
        return value

    def register(self, scope: Scope, value: dict[str, Any], now: datetime) -> None:
        raw = fields(value, {"scope", "requested_at", "expires_at", "expected_generation"})
        if scope.environment != Environment.PRODUCTION or scope_from_dict(raw["scope"]) != scope:
            raise ValueError("use preview registration for non-production")
        expected = integer(raw["expected_generation"], 0, 2**63 - 1)
        if (
            expected != scope.generation - 1
            or not timestamp(raw["requested_at"]) <= now < timestamp(raw["expires_at"])
            or timestamp(raw["expires_at"]) > now + timedelta(days=30)
        ):
            raise ValueError("invalid environment generation or validity")
        prior = self.db.execute(
            "SELECT payload, closed FROM control_environments WHERE id=?", (scope.environment_id,)
        ).fetchone()
        if prior and prior[0] == canonical(value) and not prior[1]:
            return
        previous = scope_from_dict(json.loads(prior[0])["scope"]) if prior else None
        if (
            (previous.generation if previous else 0) != expected
            or previous
            and (previous.service != scope.service or previous.environment != scope.environment)
        ):
            raise ValueError("environment replacement conflicts")
        self.db.execute(
            "INSERT INTO control_environments VALUES (?, ?, 0) ON CONFLICT(id) "
            "DO UPDATE SET payload=excluded.payload, closed=0",
            (scope.environment_id, canonical(value)),
        )

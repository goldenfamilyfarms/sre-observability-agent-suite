"""Finite telemetry schedules; deterministic identity, windows and authority binding."""

import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import cast

from ..api_wire import envelope, text
from ..contracts import aware
from ..execution.wire import canonical, digest
from .contracts import QueryPolicy, QueryRequest, identifier, integer


def policy_digest(policy: QueryPolicy) -> str:
    raw = asdict(policy)
    raw["profiles"] = [
        {**asdict(p), "scope_labels": dict(p.scope_labels)}
        for p in sorted(policy.profiles, key=lambda p: p.id)
    ]
    for profile in raw["profiles"]:
        labels = profile.pop("binding_labels")
        if labels:
            profile["binding_labels"] = dict(labels)
    return digest(raw)


@dataclass(frozen=True)
class TelemetrySchedule:
    schedule_id: str
    policy_sha256: str
    request: QueryRequest
    first_run_at: datetime
    interval_seconds: int
    occurrences: int
    max_lateness_seconds: int

    def __post_init__(self) -> None:
        identifier(self.schedule_id)
        if not re.fullmatch(r"[0-9a-f]{64}", self.policy_sha256):
            raise ValueError("schedule requires the reviewed policy fingerprint")
        aware(self.first_run_at)
        integer(self.interval_seconds, 1, 3600)
        integer(self.occurrences, 1, 24)
        integer(self.max_lateness_seconds, 1, 300)
        if not timedelta(0) <= self.first_run_at - self.request.end <= timedelta(minutes=5):
            raise ValueError("collection must follow its window by at most five minutes")

    @classmethod
    def parse(cls, value: object) -> "TelemetrySchedule":
        row = envelope(
            value,
            {
                "schedule_id",
                "policy_sha256",
                "request",
                "first_run_at",
                "interval_seconds",
                "occurrences",
                "max_lateness_seconds",
            },
        )
        return cls(
            text(row["schedule_id"]),
            text(row["policy_sha256"], 64),
            QueryRequest.parse(row["request"]),
            datetime.fromisoformat(text(row["first_run_at"])),
            integer(row["interval_seconds"], 1, 3600),
            integer(row["occurrences"], 1, 24),
            integer(row["max_lateness_seconds"], 1, 300),
        )

    def wire(self) -> dict[str, object]:
        return cast(
            dict[str, object],
            json.loads(
                canonical(
                    {
                        "schema_version": 2,
                        **asdict(self),
                        "request": {"schema_version": 2, **asdict(self.request)},
                    }
                )
            ),
        )

    @property
    def workflow_id(self) -> str:
        # Reusing a logical schedule name with changed settings cannot start a second run.
        return "sre-telemetry-" + digest(
            {"scope": asdict(self.request.scope), "schedule_id": self.schedule_id}
        )

    def authorize(self, policy: QueryPolicy) -> None:
        if policy_digest(policy) != self.policy_sha256:
            raise ValueError("worker policy changed since schedule review")
        policy.resolve(self.request)

    def due(self, slot: int) -> datetime:
        integer(slot, 0, self.occurrences - 1)
        return self.first_run_at + timedelta(seconds=slot * self.interval_seconds)

    def for_slot(self, slot: int) -> QueryRequest:
        offset = self.due(slot) - self.first_run_at
        return replace(
            self.request, start=self.request.start + offset, end=self.request.end + offset
        )

    def startable(self, now: datetime) -> None:
        aware(now)
        if (
            not now - timedelta(seconds=self.max_lateness_seconds)
            <= self.first_run_at
            <= now + timedelta(hours=1)
        ):
            raise ValueError("first collection must be current or within one hour")

    def runnable(self, slot: int, now: datetime) -> QueryRequest:
        if (
            not self.due(slot)
            <= now
            <= self.due(slot) + timedelta(seconds=self.max_lateness_seconds)
        ):
            raise ValueError("collection is early or exceeded its lateness budget")
        request = self.for_slot(slot)
        request.current(now)
        return request

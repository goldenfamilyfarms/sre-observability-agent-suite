"""Durable finite timers and serialized telemetry activities, with no workflow-side I/O."""

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, is_cancelled_exception

with workflow.unsafe.imports_passed_through():
    from .schedule_contracts import TelemetrySchedule


@workflow.defn
class TelemetryCollection:
    def __init__(self) -> None:
        self.state = "queued"

    @workflow.query
    def status(self) -> str:
        return self.state

    @workflow.run
    async def run(self, value: dict[str, Any]) -> dict[str, Any]:
        try:
            schedule = TelemetrySchedule.parse(value)
            schedule.startable(workflow.now())
        except (ValueError, TypeError):
            self.state = "blocked"
            return {"outcome": "blocked", "reason": "invalid_schedule", "collections": []}
        reports: list[dict[str, Any]] = []
        try:
            for slot in range(schedule.occurrences):
                self.state = "waiting"
                delay = (schedule.due(slot) - workflow.now()).total_seconds()
                if delay > 0:
                    await workflow.sleep(delay)
                try:
                    schedule.runnable(slot, workflow.now())
                except ValueError:
                    self.state = "blocked"
                    return {"outcome": "blocked", "reason": "missed_window", "collections": reports}
                self.state = "collecting"
                report = await workflow.execute_activity(
                    "collect_telemetry",
                    {"schedule": value, "slot": slot},
                    start_to_close_timeout=timedelta(seconds=90),
                    schedule_to_close_timeout=timedelta(seconds=180),
                    heartbeat_timeout=timedelta(seconds=5),
                    retry_policy=RetryPolicy(
                        initial_interval=timedelta(seconds=1),
                        maximum_interval=timedelta(seconds=5),
                        maximum_attempts=3,
                    ),
                    cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                )
                reports.append(report)
            self.state = "collected"
            return {"outcome": "collected", "collections": reports}
        except asyncio.CancelledError:
            self.state = "canceled"
            raise
        except ActivityError as error:
            if is_cancelled_exception(error):
                self.state = "canceled"
                raise asyncio.CancelledError from error
            self.state = "blocked"
            return {"outcome": "blocked", "reason": "collection_failed", "collections": reports}

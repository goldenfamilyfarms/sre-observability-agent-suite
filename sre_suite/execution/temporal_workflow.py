"""Replay-safe Temporal workflow; all network/process/filesystem work is in activities."""

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, is_cancelled_exception


@workflow.defn
class TicketExecution:
    def __init__(self) -> None:
        self.state = "queued"

    @workflow.query
    def status(self) -> str:
        return self.state

    @workflow.run
    async def run(self, spec: dict[str, Any]) -> dict[str, Any]:
        options: dict[str, Any] = dict(
            start_to_close_timeout=timedelta(minutes=35),
            schedule_to_close_timeout=timedelta(hours=2),
            heartbeat_timeout=timedelta(seconds=45),
            retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=3),
            cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        )
        results: list[dict[str, Any]] = []
        try:
            self.state = "preparing"
            reference = await workflow.execute_activity("prepare_ticket", spec, **options)
            for role in ("implement", "review", "test", "qa"):
                self.state = role
                result = await workflow.execute_activity(
                    "execute_stage",
                    {"reference": reference, "role": role, "results": results},
                    **options,
                )
                results.append(result)
                if result["verdict"] != "passed":
                    break
            self.state = "recording"
            report: dict[str, Any] = await workflow.execute_activity(
                "record_result", {"reference": reference, "results": results}, **options
            )
            self.state = str(report["outcome"])
            return report
        except asyncio.CancelledError:
            self.state = "canceled"
            raise
        except ActivityError as error:
            if is_cancelled_exception(error):
                self.state = "canceled"
                raise asyncio.CancelledError from error
            failed_stage = self.state
            self.state = "blocked"
            return {"outcome": "blocked", "failed_stage": failed_stage, "jira_reported": False}

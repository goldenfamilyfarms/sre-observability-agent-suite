"""Replay-safe Temporal workflow; all network/process/filesystem work is in activities."""

import asyncio
import json
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ActivityError, ChildWorkflowError, is_cancelled_exception


@workflow.defn
class TicketExecution:
    def __init__(self) -> None:
        self.state = "queued"
        self.plan: dict[str, Any] | None = None
        self.certificate_plan: dict[str, Any] | None = None
        self.fixture: dict[str, Any] | None = None

    @workflow.query
    def status(self) -> str:
        return self.state

    @workflow.query
    def telemetry_plan(self) -> dict[str, Any] | None:
        return self.plan

    @workflow.query
    def certification_plan(self) -> dict[str, Any] | None:
        return self.certificate_plan

    @workflow.signal
    def submit_certification_fixture(self, evidence: dict[str, Any]) -> None:
        # Metadata only; activities authenticate evidence. First submission is frozen.
        if (
            self.fixture is None
            and isinstance(evidence, dict)
            and len(json.dumps(evidence)) <= 8192
        ):
            self.fixture = evidence

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
            arguments: dict[str, Any] = {"reference": reference, "results": results}
            # Recorded prepare output keeps pre-integration histories on their original path.
            if (
                reference.get("telemetry_required")
                and len(results) == 4
                and all(result["verdict"] == "passed" for result in results)
            ):
                self.state = "preparing_telemetry"
                self.plan = await workflow.execute_activity(
                    "prepare_engineering_telemetry", arguments, **options
                )
                assert self.plan is not None
                if reference.get("certification_required"):
                    self.state = "preparing_certification"
                    self.certificate_plan = await workflow.execute_activity(
                        "prepare_engineering_certification", arguments, **options
                    )
                    arguments["certification_plan"] = self.certificate_plan
                self.state = "collecting_telemetry"
                reports = await asyncio.gather(
                    *(
                        workflow.execute_child_workflow(
                            "TelemetryCollection",
                            entry["schedule"],
                            id=entry["id"],
                            task_queue=self.plan["task_queue"],
                            execution_timeout=timedelta(hours=1),
                            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                            parent_close_policy=workflow.ParentClosePolicy.REQUEST_CANCEL,
                            cancellation_type=workflow.ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
                        )
                        for entry in self.plan["schedules"]
                    )
                )
                arguments["telemetry"] = {"plan": self.plan, "reports": reports}
                if reference.get("certification_required"):
                    assert self.certificate_plan is not None
                    receipt = {"verdict": "INCOMPLETE", "evidence": None}
                    request = None
                    # Scheduling hint only; activities verify actual evidence bytes.
                    observed = all(
                        r.get("outcome") == "collected"
                        and len(r.get("collections", [])) == 1
                        and r["collections"][0].get("status") == "observed"
                        for r in reports
                    )
                    if observed:
                        self.state = "waiting_certification_fixture"
                        try:
                            await workflow.wait_condition(
                                lambda: self.fixture is not None,
                                timeout=timedelta(
                                    seconds=self.certificate_plan["fixture_wait_seconds"]
                                ),
                            )
                        except TimeoutError:
                            pass
                        if self.fixture is not None:
                            arguments["certification_fixture"] = self.fixture
                            self.state = "binding_certification"
                            request = await workflow.execute_activity(
                                "bind_engineering_certification", arguments, **options
                            )
                            self.state = "certifying"
                            receipt = await workflow.execute_child_workflow(
                                "ServiceCertification",
                                request["input"],
                                id=request["id"],
                                task_queue=request["task_queue"],
                                execution_timeout=timedelta(minutes=15),
                                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                                parent_close_policy=workflow.ParentClosePolicy.REQUEST_CANCEL,
                                cancellation_type=workflow.ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
                            )
                    arguments["certification"] = {"request": request, "receipt": receipt}
            self.state = "recording"
            report: dict[str, Any] = await workflow.execute_activity(
                "record_result", arguments, **options
            )
            self.state = str(report["outcome"])
            return report
        except asyncio.CancelledError:
            self.state = "canceled"
            raise
        except (ActivityError, ChildWorkflowError) as error:
            if is_cancelled_exception(error):
                self.state = "canceled"
                raise asyncio.CancelledError from error
            failed_stage = self.state
            self.state = "blocked"
            return {"outcome": "blocked", "failed_stage": failed_stage, "jira_reported": False}

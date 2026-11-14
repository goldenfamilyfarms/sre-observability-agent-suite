"""Durable dependency dispatch, bounded corrections and explicit review checkpoints."""

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ActivityError, ChildWorkflowError, is_cancelled_exception

with workflow.unsafe.imports_passed_through():
    from .contracts import CHILDREN, SuitePlan, passed


@workflow.defn
class SuiteExecution:
    def __init__(self) -> None:
        self.state = "queued"
        self.completed: list[str] = []

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {"state": self.state, "completed": self.completed}

    @workflow.run
    async def run(self, raw: dict[str, Any]) -> dict[str, Any]:
        try:
            plan = SuitePlan.parse(raw)
            results: dict[str, Any] = {}
            options: dict[str, Any] = dict(
                start_to_close_timeout=timedelta(minutes=10),
                schedule_to_close_timeout=timedelta(minutes=25),
                heartbeat_timeout=timedelta(seconds=10),
                retry_policy=RetryPolicy(maximum_attempts=3),
                cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            )
            for node in plan.nodes:
                if not all(d in self.completed for d in node["depends_on"]):
                    raise ValueError("dependency not complete")
                self.state = node["id"]
                attempts = [node["request_sha256"], *node["corrections"]]
                previous = None
                for index, request in enumerate(attempts):
                    parameters = {
                        "plan": plan.identity,
                        "node": node["id"],
                        "request": request,
                        "previous": previous,
                        "attempt": index,
                    }
                    dispatch = await workflow.execute_activity(
                        "suite_prepare", parameters, **options
                    )
                    if node["kind"] in CHILDREN:
                        result = await workflow.execute_child_workflow(
                            CHILDREN[node["kind"]],
                            dispatch["input"],
                            id=dispatch["workflow_id"],
                            task_queue=node["task_queue"],
                            execution_timeout=timedelta(hours=6),
                            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                            parent_close_policy=workflow.ParentClosePolicy.REQUEST_CANCEL,
                            cancellation_type=workflow.ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
                        )
                    else:
                        result = await workflow.execute_activity(
                            "suite_execute", parameters, task_queue=node["task_queue"], **options
                        )
                    previous = await workflow.execute_activity(
                        "suite_record", {**parameters, "result": result}, **options
                    )
                    if passed(node["kind"], result):
                        break
                results[node["id"]] = previous
                if not passed(node["kind"], result):
                    self.state = "blocked"
                    return {
                        "outcome": "blocked",
                        "node": node["id"],
                        "results": results,
                        "correction_budget_exhausted": len(attempts) > 1,
                    }
                self.completed.append(node["id"])
                # A changed source must be rediscovered and approved. Never rewrite a scoped plan
                # in history or silently dispatch dependent work against yesterday's source.
                if node["checkpoint_after"] or node["kind"] in {"shipping", "steward", "epic"}:
                    self.state = "awaiting_review"
                    return {
                        "outcome": "awaiting_review",
                        "results": results,
                        "requires_new_plan": True,
                        "remaining": len(plan.nodes) - len(self.completed),
                    }
            self.state = "completed"
            return {"outcome": "completed", "results": results}
        except asyncio.CancelledError:
            self.state = "canceled"
            raise
        except (ActivityError, ChildWorkflowError) as error:
            if is_cancelled_exception(error):
                self.state = "canceled"
                raise asyncio.CancelledError from error
            self.state = "blocked"
            return {
                "outcome": "blocked",
                "reason": "agent_unavailable",
                "completed": self.completed,
            }
        except ValueError:
            self.state = "blocked"
            return {"outcome": "blocked", "reason": "invalid_plan", "completed": self.completed}

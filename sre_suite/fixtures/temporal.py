"""Fixture traffic during the window; assessment afterward on an independent worker queue."""

import asyncio
import re
import sys
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    CancelledError,
    is_cancelled_exception,
)

with workflow.unsafe.imports_passed_through():
    from ..api import evidence_id
    from ..api_wire import decode, evidence_from_wire, fields, text
    from ..demo_fixture.managed_contracts import ready_receipt, validate_export
    from ..execution.process import ExecutionBlocked, run_process
    from ..execution.temporal_activities import heartbeat
    from ..execution.wire import canonical
    from ..telemetry.schedule_config import private_path
    from .config import environment, load_policy
    from .contracts import FixtureRequest


class FixtureActivities:
    def __init__(self, policy: Path, journal: Path, role: str, env: Mapping[str, str]) -> None:
        self.policy_file, self.journal, self.role = policy, journal, role
        self.policy = load_policy(policy)
        private_path(journal, directory=True)
        self.env = environment(self.policy, role, env)

    def call(self, operation: str, value: dict[str, Any]) -> dict[str, Any]:
        try:
            heartbeat()
            if (operation == "assess") != (self.role == "assessor"):
                raise ValueError("wrong role")
            fields(value, {"request", "reference"})
            request = FixtureRequest.parse(value["request"])
            request.authorize(self.policy)
            result = run_process(
                [
                    sys.executable,
                    "-m",
                    "sre_suite.fixtures.child",
                    operation,
                    "--policy",
                    str(self.policy_file),
                    "--journal",
                    str(self.journal),
                ],
                self.journal,
                self.env,
                input_bytes=canonical(value).encode(),
                timeout=300,
                max_output=8192,
                heartbeat=heartbeat,
            )
            if result.code != 0:
                raise ApplicationError(
                    "fixture operation unavailable", non_retryable=result.code != 75
                )
            raw = decode(result.output)
            if operation == "execute":
                receipt = fields(raw, {"workflow_id", "artifact"})
                if receipt["workflow_id"] != request.identity or not re.fullmatch(
                    r"[0-9a-f]{64}", text(receipt["artifact"], 64)
                ):
                    raise ValueError("invalid fixture artifact")
            else:
                if operation == "assess":
                    receipt = fields(raw, {"outcome", "workflow_id", "evidence"})
                    if (
                        receipt["outcome"] != "assessed"
                        or receipt["workflow_id"] != request.identity
                    ):
                        raise ValueError("invalid assessment receipt")
                    ref = evidence_from_wire(receipt["evidence"])
                    producer, key = (
                        self.policy.verification.assessor,
                        request.identity + "-assessment",
                    )
                else:
                    ref = evidence_from_wire(raw)
                    producer, key = self.policy.runner, request.identity
                if (
                    ref.scope != request.scope
                    or ref.producer != producer
                    or ref.id != evidence_id(producer, request.scope, key)
                ):
                    raise ValueError("fixture evidence identity mismatch")
            heartbeat()
            if not isinstance(raw, dict):
                raise ValueError("invalid receipt")
            return raw
        except (ApplicationError, CancelledError):
            raise
        except ExecutionBlocked:
            raise ApplicationError(
                "fixture process deadline exceeded", non_retryable=True
            ) from None
        except Exception:
            raise ApplicationError("fixture operation blocked", non_retryable=True) from None

    @activity.defn(name="execute_fixture")
    def execute(self, value: dict[str, Any]) -> dict[str, Any]:
        return self.call("execute", value)

    @activity.defn(name="publish_fixture_run")
    def publish(self, value: dict[str, Any]) -> dict[str, Any]:
        return self.call("publish", value)

    @activity.defn(name="assess_fixture")
    def assess(self, value: dict[str, Any]) -> dict[str, Any]:
        return self.call("assess", value)


@workflow.defn
class FixtureAssessment:
    @workflow.run
    async def run(self, raw: dict[str, Any]) -> dict[str, Any]:
        try:
            request = FixtureRequest.parse(raw)
            if not request.start - timedelta(hours=1) <= workflow.now() < request.end:
                raise ValueError("fixture window is missed or too distant")
        except (ValueError, TypeError, KeyError):
            return {"outcome": "blocked", "reason": "invalid_fixture_window", "evidence": None}
        options: dict[str, Any] = dict(
            start_to_close_timeout=timedelta(seconds=330),
            schedule_to_close_timeout=timedelta(minutes=6),
            heartbeat_timeout=timedelta(seconds=5),
            retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=3),
            cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        )
        target = None
        try:
            if request.demo:
                demo = request.demo
                launch = request.start - timedelta(seconds=demo["startup_seconds"])
                if workflow.now() < launch:
                    await workflow.sleep(launch - workflow.now())
                target = workflow.start_activity(
                    "serve_demo_target",
                    raw,
                    task_queue=demo["task_queue"],
                    start_to_close_timeout=timedelta(seconds=500),
                    schedule_to_close_timeout=timedelta(seconds=550),
                    heartbeat_timeout=timedelta(seconds=5),
                    retry_policy=RetryPolicy(
                        initial_interval=timedelta(seconds=1), maximum_attempts=3
                    ),
                    cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                )
                ready = await workflow.execute_activity(
                    "ready_demo_target",
                    raw,
                    task_queue=demo["task_queue"],
                    start_to_close_timeout=timedelta(seconds=min(65, demo["startup_seconds"])),
                    schedule_to_close_timeout=timedelta(seconds=demo["startup_seconds"]),
                    heartbeat_timeout=timedelta(seconds=5),
                    retry_policy=RetryPolicy(
                        initial_interval=timedelta(seconds=1), maximum_attempts=10
                    ),
                    cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                )
                if ready != ready_receipt(request.identity, demo, request.binding):
                    raise ValueError("managed readiness binding mismatch")
            if workflow.now() < request.start:
                await workflow.sleep(request.start - workflow.now())
            artifact = await workflow.execute_activity(
                "execute_fixture",
                {"request": raw, "reference": None},
                task_queue=request.dispatch["runner_queue"],
                **options,
            )
            if request.demo:
                cutoff = request.end - timedelta(seconds=request.demo["settle_seconds"])
                remaining = cutoff - workflow.now()
                if remaining <= timedelta(0):
                    raise ValueError("managed export window was missed")
                export = await workflow.execute_activity(
                    "export_demo_telemetry",
                    raw,
                    task_queue=request.demo["task_queue"],
                    start_to_close_timeout=min(timedelta(seconds=65), remaining),
                    schedule_to_close_timeout=remaining,
                    heartbeat_timeout=timedelta(seconds=5),
                    retry_policy=RetryPolicy(
                        initial_interval=timedelta(seconds=1), maximum_attempts=5
                    ),
                    cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                )
                validate_export(export, request.identity, request.demo, request.start, request.end)
            if workflow.now() < request.end:
                await workflow.sleep(request.end - workflow.now())
            reference = await workflow.execute_activity(
                "publish_fixture_run",
                {"request": raw, "reference": artifact},
                task_queue=request.dispatch["runner_queue"],
                **options,
            )
            result: dict[str, Any] = await workflow.execute_activity(
                "assess_fixture",
                {"request": raw, "reference": reference},
                task_queue=request.dispatch["task_queue"],
                **options,
            )
            if target is not None and target.done():
                await target
                raise ValueError("managed target ended before assessment completed")
            return result
        except ValueError:
            return {"outcome": "blocked", "reason": "managed_demo_failed", "evidence": None}
        except asyncio.CancelledError:
            raise
        except ActivityError as error:
            if is_cancelled_exception(error):
                raise asyncio.CancelledError from error
            return {
                "outcome": "blocked",
                "reason": "fixture_or_assessment_failed",
                "evidence": None,
            }

        finally:
            if target is not None:
                target.cancel()
                try:
                    await asyncio.shield(target)
                except (asyncio.CancelledError, ActivityError):
                    pass

"""Durable intake -> independent assessment -> draft plan; no Jira or engineering start."""

import asyncio
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
    from ..api_wire import decode, evidence_from_wire, fields
    from ..execution.process import ExecutionBlocked, run_process
    from ..execution.temporal_activities import heartbeat
    from ..execution.wire import canonical
    from ..verification.config import environment, private_document
    from .analysis import POLICY_SHA256
    from .contracts import AnalysisRequest


class DiscoveryActivities:
    def __init__(
        self,
        policy: Path,
        journal: Path,
        role: str,
        env: Mapping[str, str],
        repository_dir: Path | None = None,
    ) -> None:
        if role not in {"intake", "assessment"} or (role == "intake") != (
            repository_dir is not None
        ):
            raise ValueError("invalid discovery worker role")
        self.policy_file, self.journal, self.role, self.repository_dir = (
            policy,
            journal,
            role,
            repository_dir,
        )
        from ..telemetry.schedule_config import private_path

        private_path(journal, directory=True)
        self.request = AnalysisRequest.parse(private_document(policy))
        self.env = environment(env)

    def call(self, operation: str, raw: dict[str, Any]) -> dict[str, Any]:
        try:
            heartbeat()
            names = {"request", "policy_sha256"}
            if operation in {"assessment", "plan"}:
                names.add("intake")
            if operation == "plan":
                names.add("assessment")
            fields(raw, names)
            request = AnalysisRequest.parse(raw["request"])
            if (
                request != self.request
                or raw["policy_sha256"] != POLICY_SHA256
                or AnalysisRequest.parse(private_document(self.policy_file)) != self.request
                or self.role != ("intake" if operation == "intake" else "assessment")
            ):
                raise ValueError("discovery operation differs from worker authorization")
            argv = [
                sys.executable,
                "-m",
                "sre_suite.discovery.child",
                operation,
                "--request",
                str(self.policy_file),
                "--journal",
                str(self.journal),
            ]
            if self.repository_dir is not None:
                argv += ["--repository-dir", str(self.repository_dir)]
            result = run_process(
                argv,
                self.journal,
                self.env,
                input_bytes=canonical(raw).encode(),
                timeout=300,
                max_output=262144,
                heartbeat=heartbeat,
            )
            if result.code:
                raise ApplicationError("discovery unavailable", non_retryable=result.code != 75)
            value = decode(result.output)
            if not isinstance(value, dict):
                raise ValueError("invalid discovery result")
            if operation == "plan":
                from ..jira_planning.contracts import Plan

                fields(value, {"plan", "plan_sha256", "execution_started", "jira_published"})
                plan = Plan.parse(value["plan"])
                if (
                    plan.discovery is None
                    or plan.discovery.request != request
                    or plan.fingerprint != value["plan_sha256"]
                    or value["execution_started"] is not False
                    or value["jira_published"] is not False
                ):
                    raise ValueError("draft identity mismatch")
            else:
                from ..api import evidence_id

                ref = evidence_from_wire(value)
                subject = (
                    request.intake_subject if operation == "intake" else request.assessor_subject
                )
                if (
                    ref.scope != request.scope
                    or ref.producer != subject
                    or ref.id
                    != evidence_id(subject, request.scope, operation + "-" + request.fingerprint)
                ):
                    raise ValueError("discovery receipt identity mismatch")
            heartbeat()
            return value
        except (ApplicationError, CancelledError):
            raise
        except ExecutionBlocked:
            raise ApplicationError("discovery budget exceeded", non_retryable=True) from None
        except Exception:
            raise ApplicationError("discovery blocked", non_retryable=True) from None

    @activity.defn(name="architecture_intake")
    def intake(self, raw: dict[str, Any]) -> dict[str, Any]:
        return self.call("intake", raw)

    @activity.defn(name="service_assessment")
    def assess(self, raw: dict[str, Any]) -> dict[str, Any]:
        return self.call("assessment", raw)

    @activity.defn(name="assessed_plan")
    def plan(self, raw: dict[str, Any]) -> dict[str, Any]:
        return self.call("plan", raw)


@workflow.defn
class ArchitectureAssessmentPlanning:
    @workflow.run
    async def run(self, raw: dict[str, Any]) -> dict[str, Any]:
        try:
            fields(raw, {"request", "policy_sha256"})
            request = AnalysisRequest.parse(raw["request"])
            if raw["policy_sha256"] != POLICY_SHA256:
                raise ValueError("analysis policy differs")
            current = dict(raw)
            for operation, name, queue in (
                ("intake", "architecture_intake", request.intake_queue),
                ("assessment", "service_assessment", request.assessment_queue),
                ("plan", "assessed_plan", request.assessment_queue),
            ):
                result: dict[str, Any] = await workflow.execute_activity(
                    name,
                    current,
                    task_queue=queue,
                    start_to_close_timeout=timedelta(seconds=330),
                    schedule_to_close_timeout=timedelta(minutes=12),
                    heartbeat_timeout=timedelta(seconds=5),
                    retry_policy=RetryPolicy(
                        initial_interval=timedelta(seconds=1), maximum_attempts=3
                    ),
                    cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                )
                if operation == "plan":
                    return {"outcome": "plan_proposed", **result}
                current = {**current, operation: result}
            raise ValueError("incomplete discovery workflow")
        except asyncio.CancelledError:
            raise
        except ActivityError as error:
            if is_cancelled_exception(error):
                raise asyncio.CancelledError from error
            return {"outcome": "blocked", "reason": "analysis_failed", "execution_started": False}
        except ValueError:
            return {"outcome": "blocked", "reason": "invalid_analysis", "execution_started": False}

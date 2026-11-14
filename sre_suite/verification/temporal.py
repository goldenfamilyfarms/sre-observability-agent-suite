"""Durable certification dispatch; bounded isolated deterministic verifier activity."""

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
    from ..api import evidence_id
    from ..api_wire import decode, evidence_from_wire, fields
    from ..execution.process import ExecutionBlocked, run_process
    from ..execution.temporal_activities import heartbeat
    from ..execution.wire import canonical
    from ..telemetry.schedule_config import private_path
    from .config import environment, load_policy
    from .contracts import VerificationInput
    from .runtime import identity


class VerificationActivities:
    def __init__(self, policy: Path, journal: Path, env: Mapping[str, str]) -> None:
        self.policy_file, self.journal = policy, journal
        self.policy = load_policy(policy)
        private_path(journal, directory=True)
        self.env = environment(env)

    @activity.defn(name="verify_certification")
    def verify(self, raw: dict[str, Any]) -> dict[str, Any]:
        try:
            heartbeat()
            value = VerificationInput.parse(raw)
            if (
                value.policy_sha256 != self.policy.fingerprint
                or value.release.scope != self.policy.scope
            ):
                raise ValueError("unauthorized certification policy")
            result = run_process(
                [
                    sys.executable,
                    "-m",
                    "sre_suite.verification.child",
                    "--policy",
                    str(self.policy_file),
                    "--journal",
                    str(self.journal),
                ],
                self.journal,
                self.env,
                input_bytes=canonical(raw).encode(),
                timeout=300,
                max_output=8192,
                heartbeat=heartbeat,
            )
            if result.code != 0:
                raise ApplicationError("certification unavailable", non_retryable=result.code != 75)
            receipt = fields(
                decode(result.output), {"workflow_id", "verdict", "report_sha256", "evidence"}
            )
            ref = evidence_from_wire(receipt["evidence"])
            if (
                receipt["workflow_id"] != identity(value)
                or receipt["verdict"] not in {"PASSED", "FAILED", "INCOMPLETE"}
                or ref.producer != self.policy.verifier
                or ref.scope != value.release.scope
                or ref.id != evidence_id(self.policy.verifier, value.release.scope, identity(value))
                or ref.sha256 != receipt["report_sha256"]
            ):
                raise ValueError("invalid certification receipt")
            heartbeat()
            return dict(receipt)
        except (ApplicationError, CancelledError):
            raise
        except ExecutionBlocked:
            raise ApplicationError(
                "certification process budget exceeded", non_retryable=True
            ) from None
        except Exception:
            raise ApplicationError("certification blocked", non_retryable=True) from None


@workflow.defn
class ServiceCertification:
    @workflow.run
    async def run(self, raw: dict[str, Any]) -> dict[str, Any]:
        try:
            result: dict[str, Any] = await workflow.execute_activity(
                "verify_certification",
                raw,
                start_to_close_timeout=timedelta(seconds=330),
                schedule_to_close_timeout=timedelta(minutes=12),
                heartbeat_timeout=timedelta(seconds=5),
                retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=3),
                cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            )
            return result
        except asyncio.CancelledError:
            raise
        except ActivityError as error:
            if is_cancelled_exception(error):
                raise asyncio.CancelledError from error
            return {"verdict": "INCOMPLETE", "reason": "verification_failed", "evidence": None}

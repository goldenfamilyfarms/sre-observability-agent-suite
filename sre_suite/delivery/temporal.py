"""Finite durable delivery with explicit start, policy pinning and cancellation."""

import asyncio
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, is_cancelled_exception

with workflow.unsafe.imports_passed_through():
    from ..api_wire import decode, fields
    from ..execution.process import ExecutionBlocked, run_process
    from ..execution.temporal_activities import heartbeat
    from ..execution.wire import canonical
    from .config import fingerprint, load


class DeliveryActivities:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.config = load(path)
        self.identity = fingerprint(self.config)

    @activity.defn(name="delivery_step")
    def step(self, value: dict[str, Any]) -> dict[str, Any]:
        try:
            raw = fields(value, {"policy", "operation"})
            if raw["policy"] != self.identity or fingerprint(load(self.path)) != self.identity:
                raise ValueError("delivery policy changed")
            if raw["operation"] not in {"deploy", "readiness", "cleanup"}:
                raise ValueError("invalid delivery operation")
            allowed = {
                "PATH",
                "LANG",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "SRE_EVIDENCE_API_URL",
                "SRE_EVIDENCE_API_TOKEN",
                "SRE_EVIDENCE_CA_FILE",
                "SRE_CONTROL_URL",
                "SRE_CONTROL_TOKEN",
                "SRE_CONTROL_CA_FILE",
                "SRE_KUBERNETES_TOKEN",
                "SRE_GCP_ACCESS_TOKEN",
            }
            result = run_process(
                [sys.executable, "-m", "sre_suite.delivery", "child", "--config", str(self.path)],
                Path(self.config["journal"]),
                {k: v for k, v in os.environ.items() if k in allowed},
                input_bytes=canonical(value).encode(),
                timeout=330,
                max_output=262144,
                heartbeat=heartbeat,
            )
            if result.code:
                raise ApplicationError("delivery unavailable", non_retryable=result.code != 75)
            output = decode(result.output)
            if not isinstance(output, dict):
                raise ValueError("invalid delivery receipt")
            return output
        except (ApplicationError, asyncio.CancelledError):
            raise
        except ExecutionBlocked:
            raise ApplicationError("delivery process budget exceeded", non_retryable=True) from None
        except Exception:
            raise ApplicationError("delivery blocked", non_retryable=True) from None


@workflow.defn
class ReleaseDelivery:
    @workflow.run
    async def run(self, raw: dict[str, Any]) -> dict[str, Any]:
        try:
            fields(raw, {"policy", "action", "timeout_seconds"})
            if (
                raw["action"] not in {"promote", "rollback", "cleanup"}
                or type(raw["timeout_seconds"]) is not int
                or not 30 <= raw["timeout_seconds"] <= 3600
            ):
                raise ValueError("invalid delivery workflow")
            options: dict[str, Any] = dict(
                start_to_close_timeout=timedelta(seconds=360),
                schedule_to_close_timeout=timedelta(minutes=15),
                heartbeat_timeout=timedelta(seconds=5),
                retry_policy=RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=1)),
                cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            )

            async def step(operation: str) -> dict[str, Any]:
                result: dict[str, Any] = await workflow.execute_activity(
                    "delivery_step", {"policy": raw["policy"], "operation": operation}, **options
                )
                return result

            deadline = workflow.now() + timedelta(seconds=raw["timeout_seconds"])
            if raw["action"] != "cleanup":
                await step("deploy")
            while workflow.now() < deadline:
                result = await step("cleanup" if raw["action"] == "cleanup" else "readiness")
                if result.get("complete") is True or result.get("ready") is True:
                    return result
                await workflow.sleep(10)
            return {"outcome": "timed_out", "accepted": False, "requires_reconciliation": True}
        except asyncio.CancelledError:
            raise
        except ActivityError as error:
            if is_cancelled_exception(error):
                raise asyncio.CancelledError from error
            return {"outcome": "blocked", "accepted": False, "requires_reconciliation": True}
        except ValueError:
            return {"outcome": "invalid_request", "accepted": False}

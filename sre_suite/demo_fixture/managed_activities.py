"""Long-lived target ownership and bounded export attempts on a dedicated Temporal worker."""

import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError, CancelledError

from ..api_wire import decode, fields
from ..execution.process import ExecutionBlocked, run_process
from ..execution.temporal_activities import heartbeat
from ..execution.wire import canonical
from ..telemetry.schedule_config import private_path
from .managed import ManagedPolicy, readiness, role_environment


class ManagedActivities:
    def __init__(self, config: Path, root: Path, env: Mapping[str, str]) -> None:
        self.config, self.root = config, root
        private_path(root, directory=True)
        self.policy = ManagedPolicy.load(config)
        self.env = dict(env)

    def call(self, operation: str, raw: dict[str, Any]) -> dict[str, Any]:
        try:
            heartbeat()
            # Re-read private policy so a changed endpoint or recipe cannot silently reuse routing.
            policy = ManagedPolicy.load(self.config)
            if policy.dispatch != self.policy.dispatch:
                raise ValueError("managed policy changed while worker was running")
            request = policy.authorize(raw)
            now = datetime.now(UTC)
            if (
                not request.start - timedelta(seconds=policy.dispatch["startup_seconds"])
                <= now
                <= request.end + timedelta(seconds=120)
            ):
                raise ValueError("managed request is outside its interval")
            timeout = 490 if operation == "serve" else 65
            result = run_process(
                [
                    sys.executable,
                    "-m",
                    "sre_suite.demo_fixture.managed_child",
                    operation,
                    "--config",
                    str(self.config),
                    "--root",
                    str(self.root),
                ],
                self.root,
                role_environment(policy, operation, self.env),
                input_bytes=canonical(raw).encode(),
                timeout=timeout,
                max_output=8192,
                heartbeat=heartbeat,
            )
            if result.code != 0:
                raise ApplicationError("managed demo unavailable", non_retryable=result.code != 75)
            value = decode(result.output)
            if operation == "ready":
                if value != readiness(request, policy):
                    raise ValueError("target readiness binding mismatch")
            elif operation == "export":
                row = fields(
                    value,
                    {
                        "workflow_id",
                        "policy_sha256",
                        "outbox_sha256",
                        "delivered_batches",
                        "completed_at",
                    },
                )
                from .managed_contracts import validate_export

                validate_export(row, request.identity, policy.dispatch, request.start, request.end)
            else:
                if value != {"status": "expired", "workflow_id": request.identity}:
                    raise ValueError("invalid target lifetime result")
            if not isinstance(value, dict):
                raise ValueError("invalid managed result")
            return value
        except (ApplicationError, CancelledError):
            raise
        except ExecutionBlocked:
            raise ApplicationError(
                "managed process deadline exceeded", non_retryable=True
            ) from None
        except Exception:
            raise ApplicationError("managed demo blocked", non_retryable=True) from None

    @activity.defn(name="serve_demo_target")
    def serve(self, raw: dict[str, Any]) -> dict[str, Any]:
        return self.call("serve", raw)

    @activity.defn(name="ready_demo_target")
    def ready(self, raw: dict[str, Any]) -> dict[str, Any]:
        return self.call("ready", raw)

    @activity.defn(name="export_demo_telemetry")
    def export(self, raw: dict[str, Any]) -> dict[str, Any]:
        return self.call("export", raw)

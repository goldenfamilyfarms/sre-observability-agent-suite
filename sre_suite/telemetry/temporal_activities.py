"""Heartbeat-aware process isolation supplies a real total deadline and cancellation."""

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError, CancelledError

from ..api_wire import decode, fields, text
from ..execution.process import ExecutionBlocked, run_process
from ..execution.temporal_activities import heartbeat
from ..execution.wire import canonical
from .contracts import integer
from .schedule_config import child_environment, load_policy, private_path
from .schedule_contracts import TelemetrySchedule


class TelemetryActivities:
    def __init__(
        self, policy_file: Path, journal: Path, env: Mapping[str, str], *, timeout: int = 60
    ) -> None:
        self.policy_file = policy_file
        self.policy = load_policy(policy_file)
        private_path(journal, directory=True)
        self.journal = journal
        self.env = child_environment(self.policy, env)
        self.timeout = integer(timeout, 1, 60)

    @activity.defn(name="collect_telemetry")
    def collect(self, value: dict[str, Any]) -> dict[str, Any]:
        try:
            heartbeat()
            raw = fields(value, {"schedule", "slot"})
            schedule = TelemetrySchedule.parse(raw["schedule"])
            schedule.authorize(self.policy)
            slot = integer(raw["slot"], 0, schedule.occurrences - 1)
            result = run_process(
                [
                    sys.executable,
                    "-m",
                    "sre_suite.telemetry.schedule_child",
                    "--policy",
                    str(self.policy_file),
                    "--journal",
                    str(self.journal),
                ],
                self.journal,
                self.env,
                input_bytes=canonical(value).encode(),
                timeout=self.timeout,
                max_output=8192,
                heartbeat=heartbeat,
            )
            heartbeat()
            if result.code != 0:
                raise ApplicationError(
                    "telemetry collection failed", non_retryable=result.code != 75
                )
            receipt = fields(
                decode(result.output), {"slot", "status", "evidence_id", "evidence_sha256"}
            )
            if integer(receipt["slot"], 0, 23) != slot or receipt["status"] not in {
                "observed",
                "partial",
                "empty",
            }:
                raise ValueError("invalid receipt")
            import re

            sha = text(receipt["evidence_sha256"], 64)
            if (
                not re.fullmatch(r"[0-9a-f]{64}", sha)
                or receipt["evidence_id"] != "telemetry-" + sha
            ):
                raise ValueError("invalid evidence reference")
            return dict(receipt)
        except (ApplicationError, CancelledError):
            raise
        except ExecutionBlocked:
            raise ApplicationError(
                "telemetry process budget exceeded", non_retryable=True
            ) from None
        except Exception:
            raise ApplicationError("telemetry collection blocked", non_retryable=True) from None

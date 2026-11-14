"""Temporal activity dispatch with bounded retry classification and cancellation."""

import asyncio
from collections.abc import Callable
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .http import RemoteError
from .runtime import ExecutionRuntime


def heartbeat() -> None:
    activity.heartbeat()
    if activity.is_cancelled():
        raise asyncio.CancelledError


class Activities:
    def __init__(self, runtime: ExecutionRuntime) -> None:
        self.runtime = runtime

    def _call(
        self, operation: Callable[..., dict[str, Any]], value: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            heartbeat()
            return operation(value, heartbeat)
        except RemoteError as error:
            retry = error.status in {0, 429} or error.status >= 500
            raise ApplicationError(
                "remote operation unavailable", non_retryable=not retry
            ) from None
        except Exception:
            raise ApplicationError(
                "execution blocked; inspect private configuration and artifacts", non_retryable=True
            ) from None

    @activity.defn(name="prepare_ticket")
    def prepare(self, value: dict[str, Any]) -> dict[str, Any]:
        return self._call(self.runtime.prepare, value)

    @activity.defn(name="execute_stage")
    def stage(self, value: dict[str, Any]) -> dict[str, Any]:
        return self._call(self.runtime.stage, value)

    @activity.defn(name="record_result")
    def finish(self, value: dict[str, Any]) -> dict[str, Any]:
        return self._call(self.runtime.finish, value)

    @activity.defn(name="prepare_engineering_telemetry")
    def telemetry(self, value: dict[str, Any]) -> dict[str, Any]:
        return self._call(self.runtime.prepare_telemetry, value)

    @activity.defn(name="prepare_engineering_certification")
    def certification(self, value: dict[str, Any]) -> dict[str, Any]:
        return self._call(self.runtime.prepare_certification, value)

    @activity.defn(name="bind_engineering_certification")
    def certification_input(self, value: dict[str, Any]) -> dict[str, Any]:
        return self._call(self.runtime.bind_certification, value)

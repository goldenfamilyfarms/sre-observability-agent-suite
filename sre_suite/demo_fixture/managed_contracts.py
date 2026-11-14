"""Small deterministic managed-target dispatch; credentials and host paths stay private."""

import re
from datetime import datetime
from typing import Any

from ..api_wire import fields, text
from ..telemetry.contracts import integer


def demo_dispatch(value: object) -> dict[str, Any]:
    row = fields(value, {"policy_sha256", "task_queue", "startup_seconds", "settle_seconds"})
    if not re.fullmatch(r"[0-9a-f]{64}", text(row["policy_sha256"], 64)):
        raise ValueError("invalid demo lifecycle policy digest")
    queue = text(row["task_queue"], 80)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", queue):
        raise ValueError("invalid demo lifecycle queue")
    return {
        "policy_sha256": row["policy_sha256"],
        "task_queue": queue,
        "startup_seconds": integer(row["startup_seconds"], 5, 60),
        "settle_seconds": integer(row["settle_seconds"], 1, 60),
    }


def validate_export(
    value: object, identity: str, dispatch: dict[str, Any], start: "datetime", end: "datetime"
) -> dict[str, Any]:
    from datetime import datetime, timedelta

    from ..contracts import aware

    row = fields(
        value,
        {"workflow_id", "policy_sha256", "outbox_sha256", "delivered_batches", "completed_at"},
    )
    stamp = datetime.fromisoformat(text(row["completed_at"], 64))
    aware(stamp)
    if (
        row["workflow_id"] != identity
        or row["policy_sha256"] != dispatch["policy_sha256"]
        or type(row["delivered_batches"]) is not int
        or row["delivered_batches"] != 2
        or not re.fullmatch(r"[0-9a-f]{64}", text(row["outbox_sha256"], 64))
        or not start <= stamp < end - timedelta(seconds=dispatch["settle_seconds"])
    ):
        raise ValueError("invalid managed export completion")
    return dict(row)


def ready_receipt(identity: str, dispatch: dict[str, Any], binding: object) -> dict[str, Any]:
    from ..execution.wire import digest

    return {
        "workflow_id": identity,
        "policy_sha256": dispatch["policy_sha256"],
        "binding_sha256": digest(binding),
        "status": "ready",
    }

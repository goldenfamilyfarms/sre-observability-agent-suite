"""Explicit schedule validation/start, dedicated worker and scoped execution management."""

import argparse
import asyncio
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..execution.wire import canonical
from ..local_pilot.config import private_json
from .schedule_config import collector_from_env, load_policy, private_path
from .schedule_contracts import TelemetrySchedule, policy_digest


async def run(args: argparse.Namespace) -> None:
    from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
    from temporalio.worker import Worker

    from ..execution.cli import connect
    from .temporal_activities import TelemetryActivities
    from .temporal_workflow import TelemetryCollection

    queue = os.environ.get("SRE_TELEMETRY_TASK_QUEUE", "sre-telemetry-v1")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", queue):
        raise ValueError("invalid telemetry queue")
    if args.command == "start":
        if not args.authorize_collection:
            raise ValueError("explicit collection authorization is required")
        policy = load_policy(args.policy)
        private_path(args.schedule, directory=False)
        schedule = TelemetrySchedule.parse(private_json(args.schedule))
        schedule.authorize(policy)
        schedule.startable(datetime.now(UTC))
        collector_from_env(policy, os.environ).evidence.context(schedule.request.scope)
        client = await connect()
        await client.start_workflow(
            TelemetryCollection.run,
            schedule.wire(),
            id=schedule.workflow_id,
            task_queue=queue,
            execution_timeout=timedelta(hours=26),
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )
        print(canonical({"workflow_id": schedule.workflow_id}))
    elif args.command == "worker":
        activities = TelemetryActivities(args.policy, args.journal, os.environ)
        client = await connect()
        with ThreadPoolExecutor(max_workers=2) as executor:
            worker = Worker(
                client,
                task_queue=queue,
                workflows=[TelemetryCollection],
                activities=[activities.collect],
                activity_executor=executor,
                max_concurrent_activities=2,
                max_heartbeat_throttle_interval=timedelta(seconds=1),
                default_heartbeat_throttle_interval=timedelta(seconds=1),
            )
            await worker.run()
    else:
        if not re.fullmatch(r"sre-telemetry-[0-9a-f]{64}", args.workflow_id):
            raise ValueError("expected a telemetry workflow identity")
        client = await connect()
        handle = client.get_workflow_handle(args.workflow_id)
        if args.command == "cancel":
            await handle.cancel()
            print(canonical({"cancellation_requested": True}))
        elif args.command == "result":
            result = await handle.result()
            print(canonical(result))
        else:
            state = await handle.describe()
            print(canonical({"status": state.status.name if state.status else "UNKNOWN"}))


def main() -> int:
    parser = argparse.ArgumentParser(description="Explicit finite Temporal telemetry scheduling")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("fingerprint", "validate", "start", "worker"):
        command = commands.add_parser(name)
        command.add_argument("--policy", type=Path, required=True)
        if name in {"validate", "start"}:
            command.add_argument("--schedule", type=Path, required=True)
        if name == "start":
            command.add_argument("--authorize-collection", action="store_true")
        if name == "worker":
            command.add_argument("--journal", type=Path, required=True)
    for name in ("status", "result", "cancel"):
        commands.add_parser(name).add_argument("workflow_id")
    args = parser.parse_args()
    try:
        if args.command in {"fingerprint", "validate"}:
            policy = load_policy(args.policy)
            result = {"policy_sha256": policy_digest(policy)}
            if args.command == "validate":
                private_path(args.schedule, directory=False)
                schedule = TelemetrySchedule.parse(private_json(args.schedule))
                schedule.authorize(policy)
                schedule.startable(datetime.now(UTC))
                result["workflow_id"] = schedule.workflow_id
            print(json.dumps(result))  # Offline: no API reads or workflow creation.
        else:
            asyncio.run(run(args))
        return 0
    except Exception:
        print("Telemetry scheduling failed; inspect private configuration and worker health.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

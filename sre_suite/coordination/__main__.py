"""Suite worker and explicit workflow start; agent subprocesses receive role-scoped credentials."""

import argparse
import asyncio
import json
import signal
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any

from ..api_wire import decode, fields
from ..execution.http import RemoteError
from ..execution.wire import canonical
from .runtime import SuiteRuntime


def main() -> int:
    parser = argparse.ArgumentParser(description="Guarded suite orchestration")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "start", "worker", "child"):
        command = sub.add_parser(name)
        command.add_argument("--config", required=True, type=Path)
        if name == "start":
            command.add_argument("--authorize-start", action="store_true")
    args = parser.parse_args()
    try:
        agent = SuiteRuntime(args.config)
        if args.command in {"start", "worker"}:
            if args.command == "start" and not args.authorize_start:
                raise ValueError("explicit workflow start required")
            result = asyncio.run(durable(args, agent))
        elif args.command == "child":
            signal.alarm(550)
            value = fields(decode(sys.stdin.buffer.read(262145)), {"operation", "request"})
            if value["operation"] not in {"prepare", "execute", "record"}:
                raise ValueError("unsupported operation")
            result = getattr(agent, str(value["operation"]))(value["request"])
        else:
            agent.guard()
            result = {
                "workflow_id": agent.plan.identity,
                "nodes": len(agent.plan.nodes),
                "execution_started": False,
            }
        print(canonical(result))
        return 0
    except RemoteError as error:
        print(json.dumps({"outcome": "blocked", "reason": "service_unavailable"}))
        return 75 if error.status in {0, 429} or error.status >= 500 else 1
    except Exception:
        print(json.dumps({"outcome": "blocked", "reason": "suite_unavailable"}))
        return 1
    finally:
        if args.command == "child":
            signal.alarm(0)


async def durable(args: argparse.Namespace, agent: SuiteRuntime) -> dict[str, Any]:
    from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
    from temporalio.worker import Worker

    from ..execution.cli import connect
    from .activities import SuiteActivities
    from .temporal import SuiteExecution

    client = await connect()
    if args.command == "start":
        if agent.config["role"] != "controller":
            raise ValueError("controller required")
        agent.guard()
        await client.start_workflow(
            SuiteExecution.run,
            agent.plan.raw,
            id=agent.plan.identity,
            task_queue=agent.plan.raw["task_queue"],
            execution_timeout=timedelta(hours=24),
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )
        return {"workflow_id": agent.plan.identity, "execution_started": True}
    activities = SuiteActivities(args.config)
    controller = agent.config["role"] == "controller"
    queue = (
        agent.plan.raw["task_queue"]
        if controller
        else next(n["task_queue"] for n in agent.plan.nodes if n["id"] == agent.config["role"])
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        await Worker(
            client,
            task_queue=queue,
            workflows=[SuiteExecution] if controller else [],
            activities=[activities.prepare, activities.record]
            if controller
            else [activities.execute],
            activity_executor=executor,
            max_heartbeat_throttle_interval=timedelta(seconds=1),
        ).run()
    return {"stopped": True}


if __name__ == "__main__":
    raise SystemExit(main())

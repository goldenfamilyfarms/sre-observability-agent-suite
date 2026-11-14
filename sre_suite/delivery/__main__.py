"""Explicit provider operations; imports and configuration preparation never deploy."""

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
from ..verification.config import private_document
from .config import fingerprint, load, runtime
from .contracts import DeliveryRequest


async def durable(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
    from temporalio.worker import Worker

    from ..execution.cli import connect
    from .temporal import DeliveryActivities, ReleaseDelivery

    if args.command == "start" and not args.authorize_start:
        raise ValueError("explicit start authorization required")
    client = await connect()
    request = DeliveryRequest.parse(private_document(Path(config["request"])))
    if args.command == "start":
        runtime(config).guard()
        await client.start_workflow(
            ReleaseDelivery.run,
            {
                "policy": fingerprint(config),
                "action": request.binding.action,
                "timeout_seconds": args.timeout,
            },
            id=request.identity,
            task_queue=config["task_queue"],
            execution_timeout=timedelta(hours=2),
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )
        return {"workflow_id": request.identity}
    with ThreadPoolExecutor(max_workers=2) as executor:
        await Worker(
            client,
            task_queue=config["task_queue"],
            workflows=[ReleaseDelivery],
            activities=[DeliveryActivities(args.config).step],
            activity_executor=executor,
            max_heartbeat_throttle_interval=timedelta(seconds=1),
        ).run()
    return {"stopped": True}


def main() -> int:
    parser = argparse.ArgumentParser(description="Guarded shipping deployment and readiness")
    sub = parser.add_subparsers(dest="command", required=True)
    chart = sub.add_parser("export-chart")
    chart.add_argument("--output", type=Path, required=True)
    for name in (
        "prepare",
        "readiness",
        "worker",
        "start",
        "child",
        "shipping-inspect",
        "shipping-merge",
    ):
        command = sub.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        if name == "shipping-merge":
            command.add_argument("--authorize-merge", action="store_true")
        if name == "start":
            command.add_argument("--authorize-start", action="store_true")
            command.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    try:
        if args.command == "export-chart":
            from ..pilot_demo.prepare import publish_directory
            from .helm import CHART, CHART_DIGEST, TEMPLATE

            publish_directory(
                args.output,
                {"Chart.yaml": CHART.encode(), "templates/resources.yaml": TEMPLATE.encode()},
            )
            print(canonical({"chart_digest": CHART_DIGEST, "deployed": False}))
            return 0
        if args.command.startswith("shipping-"):
            from .shipping_runtime import load_runtime

            shipping = load_runtime(args.config)
            if args.command == "shipping-merge" and not args.authorize_merge:
                raise ValueError("explicit merge start required")
            result = shipping.merge() if args.command == "shipping-merge" else shipping.inspect()
            print(canonical(result))
            return 0
        config = load(args.config)
        if args.command in {"worker", "start"}:
            result = asyncio.run(durable(args, config))
        elif args.command == "child":
            signal.alarm(320)
            raw = fields(decode(sys.stdin.buffer.read(65537)), {"policy", "operation"})
            if raw["policy"] != fingerprint(config) or raw["operation"] not in {
                "deploy",
                "readiness",
                "cleanup",
            }:
                raise ValueError("operation differs from worker policy")
            agent = runtime(config)
            result = getattr(agent, str(raw["operation"]))()
        else:
            agent = runtime(config)
            result = agent.prepare() if args.command == "prepare" else agent.readiness()
        print(canonical(result))
        return 0
    except RemoteError as error:
        print(json.dumps({"outcome": "blocked", "reason": "provider_unavailable"}))
        return 75 if error.status in {0, 429} or error.status >= 500 else 1
    except Exception:
        print(json.dumps({"outcome": "blocked", "reason": "delivery_unavailable"}))
        return 1
    finally:
        if args.command == "child":
            signal.alarm(0)


if __name__ == "__main__":
    raise SystemExit(main())

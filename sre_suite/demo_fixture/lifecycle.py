"""Explicit managed worker startup; canonical run scheduling remains in Temporal."""

import argparse
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from ..execution.wire import canonical
from .managed import ManagedPolicy


async def worker(args: argparse.Namespace) -> None:
    if not args.authorize_targets or not args.authorize_export:
        raise ValueError("both target startup and telemetry export require authorization")
    from temporalio.worker import Worker

    from ..execution.cli import connect
    from .managed_activities import ManagedActivities

    activities = ManagedActivities(args.config, args.root, os.environ)
    client = await connect()
    with ThreadPoolExecutor(max_workers=4) as executor:
        await Worker(
            client,
            task_queue=activities.policy.dispatch["task_queue"],
            workflows=[],
            activities=[activities.serve, activities.ready, activities.export],
            activity_executor=executor,
            max_concurrent_activities=4,
            max_heartbeat_throttle_interval=timedelta(seconds=1),
            default_heartbeat_throttle_interval=timedelta(seconds=1),
        ).run()


def main() -> int:
    parser = argparse.ArgumentParser(description="Opt-in managed local demo lifecycle worker")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "worker"):
        p = sub.add_parser(command)
        p.add_argument("--config", type=Path, required=True)
        if command == "worker":
            p.add_argument("--root", type=Path, required=True)
            p.add_argument("--authorize-targets", action="store_true")
            p.add_argument("--authorize-export", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "validate":
            policy = ManagedPolicy.load(args.config)
            print(canonical({"demo": policy.dispatch, "started": False}))
        else:
            asyncio.run(worker(args))
        return 0
    except Exception:
        print("Managed demo blocked; inspect private configuration and current pilot scope.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Explicit runtime entry points; no external service is contacted on import."""

import argparse
import asyncio
import json
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from temporalio.client import Client
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from ..api_wire import decode
from .config import Settings, flag, temporal_address
from .runtime import ExecutionRuntime, execution_key


async def connect() -> "Client":
    from temporalio.client import Client

    address = os.environ.get("SRE_TEMPORAL_ADDRESS", "127.0.0.1:7233")
    tls = flag(os.environ, "SRE_TEMPORAL_TLS")
    address = temporal_address(address, tls)
    return await Client.connect(
        address,
        namespace=os.environ.get("SRE_TEMPORAL_NAMESPACE", "default"),
        tls=tls,
        api_key=os.environ.get("SRE_TEMPORAL_API_KEY"),
    )


async def run(args: argparse.Namespace) -> None:
    from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
    from temporalio.worker import Worker

    from .temporal_activities import Activities
    from .temporal_workflow import TicketExecution

    client = await connect()
    queue = os.environ.get("SRE_TEMPORAL_TASK_QUEUE", "sre-engineering-v1")
    if args.command == "worker":
        settings = Settings.from_env(os.environ)
        activities = Activities(ExecutionRuntime(settings))
        with ThreadPoolExecutor(max_workers=2) as executor:
            worker = Worker(
                client,
                task_queue=queue,
                workflows=[TicketExecution],
                activities=[activities.prepare, activities.stage, activities.finish],
                activity_executor=executor,
                max_concurrent_activities=2,
                max_heartbeat_throttle_interval=timedelta(seconds=1),
                default_heartbeat_throttle_interval=timedelta(seconds=1),
            )
            await worker.run()
    elif args.command == "start":
        spec = decode(Path(args.spec).read_bytes())
        if not isinstance(spec, dict):
            raise ValueError("execution spec must be an object")
        identity = execution_key(spec)
        await client.start_workflow(
            TicketExecution.run,
            spec,
            id=identity,
            task_queue=queue,
            execution_timeout=timedelta(hours=4),
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )
        print(json.dumps({"workflow_id": identity}))
    elif args.command == "cancel":
        await client.get_workflow_handle(args.workflow_id).cancel()
        print(json.dumps({"cancellation_requested": True}))
    elif args.command == "result":
        print(json.dumps(await client.get_workflow_handle(args.workflow_id).result()))
    else:
        handle = client.get_workflow_handle(args.workflow_id)
        description = await handle.describe()
        print(
            json.dumps(
                {
                    "workflow_id": args.workflow_id,
                    "status": description.status.name if description.status else "UNKNOWN",
                }
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="SRE Temporal execution pilot")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("worker")
    commands.add_parser("check-config")
    commands.add_parser("start").add_argument("spec")
    commands.add_parser("preflight").add_argument("spec")
    pilot = commands.add_parser("pilot-setup")
    pilot.add_argument("manifest")
    pilot.add_argument("--baseline", required=True)
    pilot.add_argument("--output", required=True)
    export = commands.add_parser("export-patch")
    export.add_argument("workflow_id")
    export.add_argument("--digest", required=True)
    export.add_argument("--output", required=True)
    for name in ("status", "result", "cancel"):
        commands.add_parser(name).add_argument("workflow_id")
    args = parser.parse_args()
    try:
        if args.command == "pilot-setup":
            from .pilot import PilotSetupError, setup_pilot

            try:
                result = setup_pilot(
                    os.environ, Path(args.manifest), Path(args.baseline), Path(args.output)
                )
            except PilotSetupError as error:
                print(
                    json.dumps(
                        {
                            "status": "failed",
                            "phase": error.phase,
                            "remote_changes_may_exist": error.phase != "validation",
                        }
                    )
                )
                raise SystemExit(1) from None
            print(json.dumps(result))
        elif args.command == "preflight":
            from .readiness import exit_code, run_preflight

            result = asyncio.run(run_preflight(os.environ, Path(args.spec)))
            print(json.dumps(result, indent=2))
            raise SystemExit(exit_code(result))
        elif args.command == "check-config":
            settings = Settings.from_env(os.environ)
            print(
                json.dumps(
                    {
                        "configuration_valid": True,
                        "claude_enabled": settings.claude_enabled,
                        "jira_writes_enabled": settings.jira_writes,
                        "github_prs_enabled": settings.github_prs,
                        "jira_transitions_enabled": bool(settings.jira_transitions),
                    }
                )
            )
        elif args.command == "export-patch":
            from .journal import Journal

            settings = Settings.from_env(os.environ)
            artifact = Journal(settings.work_root).read(
                args.workflow_id + ":implement", args.digest
            )
            if artifact["verdict"] != "passed" or not isinstance(artifact.get("patch"), str):
                raise ValueError("no successful patch artifact")
            with Path(args.output).open("x", encoding="utf-8") as output:
                output.write(artifact["patch"])
            print(json.dumps({"patch_exported": True}))
        else:
            asyncio.run(run(args))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception:
        # Configuration values and remote exception details may contain private data.
        parser.exit(
            1, "Execution failed; check private configuration, workflow status and artifacts.\n"
        )


if __name__ == "__main__":
    main()

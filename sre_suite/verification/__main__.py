"""Explicit certification commands. No query collection, merge, or deployment actions."""

import argparse
import asyncio
import os
import re
import signal
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any

from ..certification import load_catalog
from ..execution.journal import Journal
from ..execution.wire import canonical
from ..telemetry.schedule_config import private_path
from .config import client, load_policy, private_document
from .contracts import VerificationInput, catalog_digest
from .runtime import CertificationEngine, identity


def write_new(path: Path, value: object) -> None:
    private_path(path.parent, directory=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(canonical(value) + "\n")


async def durable(args: argparse.Namespace) -> None:
    from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
    from temporalio.worker import Worker

    from ..execution.cli import connect
    from .temporal import ServiceCertification, VerificationActivities

    queue = os.environ.get("SRE_CERTIFICATION_TASK_QUEUE", "sre-certification-v1")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", queue):
        raise ValueError("invalid certification queue")
    if args.command == "start":
        if not args.authorize_verification:
            raise ValueError("explicit verification and report publication authorization required")
        policy = load_policy(args.policy)
        private_path(args.input, directory=False)
        value = VerificationInput.parse(private_document(args.input))
        if value.policy_sha256 != policy.fingerprint or value.release.scope != policy.scope:
            raise ValueError("input differs from reviewed policy")
        client(os.environ).context(policy.scope, value.context_version)
        temporal = await connect()
        await temporal.start_workflow(
            ServiceCertification.run,
            value.wire(),
            id=identity(value),
            task_queue=queue,
            execution_timeout=timedelta(minutes=15),
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )
        print(canonical({"workflow_id": identity(value)}))
    elif args.command == "worker":
        activities = VerificationActivities(args.policy, args.journal, os.environ)
        temporal = await connect()
        with ThreadPoolExecutor(max_workers=2) as executor:
            await Worker(
                temporal,
                task_queue=queue,
                workflows=[ServiceCertification],
                activities=[activities.verify],
                activity_executor=executor,
                max_concurrent_activities=2,
                max_heartbeat_throttle_interval=timedelta(seconds=1),
                default_heartbeat_throttle_interval=timedelta(seconds=1),
            ).run()
    else:
        if not re.fullmatch(r"sre-certification-[0-9a-f]{64}", args.workflow_id):
            raise ValueError("expected certification workflow identity")
        temporal = await connect()
        handle = temporal.get_workflow_handle(args.workflow_id)
        if args.command == "cancel":
            await handle.cancel()
            print(canonical({"cancellation_requested": True}))
        else:
            print(canonical(await handle.result()))


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic 40/40/40 telemetry certification")
    commands = parser.add_subparsers(dest="command", required=True)
    template = commands.add_parser("template")
    template.add_argument("--output", type=Path, required=True)
    for name in ("validate", "run", "worker", "start"):
        command = commands.add_parser(name)
        command.add_argument("--policy", type=Path, required=True)
        if name in {"run", "start"}:
            command.add_argument("--input", type=Path, required=True)
        if name in {"run", "worker"}:
            command.add_argument("--journal", type=Path, required=True)
        if name == "run":
            command.add_argument("--output", type=Path, required=True)
        if name == "start":
            command.add_argument("--authorize-verification", action="store_true")
    for name in ("result", "cancel"):
        commands.add_parser(name).add_argument("workflow_id")
    args = parser.parse_args()
    try:
        if args.command == "template":
            catalog = load_catalog()
            value: dict[str, Any] = {
                "schema_version": 2,
                "catalog_sha256": catalog_digest(catalog),
                "scope": None,
                "collector": None,
                "assessor": None,
                "verifier": None,
                "sources": [],
                "checks": {
                    c.id: {
                        "applicable": None,
                        "source": None,
                        "selector": {},
                        "parameters": None,
                        "minimum_records": 1,
                    }
                    for c in catalog.checks
                },
            }
            write_new(args.output, value)
            print(canonical({"template_created": True, "ready": False}))
        elif args.command == "validate":
            policy = load_policy(args.policy)
            print(canonical({"policy_sha256": policy.fingerprint, "checks": len(policy.rules)}))
        elif args.command == "run":
            # The local mode shares the independent process ceiling, even for slow API streams.
            signal.alarm(300)
            try:
                policy = load_policy(args.policy)
                private_path(args.input, directory=False)
                private_path(args.journal, directory=True)
                run_input = private_document(args.input)
                report = CertificationEngine(policy, client(os.environ), Journal(args.journal)).run(
                    run_input
                )
                write_new(args.output, report)
                print(canonical({"verdict": report["verdict"], "checks": 120}))
                return 0 if report["verdict"] == "PASSED" else 2
            finally:
                signal.alarm(0)
        else:
            asyncio.run(durable(args))
        return 0
    except Exception:
        print("Certification failed; inspect private inputs, evidence and worker health.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

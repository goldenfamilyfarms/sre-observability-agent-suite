"""Explicit independent fixture workers and authorized standalone dispatch."""

import argparse
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from ..execution.wire import canonical
from ..verification.__main__ import write_new
from ..verification.config import private_document
from ..verification.contracts import VerificationPolicy
from .config import load_policy
from .contracts import FixtureRequest


async def run(args: argparse.Namespace) -> None:
    from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
    from temporalio.worker import Worker

    from ..execution.cli import connect
    from .temporal import FixtureActivities, FixtureAssessment

    policy = load_policy(args.policy)
    if args.command == "start" and not args.authorize_fixtures:
        raise ValueError("explicit authorization for fixture traffic and assessment is required")
    if args.command == "start":
        raw = private_document(args.request, 65536)
        if not isinstance(raw, dict):
            raise ValueError("fixture request must be an object")
        request = FixtureRequest.parse(raw)
        request.authorize(policy)
        if request.demo and not getattr(args, "authorize_demo", False):
            raise ValueError("managed target startup and export require explicit authorization")
    client = await connect()
    if args.command == "worker":
        dispatch = FixtureActivities(args.policy, args.journal, args.role, os.environ)
        with ThreadPoolExecutor(max_workers=2) as executor:
            await Worker(
                client,
                task_queue=policy.runner_queue if args.role == "runner" else policy.assessor_queue,
                workflows=[] if args.role == "runner" else [FixtureAssessment],
                activities=[dispatch.execute, dispatch.publish]
                if args.role == "runner"
                else [dispatch.assess],
                activity_executor=executor,
                max_concurrent_activities=2,
                max_heartbeat_throttle_interval=timedelta(seconds=1),
                default_heartbeat_throttle_interval=timedelta(seconds=1),
            ).run()
    else:
        assert isinstance(raw, dict)
        await client.start_workflow(
            FixtureAssessment.run,
            raw,
            id=request.identity,
            task_queue=policy.assessor_queue,
            execution_timeout=timedelta(minutes=70),
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )
        print(canonical({"workflow_id": request.identity}))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Independent fixture runner and 120-check assessor"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "worker", "start"):
        p = sub.add_parser(name)
        p.add_argument("--policy", type=Path, required=True)
        if name == "worker":
            p.add_argument("--role", choices=("runner", "assessor"), required=True)
            p.add_argument("--journal", type=Path, required=True)
        if name == "start":
            p.add_argument("--request", type=Path, required=True)
            p.add_argument("--authorize-fixtures", action="store_true")
            p.add_argument("--authorize-demo", action="store_true")
    template = sub.add_parser("template")
    template.add_argument("--verification-policy", type=Path, required=True)
    template.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "template":
            raw = private_document(args.verification_policy, 1_048_576)
            verification = VerificationPolicy.parse(raw)
            write_new(
                args.output,
                {
                    "schema_version": 2,
                    "verification_policy": raw,
                    "runner": None,
                    "target": {
                        "base_url": None,
                        "authorization_env": "SRE_FIXTURE_TARGET_AUTH",
                        "ca_env": None,
                    },
                    "runner_queue": "sre-fixture-runner-v1",
                    "assessor_queue": "sre-fixture-assessor-v1",
                    "scenarios": [],
                    "checks": {
                        r.id: {"scenario": None, "expected": [], "justification": ""}
                        for r in verification.rules
                    },
                },
            )
            print(canonical({"template_created": True, "ready": False}))
        elif args.command == "validate":
            policy = load_policy(args.policy)
            print(
                canonical(
                    {"dispatch": policy.dispatch, "scenarios": len(policy.scenarios), "checks": 120}
                )
            )
        else:
            asyncio.run(run(args))
        return 0
    except Exception:
        print("Fixture operation blocked; inspect private policy, evidence and worker health.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

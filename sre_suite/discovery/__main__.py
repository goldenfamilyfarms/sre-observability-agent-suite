"""Explicit source pinning, role commands and durable analysis; never publish Jira implicitly."""

import argparse
import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any

from ..api_wire import evidence_from_wire
from ..execution.journal import Journal
from ..execution.wire import canonical
from ..pilot_demo.prepare import publish_directory
from ..verification.config import client, private_document
from .analysis import POLICY_SHA256
from .contracts import AnalysisRequest, paths
from .runtime import DiscoveryRuntime
from .source import snapshot


async def durable(args: argparse.Namespace, request: AnalysisRequest) -> dict[str, Any]:
    from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
    from temporalio.worker import Worker

    from ..execution.cli import connect
    from .temporal import ArchitectureAssessmentPlanning, DiscoveryActivities

    if args.command == "start" and not args.authorize_analysis:
        raise ValueError("explicit analysis start authorization required")
    client(os.environ).context(request.scope, request.context_version)
    temporal = await connect()
    if args.command == "start":
        from datetime import UTC, datetime

        if not datetime.now(UTC) < request.expires_at:
            raise ValueError("analysis expired")
        await temporal.start_workflow(
            ArchitectureAssessmentPlanning.run,
            {"request": request.wire(), "policy_sha256": POLICY_SHA256},
            id=request.workflow_id,
            task_queue=request.intake_queue,
            execution_timeout=timedelta(minutes=40),
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )
        return {"workflow_id": request.workflow_id, "engineering_started": False}
    if args.command == "result":
        from ..jira_planning.contracts import Plan

        value = await temporal.get_workflow_handle(request.workflow_id).result()
        if not isinstance(value, dict) or value.get("outcome") != "plan_proposed":
            raise ValueError("analysis did not produce a plan")
        proposed = Plan.parse(value["plan"])
        origin = proposed.discovery
        if origin is None or origin.request != request:
            raise ValueError("analysis result differs from request")
        runtime = DiscoveryRuntime(request, client(os.environ), Journal(args.journal), "assessment")
        checked = runtime.plan(origin.intake, origin.assessment)
        if canonical(checked) != canonical({k: v for k, v in value.items() if k != "outcome"}):
            raise ValueError("analysis result differs from authenticated evidence")
        publish_directory(args.output, plan_files(checked))
        return {"prepared": True, "execution_started": False, "jira_published": False}
    activities = DiscoveryActivities(
        args.request, args.journal, args.role, os.environ, args.repository_dir
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        await Worker(
            temporal,
            task_queue=request.intake_queue if args.role == "intake" else request.assessment_queue,
            workflows=[ArchitectureAssessmentPlanning] if args.role == "intake" else [],
            activities=[activities.intake]
            if args.role == "intake"
            else [activities.assess, activities.plan],
            activity_executor=executor,
            max_concurrent_activities=2,
            max_heartbeat_throttle_interval=timedelta(seconds=1),
            default_heartbeat_throttle_interval=timedelta(seconds=1),
        ).run()
    return {"stopped": True}


def plan_files(result: dict[str, Any]) -> dict[str, bytes]:
    return {
        "plan.json": (canonical(result["plan"]) + "\n").encode(),
        "review.json": (
            canonical({k: v for k, v in result.items() if k != "plan"}) + "\n"
        ).encode(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only architecture and assessment agents")
    commands = parser.add_subparsers(dest="command", required=True)
    pin = commands.add_parser("pin-source")
    pin.add_argument("--repository-dir", type=Path, required=True)
    pin.add_argument("--repository", required=True)
    pin.add_argument("--revision", required=True)
    pin.add_argument("--files", type=Path, required=True)
    pin.add_argument("--output", type=Path, required=True)
    for name in ("intake", "assessment", "plan", "start", "worker", "result"):
        command = commands.add_parser(name)
        command.add_argument("--request", type=Path, required=True)
        if name in {"intake", "worker"}:
            command.add_argument("--repository-dir", type=Path, required=name == "intake")
        if name != "start":
            command.add_argument("--journal", type=Path, required=True)
        if name in {"intake", "assessment", "plan", "result"}:
            command.add_argument("--output", type=Path, required=True)
        if name in {"assessment", "plan"}:
            command.add_argument("--intake-evidence", type=Path, required=True)
        if name == "plan":
            command.add_argument("--assessment-evidence", type=Path, required=True)
        if name == "worker":
            command.add_argument("--role", choices=("intake", "assessment"), required=True)
        if name == "start":
            command.add_argument("--authorize-analysis", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "pin-source":
            inventory, _ = snapshot(
                args.repository_dir,
                args.repository,
                args.revision,
                paths(private_document(args.files)),
            )
            publish_directory(
                args.output, {"source-pin.json": (canonical(inventory) + "\n").encode()}
            )
            result: dict[str, Any] = {
                "source_artifact_digest": inventory["artifact_digest"],
                "execution_started": False,
            }
        else:
            request = AnalysisRequest.parse(private_document(args.request))
            if args.command in {"start", "worker", "result"}:
                result = asyncio.run(durable(args, request))
            else:
                runtime = DiscoveryRuntime(
                    request,
                    client(os.environ),
                    Journal(args.journal),
                    "intake" if args.command == "intake" else "assessment",
                    getattr(args, "repository_dir", None),
                )
                if args.command == "intake":
                    result = runtime.intake()
                else:
                    intake = evidence_from_wire(private_document(args.intake_evidence))
                    result = (
                        runtime.assess(intake)
                        if args.command == "assessment"
                        else runtime.plan(
                            intake, evidence_from_wire(private_document(args.assessment_evidence))
                        )
                    )
                files = (
                    plan_files(result)
                    if args.command == "plan"
                    else {"evidence.json": (canonical(result) + "\n").encode()}
                )
                publish_directory(args.output, files)
                result = {"prepared": True, "execution_started": False, "jira_published": False}
        print(json.dumps(result))
        return 0
    except Exception:
        print(json.dumps({"status": "blocked", "reason": "discovery_unavailable"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

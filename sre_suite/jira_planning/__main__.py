"""Explicit planning commands; credentials come only from private environment variables."""

import argparse
import json
import os
from pathlib import Path

from ..api_wire import decode
from ..execution.pilot import _read
from ..execution.wire import canonical
from ..pilot_demo.prepare import publish_directory
from .client import Planner
from .contracts import Plan


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scoped Jira plan discovery, review and publication"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("discover")
    review = commands.add_parser("dry-run")
    review.add_argument("plan", type=Path)
    review.add_argument("--output", type=Path, required=True)
    publish = commands.add_parser("publish")
    publish.add_argument("plan", type=Path)
    publish.add_argument("--approve-digest", required=True)
    publish.add_argument("--journal", type=Path, required=True)
    args = parser.parse_args()
    try:
        planner = Planner.from_env(os.environ, discovery_only=args.command == "discover")
        if args.command == "discover":
            print(json.dumps(planner.discover()))
        else:
            plan = Plan.parse(decode(_read(args.plan, 100_000)))
            if args.command == "dry-run":
                report = planner.dry_run(plan)
                publish_directory(args.output, {"review.json": (canonical(report) + "\n").encode()})
                print(json.dumps({"read_only": True, "approval_digest": report["approval_digest"]}))
            else:
                print(json.dumps(planner.publish(plan, args.approve_digest, args.journal)))
    except Exception:
        parser.exit(
            1,
            "Jira planning stopped; inspect private configuration, scope, "
            "journal and remote issue state.\n",
        )


if __name__ == "__main__":
    main()

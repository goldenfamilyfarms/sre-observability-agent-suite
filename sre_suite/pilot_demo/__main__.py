"""Prepare a demo without reading credentials or changing external accounts."""

import argparse
import json
from pathlib import Path

from .prepare import finalize, materialize, prepare


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Synthetic demo and private live-pilot preparation"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("materialize")
    export.add_argument("--output", type=Path, required=True)
    plan = commands.add_parser("prepare")
    plan.add_argument("--checkout", type=Path, required=True)
    plan.add_argument("--repository", required=True)
    plan.add_argument("--repository-id", type=int, required=True)
    plan.add_argument("--pr-number", type=int, required=True)
    plan.add_argument("--ticket-key", required=True)
    plan.add_argument("--generation", type=int, default=1)
    plan.add_argument("--run-id", required=True)
    plan.add_argument("--base-branch", default="feat/demo-baseline")
    plan.add_argument("--minutes", type=int, default=120)
    plan.add_argument("--output", type=Path, required=True)
    bind = commands.add_parser("finalize-ticket")
    bind.add_argument("--prepared", type=Path, required=True)
    bind.add_argument("--evidence", type=Path, required=True)
    bind.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "materialize":
            materialize(args.output)
        elif args.command == "prepare":
            prepare(
                args.checkout,
                args.repository,
                args.repository_id,
                args.pr_number,
                args.ticket_key,
                args.generation,
                args.run_id,
                args.output,
                base_branch=args.base_branch,
                minutes=args.minutes,
            )
        else:
            finalize(args.prepared, args.evidence, args.output)
        print(
            json.dumps({"status": "prepared", "external_writes": False, "execution_started": False})
        )
    except Exception:
        parser.exit(
            1,
            "Demo preparation failed; check baseline, scope, validity window and private output.\n",
        )


if __name__ == "__main__":
    main()

"""Explicit CI build and push entrypoints. No deployment and no implicit workflow start."""

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory

from ..execution.wire import canonical
from .config import load_runtime
from .source import export


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reviewed image build and authenticated attestation"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "build", "publish"):
        p = sub.add_parser(name)
        p.add_argument("--config", required=True, type=Path)
        if name != "inspect":
            p.add_argument(
                "--authorize-" + ("build" if name == "build" else "push"), action="store_true"
            )
    args = parser.parse_args()
    try:
        agent = load_runtime(args.config)
        if args.command == "inspect":
            with TemporaryDirectory(dir=agent.journal.root) as folder:
                context = export(
                    agent.repository, agent.request, agent.stages(), Path(folder), agent.guard
                )
            result = {"build_id": agent.request.identity, "files": len(context), "writes": False}
        elif args.command == "build" and args.authorize_build:
            result = agent.build()
        elif args.command == "publish" and args.authorize_push:
            result = agent.publish()
        else:
            raise ValueError("explicit operation authorization required")
        print(canonical(result))
        return 0
    except Exception:
        # Build logs can contain private source. Keep them out of public CI summaries.
        print(canonical({"outcome": "blocked", "reason": "image_build_unavailable"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

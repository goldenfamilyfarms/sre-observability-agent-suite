"""Explicit preparation and foreground startup; no workflow, merge or deployment commands."""

import argparse
import json
import signal
from pathlib import Path

from ..verification.config import private_document
from .config import IMAGES, Config, initialize
from .lifecycle import stop_stack, up


def main() -> int:
    parser = argparse.ArgumentParser(description="Local Prometheus, Loki and Tempo pilot")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("images")
    init = commands.add_parser("init")
    init.add_argument("--directory", type=Path, required=True)
    init.add_argument("--manifest", type=Path, required=True)
    init.add_argument("--target", required=True)
    init.add_argument("--target-ca", type=Path, required=True)
    init.add_argument("--metrics-token-file", type=Path, required=True)
    init.add_argument("--mode", choices=("native", "desktop"), required=True)
    init.add_argument("--ports-file", type=Path)
    stop_command = commands.add_parser("stop")
    stop_command.add_argument("--directory", type=Path, required=True)
    stop_command.add_argument("--authorize-stop", action="store_true")
    start = commands.add_parser("up")
    start.add_argument("--directory", type=Path, required=True)
    start.add_argument("--authorize-start", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "images":
            print(json.dumps(IMAGES))
        elif args.command == "init":
            ports = private_document(args.ports_file) if args.ports_file else None
            if ports is not None and not isinstance(ports, dict):
                raise ValueError("ports must be an object")
            config = initialize(
                args.directory,
                args.manifest,
                args.target,
                args.target_ca,
                args.metrics_token_file,
                mode=args.mode,
                ports=ports,
            )
            print(
                json.dumps(
                    {
                        "status": "prepared",
                        "started": False,
                        "endpoints": {b: config.url(b) for b in IMAGES},
                    }
                )
            )
        elif args.command == "stop":
            stop_stack(args.directory, authorized=args.authorize_stop)
            print(json.dumps({"status": "stopped", "data_preserved": True}))
        else:
            stopped = False

            def stop(signum: int, frame: object) -> None:
                nonlocal stopped
                stopped = True

            for signum in (signal.SIGINT, signal.SIGTERM):
                signal.signal(signum, stop)
            up(
                args.directory,
                Config.load(args.directory),
                authorized=args.authorize_start,
                notify=lambda v: print(json.dumps(v), flush=True),
                stopping=lambda: stopped,
            )
        return 0
    except Exception:
        print(json.dumps({"status": "blocked", "reason": "local_telemetry_unavailable"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

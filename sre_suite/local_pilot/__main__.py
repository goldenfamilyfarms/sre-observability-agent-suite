"""Local pilot commands are explicit: initialize, start services, inspect, then execute."""

import argparse
import asyncio
import json
import secrets
import signal
import subprocess
import sys
from pathlib import Path

from .config import LocalConfig, execution_environment, initialize
from .lifecycle import StartupError, health, require_local_docker, up


def main() -> None:
    parser = argparse.ArgumentParser(description="Local-only SRE pilot services; no deployment")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--directory", required=True, type=Path)
    init.add_argument("--manifest", required=True, type=Path)
    init.add_argument("--postgres-image", required=True)
    init.add_argument("--temporal-binary", required=True)
    init.add_argument("--postgres-port", type=int, default=55432)
    init.add_argument("--temporal-port", type=int, default=7233)
    init.add_argument("--api-port", type=int, default=8443)
    for name in ("up", "check", "serve", "run", "handoff"):
        sub = commands.add_parser(name)
        sub.add_argument("--directory", required=True, type=Path)
        if name in {"up", "run", "handoff"}:
            sub.add_argument("--worker-env", type=Path, required=name == "handoff")
        if name == "handoff":
            sub.add_argument("--plan", type=Path, required=True)
            sub.add_argument("--publication", type=Path, required=True)
            sub.add_argument("--planning-journal", type=Path, required=True)
            sub.add_argument("--issue-type-id", required=True)
            sub.add_argument("--node", required=True)
            sub.add_argument("--run-id", required=True)
            sub.add_argument("--output", type=Path, required=True)
        if name == "check":
            sub.add_argument("--worker", action="store_true")
        if name == "run":
            sub.add_argument("execution_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.command == "init":
            initialize(
                args.directory,
                args.manifest,
                LocalConfig(
                    secrets.token_hex(10),
                    args.postgres_image,
                    args.temporal_binary,
                    args.postgres_port,
                    args.temporal_port,
                    args.api_port,
                ),
            )
            print(json.dumps({"status": "initialized", "services_started": False}))
            return
        config = LocalConfig.load(args.directory)
        if args.command == "serve":
            from .server import serve

            serve(args.directory)
        elif args.command == "check":
            result = health(args.directory, config, worker=args.worker)
            print(json.dumps({"services": result, "worker_checked": args.worker}))
            raise SystemExit(0 if all(v == "ready" for v in result.values()) else 1)
        elif args.command == "handoff":
            from ..jira_planning.handoff import handoff

            require_local_docker()
            env = execution_environment(args.directory, config, args.worker_env)
            env["SRE_JIRA_ISSUE_TYPE_ID"] = args.issue_type_id
            result = asyncio.run(
                handoff(
                    env,
                    args.plan,
                    args.publication,
                    args.planning_journal,
                    args.node,
                    args.run_id,
                    args.output,
                )
            )
            print(json.dumps(result))
            raise SystemExit(result["preflight_exit_code"])
        elif args.command == "run":
            argv = args.execution_args
            if argv[:1] == ["--"]:
                argv = argv[1:]
            if not argv or argv[0] not in {
                "pilot-setup",
                "preflight",
                "check-config",
                "worker",
                "start",
                "status",
                "result",
                "cancel",
                "export-patch",
            }:
                raise ValueError("choose an existing execution command")
            if argv[0] != "pilot-setup" and args.worker_env is None:
                raise ValueError("worker environment is required")
            if argv[0] in {"worker", "start", "preflight"}:
                require_local_docker()
            env = execution_environment(
                args.directory, config, args.worker_env, setup=argv[0] == "pilot-setup"
            )
            completed = subprocess.run(
                [sys.executable, "-m", "sre_suite.execution.cli", *argv], env=env
            )
            raise SystemExit(completed.returncode)
        else:
            stopped = False

            def stop(signum: int, frame: object) -> None:
                nonlocal stopped
                stopped = True

            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            up(
                args.directory,
                config,
                args.worker_env,
                notify=lambda value: print(json.dumps(value), flush=True),
                stopping=lambda: stopped,
            )
            print(json.dumps({"status": "stopped", "data_preserved": True}))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as error:
        phase = error.component if isinstance(error, StartupError) else "configuration"
        print(json.dumps({"status": "failed", "component": phase, "data_preserved": True}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

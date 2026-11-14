"""Bounded fixture process, with only role-specific credentials and compact stdout."""

import argparse
import os
import signal
import sys
from pathlib import Path

from ..api_wire import decode, fields
from ..execution.http import RemoteError
from ..execution.journal import Journal
from ..execution.wire import canonical
from ..telemetry.schedule_config import private_path
from ..verification.config import client
from .config import load_policy, target
from .runtime import FixtureRuntime


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("execute", "publish", "assess"))
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    args = parser.parse_args()
    signal.alarm(300)
    try:
        policy = load_policy(args.policy)
        private_path(args.journal, directory=True)
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError("fixture request budget exceeded")
        value = fields(decode(raw), {"request", "reference"})
        runtime = FixtureRuntime(
            policy,
            client(os.environ),
            Journal(args.journal),
            target=target(policy, os.environ) if args.operation == "execute" else None,
        )
        if args.operation == "execute":
            result = runtime.execute(value["request"])
        elif args.operation == "publish":
            result = runtime.publish(value["request"], value["reference"])
        else:
            result = runtime.assess(value["request"], value["reference"])
        print(canonical(result))
        return 0
    except RemoteError as error:
        return 75 if error.status in {0, 429} or error.status >= 500 else 1
    except Exception:
        return 1
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    raise SystemExit(main())

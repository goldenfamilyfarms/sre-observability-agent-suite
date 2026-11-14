"""Credential-filtered, deadline-bound managed controller/export subprocess."""

import argparse
import os
import signal
import sys
from pathlib import Path

from ..api_wire import decode
from ..execution.http import RemoteError
from ..execution.wire import canonical
from .managed import ManagedPolicy, ManagedRuntime


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("serve", "ready", "export"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    signal.alarm(60)
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError("managed request exceeds budget")
        runtime = ManagedRuntime(
            ManagedPolicy.load(args.config), decode(raw), args.root, os.environ
        )
        signal.alarm(runtime.remaining_seconds() if args.operation == "serve" else 60)
        if args.operation == "serve":
            result = runtime.serve(parent_pid=os.getppid())
        elif args.operation == "ready":
            result = runtime.ready()
        else:
            result = runtime.export()
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

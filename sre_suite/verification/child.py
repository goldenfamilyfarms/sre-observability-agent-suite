"""Killable certification worker process; reports only compact evidence references."""

import argparse
import os
import signal
import sys
from pathlib import Path

from ..api_wire import decode
from ..execution.http import RemoteError
from ..execution.journal import Journal
from ..execution.wire import canonical
from ..telemetry.schedule_config import private_path
from .config import client, load_policy
from .runtime import CertificationEngine


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    args = parser.parse_args()
    signal.alarm(300)
    try:
        policy = load_policy(args.policy)
        private_path(args.journal, directory=True)
        value = decode(sys.stdin.buffer.read(262_145))
        if len(canonical(value).encode()) > 262_144:
            raise ValueError("input budget exceeded")
        result = CertificationEngine(policy, client(os.environ), Journal(args.journal)).run(
            value, publish=True
        )
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

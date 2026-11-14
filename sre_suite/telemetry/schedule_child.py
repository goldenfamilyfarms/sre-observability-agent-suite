"""One killable scheduled occurrence; receives no Temporal, model or Jira credentials."""

import argparse
import os
import signal
import sys
from pathlib import Path

from ..api_wire import decode
from ..execution.http import RemoteError
from ..execution.journal import Journal
from ..execution.wire import canonical
from .adapters import TelemetryError
from .schedule_config import collector_from_env, load_policy, private_path
from .scheduled_runtime import ScheduledRuntime


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    args = parser.parse_args()
    # Independent ceiling: even a supervisor crash cannot leave this HTTP-only child
    # alive indefinitely. POSIX is already required by the durable journal's locks.
    signal.alarm(60)
    try:
        policy = load_policy(args.policy)
        private_path(args.journal, directory=True)
        value = decode(sys.stdin.buffer.read(65_537))
        if len(canonical(value).encode()) > 65_536:
            raise ValueError("scheduled input exceeds budget")
        runtime = ScheduledRuntime(collector_from_env(policy, os.environ), Journal(args.journal))
        print(canonical(runtime.collect(value)))
        return 0
    except RemoteError as error:
        return 75 if error.status in {0, 429} or error.status >= 500 else 1
    except TelemetryError as error:
        return (
            75
            if error.code in {"transport_error", "http_429"} or error.code.startswith("http_5")
            else 1
        )
    except Exception:
        # Intentionally no exception text or traceback crossing into activity history.
        return 1
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    raise SystemExit(main())

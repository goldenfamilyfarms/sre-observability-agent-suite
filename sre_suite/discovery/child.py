"""Credential-filtered discovery subprocess; source data never becomes a command."""

import argparse
import os
import signal
import sys
from pathlib import Path

from ..api_wire import decode, evidence_from_wire, fields
from ..execution.http import RemoteError
from ..execution.journal import Journal
from ..execution.wire import canonical
from ..verification.config import client, private_document
from .analysis import POLICY_SHA256
from .contracts import AnalysisRequest
from .runtime import DiscoveryRuntime


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("intake", "assessment", "plan"))
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--repository-dir", type=Path)
    args = parser.parse_args()
    signal.alarm(290)
    try:
        request = AnalysisRequest.parse(private_document(args.request))
        names = {"request", "policy_sha256"}
        if args.operation != "intake":
            names.add("intake")
        if args.operation == "plan":
            names.add("assessment")
        raw = fields(decode(sys.stdin.buffer.read(65537)), names)
        if (
            AnalysisRequest.parse(raw["request"]) != request
            or raw["policy_sha256"] != POLICY_SHA256
        ):
            raise ValueError("request differs from authorization")
        runtime = DiscoveryRuntime(
            request,
            client(os.environ),
            Journal(args.journal),
            "intake" if args.operation == "intake" else "assessment",
            args.repository_dir,
        )
        if args.operation == "intake":
            result = runtime.intake()
        elif args.operation == "assessment":
            result = runtime.assess(evidence_from_wire(raw["intake"]))
        else:
            result = runtime.plan(
                evidence_from_wire(raw["intake"]), evidence_from_wire(raw["assessment"])
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

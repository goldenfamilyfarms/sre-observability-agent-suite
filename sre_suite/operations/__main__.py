"""Run one privately configured analysis; no implicit environment writes."""

import argparse
import os
from pathlib import Path

from ..execution.journal import Journal
from ..execution.wire import canonical
from ..verification.config import client, private_document
from .runtime import AnalysisAgent


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Standards, topology, pipeline health and regression"
    )
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = AnalysisAgent(client(os.environ), Journal(args.journal)).run(
            private_document(args.request)
        )
        print(canonical(result))
        return 0 if result["passed"] else 2
    except Exception:
        print('{"outcome":"blocked","reason":"analysis_unavailable"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

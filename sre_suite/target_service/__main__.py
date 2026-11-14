"""Private fixture preparation and reference export; never starts a service or workflow."""

import argparse
from pathlib import Path

from ..execution.wire import canonical
from ..verification.config import private_document
from .prepare import export_reference, prepare


def main() -> int:
    parser = argparse.ArgumentParser(description="Target-service fixture package")
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export-reference")
    export.add_argument("--output", type=Path, required=True)
    profile = sub.add_parser("prepare-fixtures")
    profile.add_argument("--config", type=Path, required=True)
    profile.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "export-reference":
            export_reference(args.output)
        else:
            result = prepare(private_document(args.config, 1_048_576))
            args.output.mkdir(mode=0o700)
            for key, value in result.items():
                path = args.output / (key.replace("_", "-") + ".json")
                path.write_text(canonical(value) + "\n")
                path.chmod(0o600)
        print(canonical({"outcome": "prepared", "execution_started": False}))
        return 0
    except Exception:
        print(canonical({"outcome": "blocked", "reason": "target_preparation_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

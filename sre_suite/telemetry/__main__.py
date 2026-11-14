"""Explicit operator invocation; no workflow start, backend writes or model invocation."""

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path

from ..execution.evidence_client import EvidenceClient
from ..execution.http import RemoteError
from ..execution.wire import canonical
from ..local_pilot.config import private_json
from ..pilot_demo.prepare import publish_directory
from .adapters import TelemetryError, transport
from .collector import TelemetryCollector
from .contracts import ObservationStatus, QueryPolicy, QueryRequest


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect bounded, scoped telemetry observations")
    parser.add_argument("--policy", required=True, type=Path, help="private reviewed query policy")
    parser.add_argument("--request", required=True, type=Path, help="private scoped query request")
    parser.add_argument(
        "--output", required=True, type=Path, help="new private directory outside Git"
    )
    parser.add_argument("--upload-evidence", action="store_true", help="explicitly store evidence")
    args = parser.parse_args(argv)
    settings = os.environ if env is None else env
    try:
        policy = QueryPolicy.parse(private_json(args.policy))
        request = QueryRequest.parse(private_json(args.request))
        token = settings["SRE_EVIDENCE_API_TOKEN"]
        if not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError("invalid evidence credential")
        evidence = EvidenceClient(
            settings["SRE_EVIDENCE_API_URL"],
            token,
            local_ca_file=settings.get("SRE_EVIDENCE_CA_FILE"),
        )
        collector = TelemetryCollector(policy, evidence, lambda p: transport(p, settings))
        collection = collector.collect(request, upload_evidence=args.upload_evidence)
        files = {"observation.json": collection.content}
        if collection.evidence:
            files["evidence.json"] = (
                canonical(
                    {
                        "schema_version": 2,
                        "evidence": asdict(collection.evidence),
                    }
                )
                + "\n"
            ).encode()
        publish_directory(args.output, files)
        print(
            canonical(
                {
                    "status": collection.result.status,
                    "issues": collection.result.issues,
                    "evidence_uploaded": collection.evidence is not None,
                }
            )
        )
        # No-data and partial results are deliberately nonzero for shell automation.
        return 0 if collection.result.status == ObservationStatus.OBSERVED else 2
    except (OSError, ValueError, KeyError, RemoteError, TelemetryError):
        # Backend text, URLs, query text and credentials never go to stdout/stderr.
        print(
            "Telemetry collection failed; check private configuration and source health.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

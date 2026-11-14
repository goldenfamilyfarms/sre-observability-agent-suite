"""Prepare a reviewable full-certification bundle without starting services or workflows."""

import copy
import tempfile
from pathlib import Path
from typing import Any

from ..demo_fixture.managed import ManagedPolicy
from ..execution.certification import EngineeringCertification
from ..execution.telemetry import EngineeringTelemetry
from ..execution.wire import canonical, digest
from ..fixtures.contracts import queue
from ..local_pilot.config import write_json
from ..pilot_demo.prepare import publish_directory
from ..telemetry.contracts import integer
from ..verification.config import private_document
from .config import Config


def prepare(
    stack: Path,
    managed_policy: Path,
    output: Path,
    *,
    window_seconds: int = 30,
    demo_instrumentation: bool = False,
    collector_queue: str = "sre-local-telemetry-v1",
    verifier_queue: str = "sre-local-certification-v1",
) -> dict[str, Any]:
    """Rebind only transport fields; preserve every independently reviewed assertion/oracle.

    The caller must review the new policy fingerprints before starting a fresh run.
    Old execution plans cannot adopt this bundle or reuse its evidence.
    """
    config = Config.load(stack)
    original = private_document(managed_policy, 1_048_576)
    old = ManagedPolicy(original)
    if old.fixture.verification.scope != config.scope or old.fixture.base_url != config.target:
        raise ValueError("stack and managed policy must identify the same preview target")
    if old.certificate.read_bytes() != (stack / "target.crt").read_bytes():
        raise ValueError("stack must pin the managed target's local certificate")
    window = integer(window_seconds, 1, 300)
    if window <= old.dispatch["settle_seconds"]:
        raise ValueError("window must include fixture execution and export settlement")
    collector_queue, verifier_queue = queue(collector_queue), queue(verifier_queue)
    queues = {
        collector_queue,
        verifier_queue,
        old.dispatch["task_queue"],
        old.fixture.runner_queue,
        old.fixture.assessor_queue,
    }
    if len(queues) != 5:
        raise ValueError("all five telemetry/fixture/certification roles require separate queues")
    managed = copy.deepcopy(original)
    assert isinstance(managed, dict)
    if demo_instrumentation:
        from .demo_policy import reviewed

        managed = reviewed(managed)
    verification = managed["fixture_policy"]["verification_policy"]
    for source in verification["sources"]:
        profile = source["profile"]
        backend = profile["backend"]
        profile.update(
            base_url=config.url(backend),
            authorization_env="SRE_TELEMETRY_" + backend.upper() + "_AUTH",
            tenant_env=None,
            ca_env="SRE_TELEMETRY_CA_FILE",
        )
    managed["export"] = {
        signal: {
            "base_url": config.url(backend),
            "authorization_env": "SRE_DEMO_" + signal.upper() + "_AUTH",
            "tenant_env": None,
            "ca_env": "SRE_DEMO_EXPORT_CA_FILE",
        }
        for signal, backend in (("logs", "loki"), ("traces", "tempo"))
    }
    rebound = ManagedPolicy(managed)
    queries = {
        "schema_version": 2,
        "scope": verification["scope"],
        "profiles": [s["profile"] for s in verification["sources"]],
    }
    telemetry = {
        "schema_version": 2,
        "policy": queries,
        "profiles": [
            {
                "profile": s["profile"]["id"],
                "trace_id": s["trace_id"],
                "step_seconds": s["step_seconds"],
                "limit": s["limit"],
            }
            for s in verification["sources"]
        ],
        "task_queue": collector_queue,
        "producer": verification["collector"],
        "delay_seconds": rebound.dispatch["startup_seconds"],
        "window_seconds": window,
    }
    certification = {
        "schema_version": 2,
        "policy": verification,
        "task_queue": verifier_queue,
        "fixture_wait_seconds": 120,
        "fixture": rebound.fixture.dispatch,
        "demo": rebound.dispatch,
    }
    # Validate the exact paired files with the normal runtime parsers before atomic publication.
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        write_json(root / "telemetry.json", telemetry)
        write_json(root / "certification.json", certification)
        parsed = EngineeringTelemetry(root / "telemetry.json")
        cert = EngineeringCertification(root / "certification.json", parsed)
    summary = {
        "schema_version": 2,
        "stack_identity": config.identity,
        "source_managed_policy_sha256": digest(original),
        "verification_policy_sha256": rebound.fixture.verification.fingerprint,
        "fixture_policy_sha256": rebound.fixture.fingerprint,
        "managed_policy_sha256": rebound.dispatch["policy_sha256"],
        "engineering_telemetry_sha256": parsed.fingerprint,
        "engineering_certification_sha256": cert.fingerprint,
        "checks": len(rebound.fixture.verification.rules),
        "sources": len(rebound.fixture.verification.sources),
        "execution_started": False,
    }
    documents = {
        "verification-policy.json": verification,
        "fixture-policy.json": managed["fixture_policy"],
        "managed-demo.json": managed,
        "query-policy.json": queries,
        "engineering-telemetry.json": telemetry,
        "engineering-certification.json": certification,
        "engineering-env.json": {
            "SRE_ENGINEERING_TELEMETRY_CONFIG": str(output / "engineering-telemetry.json"),
            "SRE_ENGINEERING_CERTIFICATION_CONFIG": str(output / "engineering-certification.json"),
        },
        "review.json": summary,
    }
    publish_directory(output, {k: (canonical(v) + "\n").encode() for k, v in documents.items()})
    return summary

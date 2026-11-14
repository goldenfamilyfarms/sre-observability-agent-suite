"""Private worker configuration; telemetry credentials never enter workflow payloads."""

from collections.abc import Mapping
from pathlib import Path

from ..execution.evidence_client import EvidenceClient
from ..execution.pilot import _private
from ..local_pilot.config import private_json
from .adapters import transport
from .collector import TelemetryCollector
from .contracts import QueryPolicy


def private_path(path: Path, *, directory: bool) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("use physical absolute private paths")
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink() or (ancestor / ".git").exists():
            raise ValueError("private scheduler state must remain outside Git and symlinks")
    _private(path, directory=directory)


def load_policy(path: Path) -> QueryPolicy:
    private_path(path, directory=False)
    return QueryPolicy.parse(private_json(path))


def child_environment(policy: QueryPolicy, env: Mapping[str, str]) -> dict[str, str]:
    keys = {
        "PATH",
        "LANG",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "SRE_EVIDENCE_API_URL",
        "SRE_EVIDENCE_API_TOKEN",
        "SRE_EVIDENCE_CA_FILE",
    }
    for profile in policy.profiles:
        keys.add(profile.authorization_env)
        if profile.tenant_env:
            keys.add(profile.tenant_env)
    return {k: v for k, v in env.items() if k in keys}


def collector_from_env(policy: QueryPolicy, env: Mapping[str, str]) -> TelemetryCollector:
    token = env["SRE_EVIDENCE_API_TOKEN"]
    if not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError("invalid evidence credential")
    evidence = EvidenceClient(
        env["SRE_EVIDENCE_API_URL"], token, local_ca_file=env.get("SRE_EVIDENCE_CA_FILE")
    )
    return TelemetryCollector(policy, evidence, lambda p: transport(p, env))

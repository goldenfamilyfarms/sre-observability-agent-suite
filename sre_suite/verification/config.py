"""Private policy loading and minimal evidence-only process environment."""

from collections.abc import Mapping
from pathlib import Path

from ..api_wire import decode
from ..execution.evidence_client import EvidenceClient
from ..execution.pilot import _read
from ..telemetry.schedule_config import private_path
from .contracts import VerificationPolicy


def load_policy(path: Path) -> VerificationPolicy:
    return VerificationPolicy.parse(private_document(path, 1_048_576))


def environment(env: Mapping[str, str]) -> dict[str, str]:
    return {
        k: v
        for k, v in env.items()
        if k
        in {
            "PATH",
            "LANG",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "SRE_EVIDENCE_API_URL",
            "SRE_EVIDENCE_API_TOKEN",
            "SRE_EVIDENCE_CA_FILE",
        }
    }


def client(env: Mapping[str, str]) -> EvidenceClient:
    token = env["SRE_EVIDENCE_API_TOKEN"]
    if not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError("invalid evidence credential")
    return EvidenceClient(
        env["SRE_EVIDENCE_API_URL"], token, local_ca_file=env.get("SRE_EVIDENCE_CA_FILE")
    )


def private_document(path: Path, maximum: int = 262_144) -> object:
    private_path(path, directory=False)
    return decode(_read(path, maximum))

"""Optional local supervisor registration for a separately credentialed lifecycle worker."""

import sys
from pathlib import Path

from ..api_wire import text
from ..demo_fixture.managed import ManagedPolicy
from ..telemetry.schedule_config import private_path
from ..verification.config import private_document
from .config import LocalConfig, base_environment

KEYS = {
    "SRE_EVIDENCE_API_TOKEN",
    "SRE_DEMO_FIXTURE_AUTH",
    "SRE_DEMO_METRICS_AUTH",
    "SRE_DEMO_LIFECYCLE_AUTH",
    "SRE_DEMO_TRACES_AUTH",
    "SRE_DEMO_LOGS_AUTH",
    "SRE_DEMO_TRACES_TENANT",
    "SRE_DEMO_LOGS_TENANT",
    "SRE_DEMO_EXPORT_CA_FILE",
}


def prepare(
    root: Path, stack: LocalConfig, config: Path, credentials: Path, authorized: bool
) -> tuple[list[str], dict[str, str], str]:
    if not authorized:
        raise ValueError("managed demo startup and export require explicit authorization")
    policy = ManagedPolicy.load(config)
    if policy.port in {stack.postgres_port, stack.temporal_port, stack.api_port}:
        raise ValueError("managed target port conflicts with the local stack")
    raw = private_document(credentials, 65536)
    if (
        not isinstance(raw, dict)
        or set(raw) - KEYS
        or not {
            "SRE_EVIDENCE_API_TOKEN",
            "SRE_DEMO_FIXTURE_AUTH",
            "SRE_DEMO_METRICS_AUTH",
            "SRE_DEMO_LIFECYCLE_AUTH",
            "SRE_DEMO_TRACES_AUTH",
            "SRE_DEMO_LOGS_AUTH",
        }
        <= raw.keys()
    ):
        raise ValueError("supply only the explicit demo role credentials")
    env = {k: v for k, v in base_environment().items() if k in {"PATH", "LANG"}}
    for key, value in raw.items():
        item = text(value, 8192)
        if any(ord(c) < 32 or ord(c) > 126 for c in item):
            raise ValueError("credentials must be bounded single-line values")
        env[key] = item
    env.update(
        SRE_EVIDENCE_API_URL=stack.url,
        SRE_EVIDENCE_CA_FILE=str(root / "server.crt"),
        SRE_TEMPORAL_ADDRESS=f"127.0.0.1:{stack.temporal_port}",
        SRE_TEMPORAL_NAMESPACE=stack.name,
        SRE_TEMPORAL_TLS="false",
    )
    state = root / "demo-state"
    state.mkdir(mode=0o700, exist_ok=True)
    private_path(state, directory=True)
    argv = [
        sys.executable,
        "-m",
        "sre_suite.demo_fixture.lifecycle",
        "worker",
        "--config",
        str(config),
        "--root",
        str(state),
        "--authorize-targets",
        "--authorize-export",
    ]
    return argv, env, str(policy.dispatch["task_queue"])

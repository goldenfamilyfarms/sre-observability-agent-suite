"""Private local stack identity, scoped credentials and deterministic process environment."""

import os
import re
import secrets
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..api_wire import decode, envelope, fields, text
from ..auth import WORKER_ACTIONS, Action, Credential, Grant, TokenAuthenticator, token_digest
from ..execution.pilot import PilotManifest, _destination, _private, _read, _sync_directory
from ..execution.wire import canonical
from ..serialization import scope_from_dict

AUDIENCE = "sre-local-pilot"


def private_json(path: Path) -> object:
    _private(path, directory=False)
    return decode(_read(path, 65_536))


def write_private(path: Path, content: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())


def write_json(path: Path, value: object) -> None:
    write_private(path, (canonical(value) + "\n").encode())


@dataclass(frozen=True)
class LocalConfig:
    identity: str
    postgres_image: str
    temporal_binary: str
    postgres_port: int = 55432
    temporal_port: int = 7233
    api_port: int = 8443

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{20}", self.identity):
            raise ValueError("invalid local stack identity")
        if not re.fullmatch(r"postgres@sha256:[0-9a-f]{64}", self.postgres_image):
            raise ValueError("supply an explicitly pulled PostgreSQL 16/17 image digest")
        binary = Path(self.temporal_binary)
        if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError("supply an installed Temporal CLI executable")
        ports = (self.postgres_port, self.temporal_port, self.api_port)
        if any(type(p) is not int or not 1024 <= p <= 65535 for p in ports) or len(set(ports)) != 3:
            raise ValueError("local services require three distinct unprivileged ports")

    @property
    def name(self) -> str:
        return "sre-local-" + self.identity

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.api_port}"

    @classmethod
    def load(cls, root: Path) -> "LocalConfig":
        # Resolve only after rejecting aliases, writable/shared roots and repository paths.
        for path in (root, *root.parents):
            if path.is_symlink() or (path / ".git").exists():
                raise ValueError("pilot directory must be outside checkouts and symlinks")
        if not root.is_absolute() or ".." in root.parts or "," in str(root):
            raise ValueError("use a physical absolute pilot directory")
        _private(root, directory=True)
        for directory in ("journal", "temporal"):
            _private(root / directory, directory=True)
        for name in (
            "server.crt",
            "server.key",
            "postgres.password",
            "controller.token",
            "worker.token",
        ):
            _private(root / name, directory=False)
        raw = envelope(
            private_json(root / "stack.json"),
            {
                "identity",
                "postgres_image",
                "temporal_binary",
                "postgres_port",
                "temporal_port",
                "api_port",
            },
        )
        values: dict[str, Any] = {k: v for k, v in raw.items() if k != "schema_version"}
        return cls(**values)


def credential_payload(manifest: PilotManifest, controller: str, worker: str) -> dict[str, object]:
    preview = manifest.preview
    credentials = []
    for subject, token, actions in (
        ("pilot-controller", controller, (Action.PREVIEW_REGISTER,)),
        ("pilot-worker", worker, WORKER_ACTIONS),
    ):
        credentials.append(
            {
                "subject": subject,
                "token_sha256": token_digest(token),
                "audience": AUDIENCE,
                "not_before": preview.requested_at.isoformat(),
                "expires_at": preview.expires_at.isoformat(),
                "grants": [
                    {"scope": asdict(preview.scope), "action": action.value} for action in actions
                ],
            }
        )
    return {"schema_version": 2, "credentials": credentials}


def load_credentials(path: Path) -> tuple[Credential, ...]:
    raw = envelope(private_json(path), {"credentials"})
    items = raw["credentials"]
    if not isinstance(items, list) or not 0 <= len(items) <= 100:
        raise ValueError("invalid credential list")
    credentials = []
    for value in items:
        item = fields(
            value, {"subject", "token_sha256", "audience", "not_before", "expires_at", "grants"}
        )
        grants = item["grants"]
        if not isinstance(grants, list) or not 1 <= len(grants) <= 100:
            raise ValueError("invalid grants")
        decoded = []
        for grant in grants:
            row = fields(grant, {"scope", "action"})
            scope = scope_from_dict(row["scope"])
            if scope.environment.value == "production":
                raise ValueError("local pilot cannot authorize production")
            decoded.append(Grant(scope, Action(text(row["action"]))))
        credentials.append(
            Credential(
                text(item["subject"]),
                text(item["token_sha256"]),
                text(item["audience"]),
                datetime.fromisoformat(text(item["not_before"])),
                datetime.fromisoformat(text(item["expires_at"])),
                frozenset(decoded),
            )
        )
    result = tuple(credentials)
    TokenAuthenticator(AUDIENCE, result)  # Reject duplicates before accepting a reload.
    if any(c.audience != AUDIENCE for c in result):
        raise ValueError("unexpected audience")
    return result


def initialize(root: Path, manifest_path: Path, config: LocalConfig) -> None:
    """Explicit operator authorization of one manifest scope; no network or service startup."""
    import tempfile

    if not root.is_absolute() or "," in str(root):
        raise ValueError("use a physical absolute directory without commas")
    manifest_bytes = _read(manifest_path, 32_768)
    manifest = PilotManifest.parse(decode(manifest_bytes), datetime.now(UTC))
    # Reuse the private-parent/path and lock checks, but never overwrite an initialized stack.
    with _destination(root, "new-local-stack") as target:
        if target.exists():
            raise ValueError("pilot already initialized")
        with tempfile.TemporaryDirectory(prefix=".local-init-", dir=target.parent) as directory:
            bundle = Path(directory) / "bundle"
            bundle.mkdir(mode=0o700)
            controller, worker = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            write_json(bundle / "stack.json", {"schema_version": 2, **asdict(config)})
            write_json(
                bundle / "credentials.json", credential_payload(manifest, controller, worker)
            )
            write_private(bundle / "controller.token", controller.encode())
            write_private(bundle / "worker.token", worker.encode())
            write_private(bundle / "postgres.password", secrets.token_urlsafe(32).encode())
            write_private(bundle / "manifest.json", manifest_bytes)
            (bundle / "journal").mkdir(mode=0o700)
            (bundle / "temporal").mkdir(mode=0o700)
            openssl = bundle / "openssl.cnf"
            write_private(
                openssl,
                b"[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n",
            )
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-days",
                    "7",
                    "-keyout",
                    str(bundle / "server.key"),
                    "-out",
                    str(bundle / "server.crt"),
                    "-config",
                    str(openssl),
                ],
                check=True,
                capture_output=True,
                timeout=30,
                env=base_environment(),
            )
            for name in ("server.key", "server.crt"):
                (bundle / name).chmod(0o600)
            _sync_directory(bundle)
            bundle.rename(target)
            _sync_directory(target.parent)


def token(root: Path, name: str) -> str:
    path = root / name
    _private(path, directory=False)
    value = _read(path, 128).decode("ascii")
    token_digest(value)
    return value


def dsn(root: Path, config: LocalConfig) -> str:
    password = token(root, "postgres.password")
    return f"postgresql://sre_pilot:{password}@127.0.0.1:{config.postgres_port}/sre_pilot"


def base_environment() -> dict[str, str]:
    # Never inherit Jira/model tokens, arbitrary SRE_ settings or Python injection settings.
    return {
        key: value
        for key, value in os.environ.items()
        if key
        in {"PATH", "HOME", "LANG", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"}
    }


WORKER_KEYS = frozenset(
    {
        "SRE_JIRA_BASE_URL",
        "SRE_JIRA_EMAIL",
        "SRE_JIRA_API_TOKEN",
        "SRE_JIRA_PROJECT",
        "SRE_JIRA_CONTRACT_FIELD",
        "SRE_JIRA_DONE_STATUSES",
        "SRE_JIRA_WRITES_ENABLED",
        "SRE_REPOSITORY",
        "SRE_REPO_DIR",
        "SRE_ALLOWED_FILES_JSON",
        "SRE_TEST_IMAGE",
        "SRE_TEST_COMMAND_JSON",
        "SRE_WORKER_TIMEOUT_SECONDS",
        "SRE_CLAUDE_BINARY",
        "SRE_CLAUDE_MODEL",
        "ANTHROPIC_API_KEY",
        "SRE_CLAUDE_ENABLED",
        "SRE_ENGINEERING_TELEMETRY_CONFIG",
        "SRE_ENGINEERING_CERTIFICATION_CONFIG",
        "SRE_CLAUDE_BUDGET_USD",
        "SRE_GITHUB_PRS_ENABLED",
        "SRE_GITHUB_TOKEN",
        "SRE_GITHUB_BASE_BRANCH",
        "SRE_JIRA_TRANSITIONS_ENABLED",
        "SRE_JIRA_TRANSITIONS_JSON",
    }
)


def execution_environment(
    root: Path, config: LocalConfig, worker_file: Path | None, *, setup: bool = False
) -> dict[str, str]:
    env = base_environment()
    if worker_file is not None and not setup:
        raw = private_json(worker_file)
        if not isinstance(raw, dict) or set(raw) - WORKER_KEYS:
            raise ValueError("worker file contains unknown or stack-owned settings")
        for key, value in raw.items():
            if not isinstance(value, str) or "\n" in value or "\r" in value:
                raise ValueError("worker settings must be single-line strings")
            env[key] = value
    env.update(
        {
            "SRE_EVIDENCE_API_URL": config.url,
            "SRE_EVIDENCE_API_TOKEN": token(root, "worker.token"),
            "SRE_EVIDENCE_CA_FILE": str(root / "server.crt"),
            "SRE_WORK_ROOT": str(root / "journal"),
            "SRE_TEMPORAL_ADDRESS": f"127.0.0.1:{config.temporal_port}",
            "SRE_TEMPORAL_NAMESPACE": config.name,
            "SRE_TEMPORAL_TASK_QUEUE": config.name,
            "SRE_TEMPORAL_TLS": "false",
        }
    )
    if setup:
        env["SRE_PILOT_SETUP_TOKEN"] = token(root, "controller.token")
    return env

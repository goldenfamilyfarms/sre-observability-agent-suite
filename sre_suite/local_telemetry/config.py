"""Private deterministic configuration for a trusted single-host telemetry pilot."""

import re
import secrets
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..api_wire import envelope, fields, text
from ..contracts import Environment, Scope
from ..demo_fixture.server import credential
from ..execution.config import https_url
from ..execution.pilot import PilotManifest, _read
from ..local_pilot.config import base_environment
from ..pilot_demo.prepare import publish_directory
from ..serialization import scope_from_dict
from ..telemetry.contracts import integer
from ..telemetry.schedule_config import private_path
from ..verification.config import private_document
from .recording import recording_rules

IMAGES = {
    "prometheus": (
        "prom/prometheus@sha256:5ce7540c3c00ef4ab0c9d2c995c6a5b9c421f44b4a115d97a2c7af3b1c21cbb0"
    ),
    "loki": "grafana/loki@sha256:847c287ada0e12603910589f42038c5cdaaad04e248bd1dc6c6e0920a235f427",
    "tempo": (
        "grafana/tempo@sha256:785fd6c81d7f26ff0973208cd7093ac6030e13946c288612ff32f02416941d3a"
    ),
}
DEFAULT_PORTS = dict(
    zip(
        (
            "prometheus",
            "loki",
            "tempo",
            "otlp",
            "loki_grpc",
            "tempo_grpc",
            "memberlist",
            "prometheus_tls",
            "loki_tls",
            "tempo_tls",
        ),
        (19090, 13100, 13200, 14318, 19096, 19095, 17946, 19443, 19444, 19445),
        strict=True,
    )
)


@dataclass(frozen=True)
class Config:
    identity: str
    scope: Scope
    mode: str
    target: str
    ports: dict[str, int]

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{20}", self.identity) or self.mode not in {
            "native",
            "desktop",
        }:
            raise ValueError("invalid telemetry stack identity or networking mode")
        if self.scope.environment == Environment.PRODUCTION:
            raise ValueError("local telemetry cannot serve production")
        https_url(self.target, allow_loopback=True)
        origin = urlsplit(self.target)
        if origin.hostname not in {"localhost", "127.0.0.1"} or origin.path or origin.port is None:
            raise ValueError("target must be an exact loopback HTTPS origin")
        if (
            set(self.ports) != set(DEFAULT_PORTS)
            or any(type(p) is not int or not 1024 <= p <= 65535 for p in self.ports.values())
            or len(set(self.ports.values())) != len(DEFAULT_PORTS)
            or origin.port in self.ports.values()
        ):
            raise ValueError("distinct unprivileged telemetry and target ports required")

    @property
    def name(self) -> str:
        return "sre-telemetry-" + self.identity

    def url(self, backend: str) -> str:
        return f"https://localhost:{self.ports[backend + '_tls']}"

    @classmethod
    def load(cls, root: Path) -> "Config":
        private_path(root, directory=True)
        raw = envelope(
            private_document(root / "stack.json"),
            {"identity", "scope", "mode", "target", "ports", "images"},
        )
        if raw["images"] != IMAGES:
            raise ValueError("image versions changed; review retained state migration explicitly")
        for name in ("server.crt", "server.key", "target.crt", "metrics.token", "tokens.json"):
            private_path(root / name, directory=False)
        ports = fields(raw["ports"], set(DEFAULT_PORTS))
        config = cls(
            text(raw["identity"]),
            scope_from_dict(raw["scope"]),
            text(raw["mode"]),
            text(raw["target"]),
            {k: integer(v, 1024, 65535) for k, v in ports.items()},
        )

        for backend, expected in configurations(config).items():
            if private_document(root / (backend + ".json")) != expected:
                raise ValueError("backend configuration differs from the generated stack")
        if private_document(root / "recording.json") != recording_rules():
            raise ValueError("recording rules differ from reviewed calculations")
        return config


def initialize(
    root: Path,
    manifest: Path,
    target: str,
    ca: Path,
    metrics_token: Path,
    *,
    mode: str,
    ports: dict[str, int] | None = None,
) -> Config:
    import tempfile

    value = PilotManifest.parse(private_document(manifest), datetime.now(UTC))
    config = Config(
        secrets.token_hex(10),
        value.preview.scope,
        mode,
        target,
        DEFAULT_PORTS.copy() if ports is None else ports,
    )
    for path in (ca, metrics_token):
        private_path(path, directory=False)
    metrics = _read(metrics_token, 256).decode().strip()
    credential(metrics)
    # Validate the CA before creating a private bundle.
    import ssl

    ssl.create_default_context(cafile=str(ca))
    tokens = {
        role: "Bearer " + secrets.token_urlsafe(32)
        for role in ("prometheus_read", "loki_read", "tempo_read", "logs_write", "traces_write")
    }
    from ..execution.wire import canonical

    files = {
        "stack.json": (
            canonical({"schema_version": 2, **asdict(config), "images": IMAGES}) + "\n"
        ).encode(),
        "tokens.json": (canonical(tokens) + "\n").encode(),
        "target.crt": _read(ca, 65536),
        "metrics.token": metrics[7:].encode(),
        "loki-read.token": tokens["loki_read"][7:].encode(),
        "recording.json": (canonical(recording_rules()) + "\n").encode(),
    }
    # Atomic publication keeps generated private credentials out of partially initialized stacks.
    with tempfile.TemporaryDirectory() as temporary:
        temp = Path(temporary)
        openssl = temp / "openssl.cnf"
        openssl.write_text(
            "[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n"
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
                str(temp / "server.key"),
                "-out",
                str(temp / "server.crt"),
                "-config",
                str(openssl),
            ],
            check=True,
            capture_output=True,
            timeout=30,
            env=base_environment(),
        )
        for name in ("server.crt", "server.key"):
            files[name] = (temp / name).read_bytes()

    def add(path: Path, value: object) -> None:
        files[path.name] = (canonical(value) + "\n").encode()

    add(
        root / "collector-env.json",
        {
            **{"SRE_TELEMETRY_" + b.upper() + "_AUTH": tokens[b + "_read"] for b in IMAGES},
            "SRE_TELEMETRY_CA_FILE": str(root / "server.crt"),
        },
    )
    add(
        root / "export-env.json",
        {
            "SRE_DEMO_LOGS_AUTH": tokens["logs_write"],
            "SRE_DEMO_TRACES_AUTH": tokens["traces_write"],
            "SRE_DEMO_EXPORT_CA_FILE": str(root / "server.crt"),
        },
    )
    add(
        root / "export.json",
        {
            s: {
                "base_url": config.url(b),
                "authorization_env": "SRE_DEMO_" + s.upper() + "_AUTH",
                "tenant_env": None,
                "ca_env": "SRE_DEMO_EXPORT_CA_FILE",
            }
            for s, b in (("logs", "loki"), ("traces", "tempo"))
        },
    )
    add(root / "query-policy.json", query_policy(config))
    for backend, document in configurations(config).items():
        add(root / (backend + ".json"), document)
    for backend in IMAGES:
        files[backend + "-data/.keep"] = b""
    publish_directory(root, files)
    return config


def configurations(c: Config) -> dict[str, Any]:
    p = c.ports
    bind = "127.0.0.1" if c.mode == "native" else "0.0.0.0"
    host = "127.0.0.1" if c.mode == "native" else "host.docker.internal"
    return {
        "prometheus": {
            "global": {"scrape_interval": "1s", "scrape_timeout": "900ms"},
            "rule_files": ["/config/recording.json"],
            "scrape_configs": [
                {
                    "job_name": "sre-demo",
                    "sample_limit": 500,
                    "scheme": "https",
                    "metrics_path": "/metrics",
                    "authorization": {"credentials_file": "/config/metrics.token"},
                    "tls_config": {"ca_file": "/config/target.crt", "server_name": "localhost"},
                    "static_configs": [{"targets": [f"{host}:{urlsplit(c.target).port}"]}],
                },
                {
                    "job_name": "sre-loki-policy",
                    "scheme": "https",
                    "metrics_path": "/metrics",
                    "authorization": {"credentials_file": "/config/loki-read.token"},
                    "tls_config": {"ca_file": "/config/gateway.crt", "server_name": "localhost"},
                    "static_configs": [{"targets": [f"{host}:{p['loki_tls']}"]}],
                },
            ],
        },
        "loki": {
            "auth_enabled": False,
            "frontend": {"address": "127.0.0.1"},
            "server": {
                "http_listen_address": bind,
                "http_listen_port": p["loki"],
                "grpc_listen_address": "127.0.0.1",
                "grpc_listen_port": p["loki_grpc"],
            },
            "common": {
                "path_prefix": "/data",
                "replication_factor": 1,
                "ring": {"instance_addr": "127.0.0.1", "kvstore": {"store": "inmemory"}},
                "storage": {
                    "filesystem": {
                        "chunks_directory": "/data/chunks",
                        "rules_directory": "/data/rules",
                    }
                },
            },
            "schema_config": {
                "configs": [
                    {
                        "from": "2024-01-01",
                        "store": "tsdb",
                        "object_store": "filesystem",
                        "schema": "v13",
                        "index": {"prefix": "index_", "period": "24h"},
                    }
                ]
            },
            "limits_config": {
                "max_query_lookback": "24h",
                "reject_old_samples_max_age": "24h",
                "retention_period": "24h",
            },
            "compactor": {
                "working_directory": "/data/compactor",
                "retention_enabled": True,
                "delete_request_store": "filesystem",
            },
            "analytics": {"reporting_enabled": False},
        },
        "tempo": {
            "server": {
                "http_listen_address": bind,
                "http_listen_port": p["tempo"],
                "grpc_listen_address": "127.0.0.1",
                "grpc_listen_port": p["tempo_grpc"],
            },
            "distributor": {
                "receivers": {"otlp": {"protocols": {"http": {"endpoint": f"{bind}:{p['otlp']}"}}}}
            },
            "memberlist": {"bind_addr": ["127.0.0.1"], "bind_port": p["memberlist"]},
            "querier": {"frontend_worker": {"frontend_address": f"127.0.0.1:{p['tempo_grpc']}"}},
            "ingester": {
                "trace_idle_period": "1s",
                "max_block_duration": "5m",
                "lifecycler": {"address": "127.0.0.1"},
            },
            "storage": {
                "trace": {
                    "backend": "local",
                    "wal": {"path": "/data/wal"},
                    "local": {"path": "/data/blocks"},
                }
            },
            "compactor": {"compaction": {"block_retention": "24h"}},
            "usage_report": {"reporting_enabled": False},
        },
    }


def query_policy(config: Config) -> dict[str, Any]:
    """A narrow journey query policy, not a 120-check certification policy."""
    return {
        "schema_version": 2,
        "scope": asdict(config.scope),
        "profiles": [
            {
                "id": backend,
                "backend": backend,
                "base_url": config.url(backend),
                "authorization_env": "SRE_TELEMETRY_" + backend.upper() + "_AUTH",
                "tenant_env": None,
                "ca_env": "SRE_TELEMETRY_CA_FILE",
                "query_template": "sre_demo_requests_total${scope}"
                if backend == "prometheus"
                else "${scope}",
                "allow_full_traces": backend == "tempo",
                "scope_labels": {
                    "service": "service_name",
                    "environment": "deployment_environment",
                    "revision": "service_version",
                    "environment_id": "preview_id",
                    "generation": "preview_generation",
                },
                "binding_labels": {
                    "artifact_digest": "artifact_digest",
                    "plan_digest": "plan_digest",
                },
            }
            for backend in IMAGES
        ],
    }

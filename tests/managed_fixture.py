"""Synthetic private managed policy layered on the concrete demo fixture."""

import copy
from pathlib import Path
from unittest.mock import patch

from sre_suite.demo_fixture.managed import ManagedPolicy, ManagedRuntime
from sre_suite.execution.wire import canonical
from tests.demo_fixture import DemoFixture


class ManagedFixture:
    def __init__(self, root, *, certificate=None, key=None, port=9443, export=None, startup=5):
        self.root = Path(root)
        self.d = DemoFixture(
            self.root,
            target={
                "base_url": f"https://localhost:{port}",
                "authorization_env": "SRE_FIXTURE_TARGET_AUTH",
                "ca_env": "SRE_FIXTURE_TARGET_CA_FILE",
            },
        )
        if certificate is None:
            certificate, key = self.root / "server.crt", self.root / "server.key"
            for path in (certificate, key):
                path.write_text("synthetic; TLS is not opened in unit tests")
                path.chmod(0o600)
        self.config = {
            "schema_version": 2,
            "fixture_policy": self.d.raw,
            "recipe": self.d.recipe,
            "task_queue": "demo-lifecycle",
            "startup_seconds": startup,
            "settle_seconds": 1,
            "certificate": str(certificate),
            "private_key": str(key),
            "export": export
            or {
                s: {
                    "base_url": f"https://{s}.example.invalid",
                    "authorization_env": f"SRE_DEMO_{s.upper()}_AUTH",
                    "tenant_env": None,
                    "ca_env": None,
                }
                for s in ("logs", "traces")
            },
        }
        self.policy = ManagedPolicy(self.config)
        self.path = self.root / "managed.json"
        self.path.write_text(canonical(self.config))
        self.path.chmod(0o600)
        self.request = copy.deepcopy(self.d.request)
        self.request["plan"]["demo"] = self.policy.dispatch
        self.env = {
            "SRE_EVIDENCE_API_URL": "https://evidence.example.invalid",
            "SRE_EVIDENCE_API_TOKEN": "synthetic",
            "SRE_DEMO_FIXTURE_AUTH": "Bearer fixture-" + "a" * 32,
            "SRE_DEMO_METRICS_AUTH": "Bearer metrics-" + "b" * 32,
            "SRE_DEMO_LIFECYCLE_AUTH": "Bearer lifecycle-" + "c" * 32,
            "SRE_DEMO_TRACES_AUTH": "Bearer traces",
            "SRE_DEMO_LOGS_AUTH": "Bearer logs",
        }
        self.state = self.root / "managed-state"
        self.state.mkdir(mode=0o700)
        self.runtime = self.reopen()
        self.d.controller = self.runtime.controller

    def reopen(self):
        with patch("sre_suite.demo_fixture.managed.client", return_value=self.d.f.api):
            return ManagedRuntime(
                self.policy, self.request, self.state, self.env, clock=lambda: self.d.f.now
            )

    def execute(self):
        return self.d.runner.execute(self.request)

    def close(self):
        self.d.close()

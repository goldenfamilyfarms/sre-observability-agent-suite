"""Offline local telemetry configuration, ownership, TLS policy and authorization tests."""

import json
from dataclasses import asdict, replace
from unittest.mock import patch

from sre_suite.contracts import Environment
from sre_suite.local_pilot.lifecycle import StartupError
from sre_suite.local_telemetry.config import (
    IMAGES,
    Config,
    configurations,
    initialize,
)
from sre_suite.local_telemetry.gateway import route
from sre_suite.local_telemetry.lifecycle import arguments, cleanup, owned, running, stop_stack
from sre_suite.telemetry.adapters import TelemetryError, transport
from sre_suite.telemetry.contracts import Backend, QueryPolicy, profile_document
from sre_suite.telemetry.schedule_config import child_environment
from tests.test_local_pilot import LocalFixture
from tests.test_telemetry import profile


class LocalTelemetryTests(LocalFixture):
    def setUp(self):
        super().setUp()
        self.initialize()
        self.manifest_path.chmod(0o600)
        self.metrics = self.root / "metrics-token"
        self.metrics.write_text("Bearer " + "m" * 40)
        self.metrics.chmod(0o600)
        self.target = self.root / "telemetry"

    def prepare(self):
        return initialize(
            self.target,
            self.manifest_path,
            "https://localhost:9443",
            self.stack / "server.crt",
            self.metrics,
            mode="desktop",
        )

    def test_init_private_exclusive_and_independent_role_credentials(self):
        with patch("sre_suite.local_telemetry.lifecycle.command") as docker:
            c = self.prepare()
        docker.assert_not_called()
        self.assertEqual(Config.load(self.target), c)
        parsed = QueryPolicy.parse(json.loads((self.target / "query-policy.json").read_text()))
        self.assertEqual(parsed.scope, c.scope)
        self.assertEqual(len(parsed.profiles), 3)
        tokens = json.loads((self.target / "tokens.json").read_text())
        self.assertEqual(len(set(tokens.values())), 5)
        for p in self.target.iterdir():
            self.assertEqual(p.stat().st_mode & 0o077, 0)
        before = (self.target / "tokens.json").read_bytes()
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual((self.target / "tokens.json").read_bytes(), before)

    def test_port_scope_and_origin_validation(self):
        c = self.prepare()
        for values in (
            {"scope": replace(c.scope, environment=Environment.PRODUCTION)},
            {"ports": {**c.ports, "loki": c.ports["tempo"]}},
            {"ports": {**c.ports, "loki": True}},
            {"ports": {**c.ports, "loki": 9443}},
            {"target": "https://remote.example.invalid:9443"},
            {"target": "http://localhost:9443"},
            {"mode": "remote"},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                replace(c, **values)

    def test_direct_scrape_tls_identity_no_insecure_fallback(self):
        c = self.prepare()
        for mode, hostname in (("desktop", "host.docker.internal"), ("native", "127.0.0.1")):
            docs = configurations(replace(c, mode=mode))
            scrape = docs["prometheus"]["scrape_configs"][0]
            self.assertEqual(scrape["static_configs"][0]["targets"], [hostname + ":9443"])
            self.assertEqual(
                scrape["tls_config"], {"ca_file": "/config/target.crt", "server_name": "localhost"}
            )
            self.assertEqual(scrape["authorization"], {"credentials_file": "/config/metrics.token"})
            self.assertNotIn("insecure", json.dumps(docs))

    def test_container_args_are_pinned_loopback_and_least_mounts(self):
        c = self.prepare()
        for backend in IMAGES:
            argv = arguments(self.target, c, backend)
            self.assertIn(IMAGES[backend], argv)
            self.assertIn("--pull=never", argv)
            self.assertIn("--read-only", argv)
            self.assertNotIn("--privileged", argv)
            self.assertNotIn(str(self.target / "tokens.json"), " ".join(argv))
            self.assertNotIn(str(self.target / "server.key"), " ".join(argv))
            self.assertTrue(
                all(
                    argv[i + 1].startswith("127.0.0.1:")
                    for i, x in enumerate(argv)
                    if x == "--publish"
                )
            )
        native = arguments(self.target, replace(c, mode="native"), "prometheus")
        self.assertIn("host", native)
        self.assertNotIn("--publish", native)

    def test_start_requires_authorization_before_docker(self):
        c = self.prepare()
        with patch("sre_suite.local_telemetry.lifecycle.command") as docker:
            with self.assertRaises(ValueError), running(self.target, c, authorized=False):
                pass
        docker.assert_not_called()

    def test_unowned_container_cannot_be_stopped_or_removed(self):
        c = self.prepare()
        with patch(
            "sre_suite.local_telemetry.lifecycle.command",
            side_effect=[
                c.name + "-loki",
                json.dumps([{"Config": {"Labels": {"sre.telemetry.id": "other"}}}]),
            ],
        ) as docker:
            with self.assertRaises(ValueError):
                owned(c, "loki")
        self.assertFalse(
            any("stop" in call.args[0] or "rm" in call.args[0] for call in docker.call_args_list)
        )

    def test_cleanup_attempts_all_owned_containers_and_keeps_data(self):
        c = self.prepare()
        with (
            patch(
                "sre_suite.local_telemetry.lifecycle.owned",
                side_effect=[ValueError(), {"Id": "synthetic-owned"}],
            ),
            patch("sre_suite.local_telemetry.lifecycle.command") as docker,
        ):
            with self.assertRaises(StartupError):
                cleanup(c, ["loki", "tempo"])
        self.assertEqual(len(docker.call_args_list), 2)
        self.assertTrue((self.target / "loki-data").is_dir())

    def test_fixed_gateway_routes_and_roles(self):
        self.assertEqual(route("tempo", "POST", "/v1/traces"), ("otlp", "traces_write"))
        self.assertEqual(
            route("loki", "GET", "/loki/api/v1/query_range?query=test"), ("loki", "loki_read")
        )
        for backend, method, path in (
            ("tempo", "GET", "//evil.example.invalid/api/search"),
            ("loki", "DELETE", "/loki/api/v1/delete"),
            ("prometheus", "POST", "/api/v1/admin/tsdb/delete_series"),
            ("tempo", "POST", "/v1/traces?x=y"),
            ("loki", "GET", "/ready#fragment"),
            ("prometheus", "GET", "/../api/v1/query_range"),
        ):
            with self.assertRaises(ValueError):
                route(backend, method, path)

    def test_explicit_loopback_ca_and_legacy_profile_hash_stability(self):
        original = profile(Backend.LOKI)
        self.assertNotIn("ca_env", profile_document(original))
        local = replace(
            original, base_url="https://localhost:19444", ca_env="SRE_TELEMETRY_CA_FILE"
        )
        raw = profile_document(local)
        raw["scope_labels"] = dict(local.scope_labels)
        parsed = QueryPolicy.parse(
            {"schema_version": 2, "scope": asdict(self.scope), "profiles": [raw]}
        )
        self.assertEqual(
            parsed.profiles[0], replace(local, scope_labels=tuple(sorted(local.scope_labels)))
        )
        with self.assertRaises(ValueError):
            replace(original, base_url="https://localhost:19444")
        with self.assertRaises(ValueError):
            replace(local, base_url="https://remote.example.invalid")
        with self.assertRaises(ValueError):
            replace(local, ca_env="SSL_CERT_FILE")
        with self.assertRaises(TelemetryError):
            transport(
                local, {local.authorization_env: "Bearer synthetic", local.tenant_env: "tenant"}
            )
        env = {
            "SRE_TELEMETRY_CA_FILE": str(self.stack / "server.crt"),
            local.authorization_env: "Bearer synthetic",
            local.tenant_env: "tenant",
            "SRE_GITHUB_TOKEN": "excluded",
        }
        p = QueryPolicy(self.scope, (local,))
        selected = child_environment(p, env)
        self.assertEqual(selected["SRE_TELEMETRY_CA_FILE"], env["SRE_TELEMETRY_CA_FILE"])
        self.assertNotIn("SRE_GITHUB_TOKEN", selected)
        transport(local, selected)

    def test_changed_backend_configuration_and_image_pins_are_rejected(self):
        self.prepare()
        for filename, key, changed in (
            ("stack.json", "images", {}),
            ("loki.json", "auth_enabled", True),
        ):
            path = self.target / filename
            original = path.read_bytes()
            raw = json.loads(original)
            raw[key] = changed
            path.write_text(json.dumps(raw))
            with self.assertRaises(ValueError):
                Config.load(self.target)
            path.write_bytes(original)
        Config.load(self.target)

    def test_stop_requires_explicit_authorization_before_docker(self):
        self.prepare()
        with patch("sre_suite.local_telemetry.lifecycle.command") as docker:
            with self.assertRaises(ValueError):
                stop_stack(self.target, authorized=False)
        docker.assert_not_called()

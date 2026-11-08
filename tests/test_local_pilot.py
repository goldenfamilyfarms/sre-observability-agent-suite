"""Local lifecycle configuration, authority separation and failure cleanup."""

import io
import json
import os
import signal
import subprocess
import sys
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import ModuleType
from unittest.mock import Mock, patch

from sre_suite.auth import WORKER_ACTIONS, Action, TokenAuthenticator
from sre_suite.execution.config import https_url
from sre_suite.execution.http import JsonHttp
from sre_suite.execution.pilot import PilotManifest
from sre_suite.local_pilot.__main__ import main
from sre_suite.local_pilot.config import (
    AUDIENCE,
    LocalConfig,
    execution_environment,
    initialize,
    load_credentials,
    private_json,
    token,
    write_json,
)
from sre_suite.local_pilot.lifecycle import (
    StartupError,
    check_ports,
    cleanup,
    postgres_args,
    stack_lock,
    stop_process,
    temporal_args,
    up,
    volume,
    wait_ready,
)
from sre_suite.local_pilot.server import with_health
from tests.test_api import request
from tests.test_pilot import PilotFixture


class LocalFixture(PilotFixture):
    def setUp(self):
        super().setUp()
        self.stack = self.root / "stack"
        now = datetime.now(UTC)
        self.manifest["registration"]["preview"].update(
            requested_at=now.isoformat(), expires_at=(now + timedelta(hours=2)).isoformat()
        )
        self.manifest["baseline"].update(
            observed_at=now.isoformat(), expires_at=(now + timedelta(hours=1)).isoformat()
        )
        self.manifest_path.write_text(json.dumps(self.manifest, default=str))
        self.config = LocalConfig("a" * 20, "postgres@sha256:" + "b" * 64, sys.executable)

    def initialize(self):
        initialize(self.stack, self.manifest_path, self.config)


class ConfigurationTests(LocalFixture):
    def test_initialize_generates_private_separate_tokens_and_exact_grants(self):
        self.initialize()
        self.assertEqual(LocalConfig.load(self.stack), self.config)
        worker = token(self.stack, "worker.token")
        controller = token(self.stack, "controller.token")
        self.assertNotEqual(worker, controller)
        raw = (self.stack / "credentials.json").read_text()
        self.assertNotIn(worker, raw)
        self.assertNotIn(controller, raw)
        auth = TokenAuthenticator(AUDIENCE, load_credentials(self.stack / "credentials.json"))
        now = datetime.now(UTC)
        scope = PilotManifest.parse(self.manifest, now).preview.scope
        worker_credential = auth.authenticate("Bearer " + worker, now)
        self.assertEqual({g.action for g in worker_credential.grants}, set(WORKER_ACTIONS))
        controller_credential = auth.authenticate("Bearer " + controller, now)
        self.assertEqual(
            {g.action for g in controller_credential.grants}, {Action.PREVIEW_REGISTER}
        )
        self.assertTrue(
            all(
                g.scope == scope
                for c in (worker_credential, controller_credential)
                for g in c.grants
            )
        )
        for path in self.stack.iterdir():
            self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_initialize_never_replaces_existing_state(self):
        self.initialize()
        before = (self.stack / "worker.token").read_bytes()
        with self.assertRaises(ValueError):
            self.initialize()
        self.assertEqual(before, (self.stack / "worker.token").read_bytes())

    def test_failed_certificate_creation_publishes_no_partial_stack(self):
        with patch("sre_suite.local_pilot.config.subprocess.run", side_effect=OSError("secret")):
            with self.assertRaises(OSError):
                self.initialize()
        self.assertFalse(self.stack.exists())

    def test_future_expired_production_and_stale_manifest_are_rejected(self):
        original = json.loads(json.dumps(self.manifest, default=str))
        for mutate in (
            lambda m: m["registration"]["preview"]["scope"].update(environment="production"),
            lambda m: m["registration"].update(expected_generation=2),
            lambda m: m["registration"]["preview"].update(expires_at="2020-01-01T00:00:00Z"),
            lambda m: m["registration"]["preview"].update(requested_at="2099-01-01T00:00:00Z"),
        ):
            value = json.loads(json.dumps(original))
            mutate(value)
            self.manifest_path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                self.initialize()
            self.assertFalse(self.stack.exists())

    def test_credentials_reload_rejects_wildcard_scope_duplicate_token_and_production(self):
        self.initialize()
        path = self.stack / "credentials.json"
        original = json.loads(path.read_text())
        for mutate in (
            lambda p: p["credentials"][0]["grants"][0]["scope"].update(environment="production"),
            lambda p: p["credentials"][0]["grants"][0].update(action="*"),
            lambda p: p["credentials"][0].update(token_sha256=p["credentials"][1]["token_sha256"]),
            lambda p: p["credentials"][0].update(audience="wrong"),
        ):
            value = json.loads(json.dumps(original))
            mutate(value)
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                load_credentials(path)

    def test_worker_environment_excludes_controller_database_and_ambient_secrets(self):
        self.initialize()
        source = self.root / "worker.json"
        write_json(source, {"SRE_CLAUDE_ENABLED": "false", "SRE_JIRA_API_TOKEN": "explicit-jira"})
        with patch.dict(
            os.environ,
            {
                "SRE_PILOT_SETUP_TOKEN": "ambient-controller",
                "PGPASSWORD": "ambient-db",
                "ANTHROPIC_API_KEY": "ambient-model",
                "PYTHONPATH": "/untrusted",
            },
        ):
            worker = execution_environment(self.stack, self.config, source)
            setup = execution_environment(self.stack, self.config, source, setup=True)
        self.assertEqual(worker["SRE_JIRA_API_TOKEN"], "explicit-jira")
        for key in ("SRE_PILOT_SETUP_TOKEN", "PGPASSWORD", "ANTHROPIC_API_KEY", "PYTHONPATH"):
            self.assertNotIn(key, worker)
        self.assertIn("SRE_PILOT_SETUP_TOKEN", setup)
        self.assertNotIn("SRE_JIRA_API_TOKEN", setup)
        self.assertNotIn(token(self.stack, "postgres.password"), json.dumps(worker))
        self.assertEqual(worker["SRE_TEMPORAL_NAMESPACE"], self.config.name)

    def test_worker_file_cannot_override_stack_authority_or_disable_tls(self):
        self.initialize()
        source = self.root / "worker.json"
        for name in (
            "SRE_PILOT_SETUP_TOKEN",
            "SRE_EVIDENCE_API_URL",
            "SRE_WORK_ROOT",
            "PYTHONPATH",
        ):
            source.write_text(json.dumps({name: "bad"}))
            source.chmod(0o600)
            with self.assertRaises(ValueError):
                execution_environment(self.stack, self.config, source)

    def test_private_root_files_and_symlink_storage_are_checked(self):
        self.initialize()
        file = self.stack / "worker.token"
        file.chmod(0o644)
        with self.assertRaises(ValueError):
            LocalConfig.load(self.stack)
        file.chmod(0o600)
        (self.stack / "temporal").rmdir()
        (self.stack / "temporal").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            LocalConfig.load(self.stack)

    def test_ports_images_binary_and_credential_files_are_strict(self):
        for changes in (
            {"api_port": True},
            {"postgres_port": 443},
            {"api_port": 7233},
            {"postgres_image": "postgres:latest"},
            {"temporal_binary": "temporal"},
        ):
            with self.assertRaises(ValueError):
                replace(self.config, **changes)
        self.initialize()
        path = self.stack / "credentials.json"
        path.write_text('{"schema_version":2,"schema_version":2,"credentials":[]}')
        with self.assertRaises(ValueError):
            private_json(path)

    def test_loopback_tls_requires_explicit_ca_and_remote_rules_remain_strict(self):
        self.initialize()
        cert = str(self.stack / "server.crt")
        with self.assertRaises(ValueError):
            https_url("https://127.0.0.1:8443")
        self.assertEqual(https_url(self.config.url, allow_loopback=True), self.config.url)
        JsonHttp(self.config.url, None, local_ca_file=cert)
        for url in (
            "http://127.0.0.1:8443",
            "https://example.invalid:8443",
            "https://example.invalid",
        ):
            with self.assertRaises(ValueError):
                JsonHttp(url, None, local_ca_file=cert)
        with self.assertRaises(ValueError):
            JsonHttp(self.config.url, None, local_ca_file="relative.crt")


class LifecycleTests(LocalFixture):
    def test_process_arguments_bind_loopback_preserve_data_and_do_not_expose_password(self):
        pg = postgres_args(self.stack, self.config, "owned-volume")
        self.assertIn("127.0.0.1:55432:5432", pg)
        self.assertIn("--pull=never", pg)
        self.assertIn("POSTGRES_PASSWORD_FILE=/run/secrets/postgres-password", pg)
        self.assertFalse(any(x.startswith("POSTGRES_PASSWORD=") for x in pg))
        temporal = temporal_args(self.stack, self.config)
        self.assertIn("--headless", temporal)
        self.assertIn("127.0.0.1", temporal)
        self.assertIn(str(self.stack / "temporal" / "state.db"), temporal)
        self.assertIn(self.config.name, temporal)

    def test_stack_lock_rejects_concurrent_start(self):
        self.initialize()
        with stack_lock(self.stack), self.assertRaises(BlockingIOError), stack_lock(self.stack):
            pass

    def test_occupied_port_is_not_reused(self):
        listener = Mock()
        listener.bind.side_effect = OSError("occupied port")
        with patch("sre_suite.local_pilot.lifecycle.socket.socket", return_value=listener):
            with self.assertRaises(OSError):
                check_ports(self.config)
        listener.close.assert_called_once()

    def test_existing_unowned_volume_is_rejected_without_mutation(self):
        with patch(
            "sre_suite.local_pilot.lifecycle.command",
            side_effect=[
                self.config.name + "-postgres",
                json.dumps([{"Labels": {"sre.pilot.id": "other"}}]),
            ],
        ) as call:
            with self.assertRaises(ValueError):
                volume(self.config)
            self.assertFalse(any("create" in c.args[0] for c in call.call_args_list))

    def test_readiness_stops_on_child_exit_timeout_and_interrupt(self):
        child = Mock()
        child.poll.return_value = 1
        with self.assertRaises(RuntimeError):
            wait_ready(lambda: None, processes=[child])
        with self.assertRaises(TimeoutError):
            wait_ready(lambda: None, processes=[], timeout=0)
        with self.assertRaises(KeyboardInterrupt):
            wait_ready(lambda: None, processes=[], stopping=lambda: True)

    def test_shutdown_escalates_only_owned_process_group(self):
        child = Mock(pid=12345)
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired("test", 10), 0]
        with patch("sre_suite.local_pilot.lifecycle.os.killpg") as kill:
            stop_process(child)
        self.assertEqual(kill.call_args_list[0].args, (12345, signal.SIGINT))
        self.assertEqual(kill.call_args_list[1].args, (12345, signal.SIGKILL))

    def test_failure_after_temporal_start_stops_children_and_preserves_volume(self):
        self.initialize()
        child = Mock()
        child.poll.return_value = None
        commands = []
        postgres = ModuleType("sre_suite.postgres")
        postgres.migrate = Mock()

        def command(argv, **kwargs):
            commands.append(argv)
            return "owned-container"

        def wait(probe, **kwargs):
            if kwargs["processes"]:
                raise RuntimeError("synthetic failure containing a secret")

        with (
            patch("sre_suite.local_pilot.lifecycle.command", side_effect=command),
            patch("sre_suite.local_pilot.lifecycle.volume", return_value="owned-volume"),
            patch("sre_suite.local_pilot.lifecycle.check_ports"),
            patch("sre_suite.local_pilot.lifecycle.wait_ready", side_effect=wait),
            patch.dict("sys.modules", {"sre_suite.postgres": postgres}),
            patch("sre_suite.local_pilot.lifecycle.spawn", return_value=child),
            patch("sre_suite.local_pilot.lifecycle.stop_process") as stop,
        ):
            with self.assertRaisesRegex(StartupError, "temporal") as error:
                up(self.stack, self.config, None, notify=lambda _: None, stopping=lambda: False)
        self.assertNotIn("secret", str(error.exception))
        stop.assert_called_once_with(child)
        self.assertIn(["docker", "stop", "--time", "10", "owned-container"], commands)
        self.assertFalse(any("rm" in cmd or "prune" in cmd for cmd in commands))
        self.assertTrue((self.stack / "credentials.json").exists())

    def test_service_health_and_invalid_credential_reload_fail_closed(self):
        api = Mock(return_value=[b"{}"])
        reload = Mock(side_effect=ValueError("private detail"))
        app = with_health(api, lambda: None, reload)
        status, body, _ = request(app, "/v1/evidence", {})
        self.assertEqual((status, body), (503, {"error": "service_unavailable"}))
        api.assert_not_called()

    def test_cli_failure_is_redacted_and_does_not_start_services(self):
        out = io.StringIO()
        with (
            patch("sys.argv", ["pilot", "up", "--directory", str(self.stack)]),
            redirect_stdout(out),
            self.assertRaises(SystemExit) as error,
        ):
            main()
        self.assertEqual(error.exception.code, 1)
        self.assertNotIn(str(self.stack), out.getvalue())
        self.assertEqual(json.loads(out.getvalue())["component"], "configuration")

    def test_cleanup_attempts_every_owned_resource_after_one_failure(self):
        first, second = Mock(), Mock()
        with (
            patch(
                "sre_suite.local_pilot.lifecycle.stop_process",
                side_effect=[OSError("private"), None],
            ) as stop,
            patch("sre_suite.local_pilot.lifecycle.command") as docker,
        ):
            with self.assertRaisesRegex(StartupError, "cleanup"):
                cleanup([first, second], "owned-container")
        self.assertEqual([call.args[0] for call in stop.call_args_list], [second, first])
        docker.assert_called_once_with(
            ["docker", "stop", "--time", "10", "owned-container"], timeout=20
        )

    def test_invalid_worker_settings_fail_before_docker_or_process_start(self):
        self.initialize()
        worker = self.root / "invalid-worker.json"
        write_json(worker, {"SRE_EVIDENCE_API_URL": "http://unsafe"})
        with (
            patch("sre_suite.local_pilot.lifecycle.command") as docker,
            patch("sre_suite.local_pilot.lifecycle.spawn") as child,
        ):
            with self.assertRaisesRegex(StartupError, "configuration"):
                up(self.stack, self.config, worker, notify=lambda _: None, stopping=lambda: False)
        docker.assert_not_called()
        child.assert_not_called()

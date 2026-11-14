"""Managed startup fences, role isolation, immutable export completion and local opt-in."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from sre_suite.demo_fixture.lifecycle import main
from sre_suite.demo_fixture.managed import ManagedPolicy, readiness, role_environment
from sre_suite.demo_fixture.managed_contracts import demo_dispatch, validate_export
from sre_suite.execution.certification import EngineeringCertification
from sre_suite.execution.http import RemoteError
from sre_suite.execution.telemetry import EngineeringTelemetry
from sre_suite.execution.wire import canonical
from sre_suite.fixtures.contracts import FixtureRequest
from sre_suite.local_pilot.demo import prepare
from tests.managed_fixture import ManagedFixture


class ManagedDemoTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.f = ManagedFixture(self.root)
        self.addCleanup(self.f.close)

    def transports(self):
        clients = {s: Mock() for s in ("traces", "logs")}
        clients["traces"].call.return_value = {}
        clients["logs"].call.return_value = None
        return clients

    def test_export_loss_restart_and_cached_receipt_preserve_payloads_and_deadline(self):
        self.f.execute()
        clients = self.transports()
        clients["traces"].call.side_effect = [RemoteError(0), {}]
        with patch("sre_suite.demo_fixture.managed.transports", return_value=clients):
            with self.assertRaises(RemoteError):
                self.f.runtime.export()
            self.f.runtime = self.f.reopen()
            receipt = self.f.runtime.export()
            self.f.d.f.now = self.f.d.f.end + timedelta(seconds=1)
            self.assertEqual(self.f.runtime.export(), receipt)
        self.assertEqual(clients["logs"].call.call_count, 1)
        self.assertEqual(
            clients["traces"].call.call_args_list[0], clients["traces"].call.call_args_list[1]
        )
        self.assertEqual(receipt["delivered_batches"], 2)
        validate_export(
            receipt,
            FixtureRequest.parse(self.f.request).identity,
            self.f.policy.dispatch,
            self.f.d.f.start,
            self.f.d.f.end,
        )

    def test_no_empty_export_or_new_delivery_at_cutoff(self):
        clients = self.transports()
        with patch("sre_suite.demo_fixture.managed.transports", return_value=clients):
            with self.assertRaises(ValueError):
                self.f.runtime.export()
            self.f.execute()
            self.f.d.f.now = self.f.d.f.end - timedelta(seconds=1)
            with self.assertRaises(ValueError):
                self.f.runtime.export()
        clients["traces"].call.assert_not_called()
        clients["logs"].call.assert_not_called()

    def test_export_crossing_cutoff_cannot_record_completion(self):
        self.f.execute()
        clients = self.transports()

        def late(*_):
            self.f.d.f.now = self.f.d.f.end
            return None

        clients["logs"].call.side_effect = late
        with (
            patch("sre_suite.demo_fixture.managed.transports", return_value=clients),
            self.assertRaises(ValueError),
        ):
            self.f.runtime.export()
        with self.f.runtime.controller.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM outbox WHERE delivered=1").fetchone()[0], 0
            )

    def test_cached_receipt_rechecks_generation_and_expiry(self):
        self.f.execute()
        with patch("sre_suite.demo_fixture.managed.transports", return_value=self.transports()):
            self.f.runtime.export()
        self.f.d.f.f.registry.start(
            replace(
                self.f.d.f.f.preview,
                scope=replace(self.f.d.policy.verification.scope, generation=2),
            ),
            1,
            self.f.d.f.now,
        )
        with self.assertRaises(RemoteError):
            self.f.runtime.export()
        self.f.d.f.now = self.f.d.f.end + timedelta(seconds=121)
        with self.assertRaises(ValueError):
            self.f.reopen()

    def test_readiness_accepts_only_exact_authenticated_target_identity(self):
        self.f.request["plan"]["window_start"] = (
            self.f.d.f.start + timedelta(seconds=5)
        ).isoformat()
        self.f.request["plan"]["window_end"] = (self.f.d.f.end + timedelta(seconds=5)).isoformat()
        self.f.runtime = self.f.reopen()
        self.f.runtime.fence()
        with patch("sre_suite.demo_fixture.managed.JsonHttp") as http:
            expected = readiness(self.f.runtime.request, self.f.policy)
            http.return_value.call.return_value = expected
            self.assertEqual(self.f.runtime.ready(), expected)
            http.return_value.call.return_value = {**expected, "workflow_id": "another-target"}
            with self.assertRaises(ValueError):
                self.f.runtime.ready()
        with self.assertRaises(ValueError):
            self.f.runtime.controller.execute("/fixtures/capabilities", {})
        self.f.d.f.now -= timedelta(seconds=2)
        with self.assertRaises(ValueError):
            self.f.reopen()

    def test_managed_identity_and_authorization_bind_all_dimensions(self):
        original = FixtureRequest.parse(self.f.request).identity
        self.assertNotEqual(original, FixtureRequest.parse(self.f.d.request).identity)
        for mutate in (
            lambda r: r["plan"]["release"].update(artifact_digest="sha256:" + "e" * 64),
            lambda r: r["plan"]["release"]["scope"].update(generation=2),
            lambda r: r["plan"]["release"]["scope"].update(environment_id="other-preview"),
            lambda r: r["plan"]["demo"].update(policy_sha256="e" * 64),
            lambda r: r["plan"]["demo"].update(task_queue="untrusted"),
            lambda r: r["plan"]["demo"].update(settle_seconds=3),
        ):
            raw = copy.deepcopy(self.f.request)
            mutate(raw)
            with self.assertRaises(ValueError):
                self.f.policy.authorize(raw)
        changed = copy.deepcopy(self.f.config)
        changed["export"]["traces"]["base_url"] = "https://another.example.invalid"
        with self.assertRaises(ValueError):
            ManagedPolicy(changed).authorize(self.f.request)

    def test_serve_probe_export_subprocess_credentials_are_separate(self):
        env = {
            **self.f.env,
            "SRE_JIRA_API_TOKEN": "no",
            "ANTHROPIC_API_KEY": "no",
            "SRE_GITHUB_TOKEN": "no",
            "PYTHONPATH": "no",
        }
        for op in ("serve", "ready", "export"):
            values = role_environment(self.f.policy, op, env)
            for forbidden in (
                "SRE_JIRA_API_TOKEN",
                "ANTHROPIC_API_KEY",
                "SRE_GITHUB_TOKEN",
                "PYTHONPATH",
            ):
                self.assertNotIn(forbidden, values)
            self.assertEqual("SRE_DEMO_TRACES_AUTH" in values, op == "export")
            self.assertEqual("SRE_DEMO_FIXTURE_AUTH" in values, op == "serve")
            self.assertEqual("SRE_DEMO_LIFECYCLE_AUTH" in values, op in {"serve", "ready"})

    def test_policy_rejects_remote_target_bad_queue_and_small_budgets(self):
        for mutate in (
            lambda c: c.update(task_queue=c["fixture_policy"]["runner_queue"]),
            lambda c: c.update(startup_seconds=True),
            lambda c: c.update(settle_seconds=0),
            lambda c: c["fixture_policy"]["target"].update(
                base_url="https://target.example.invalid", ca_env=None
            ),
            lambda c: c["recipe"]["edits"][0].update(content="different source"),
        ):
            raw = copy.deepcopy(self.f.config)
            mutate(raw)
            with self.assertRaises(ValueError):
                ManagedPolicy(raw)

    def test_engineering_config_freezes_dispatch_and_checks_startup_budget(self):
        engine = self.f.d.f.engineering
        config = copy.deepcopy(engine.config)
        config["demo"] = self.f.policy.dispatch
        path = self.root / "cert-config.json"
        path.write_text(canonical(config))
        path.chmod(0o600)
        with self.assertRaises(ValueError):
            EngineeringCertification(path, engine.runtime.telemetry)
        raw = copy.deepcopy(engine.telemetry)
        raw["delay_seconds"] = 5
        engine.telemetry_path.write_text(canonical(raw))
        telemetry = EngineeringTelemetry(engine.telemetry_path)
        cert = EngineeringCertification(path, telemetry)
        self.assertEqual(cert.demo, self.f.policy.dispatch)
        config.pop("demo")
        path.write_text(canonical(config))
        self.assertNotEqual(cert.fingerprint, EngineeringCertification(path, telemetry).fingerprint)

    def test_dispatch_strict_fields_and_completion_binding(self):
        for raw in (
            {},
            {**self.f.policy.dispatch, "secret": "no"},
            {**self.f.policy.dispatch, "task_queue": "../bad"},
        ):
            with self.assertRaises(ValueError):
                demo_dispatch(raw)
        self.f.execute()
        with patch("sre_suite.demo_fixture.managed.transports", return_value=self.transports()):
            receipt = self.f.runtime.export()
        for mutate in (
            lambda r: r.update(delivered_batches=True),
            lambda r: r.update(policy_sha256="d" * 64),
            lambda r: r.update(completed_at=self.f.d.f.end.isoformat()),
        ):
            raw = copy.deepcopy(receipt)
            mutate(raw)
            with self.assertRaises(ValueError):
                validate_export(
                    raw,
                    self.f.runtime.request.identity,
                    self.f.policy.dispatch,
                    self.f.d.f.start,
                    self.f.d.f.end,
                )

    def test_worker_start_authorization_precedes_temporal_connection(self):
        with (
            patch(
                "sys.argv",
                ["managed", "worker", "--config", str(self.f.path), "--root", str(self.f.state)],
            ),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(), 1)

    def test_local_supervisor_environment_and_explicit_authorization(self):
        from types import SimpleNamespace

        stack = SimpleNamespace(
            postgres_port=55432,
            temporal_port=7233,
            api_port=8443,
            url="https://127.0.0.1:8443",
            name="synthetic-pilot",
        )
        creds = self.root / "demo-env.json"
        values = {k: v for k, v in self.f.env.items() if k != "SRE_EVIDENCE_API_URL"}
        creds.write_text(canonical(values))
        creds.chmod(0o600)
        with self.assertRaises(ValueError):
            prepare(self.root, stack, self.f.path, creds, False)
        argv, env, queue = prepare(self.root, stack, self.f.path, creds, True)
        self.assertIn("--authorize-targets", argv)
        self.assertIn("--authorize-export", argv)
        self.assertEqual(env["SRE_TEMPORAL_NAMESPACE"], "synthetic-pilot")
        self.assertEqual(queue, self.f.policy.dispatch["task_queue"])
        values["SRE_JIRA_API_TOKEN"] = "forbidden"
        creds.write_text(json.dumps(values))
        with self.assertRaises(ValueError):
            prepare(self.root, stack, self.f.path, creds, True)

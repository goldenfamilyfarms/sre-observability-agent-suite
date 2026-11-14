"""Pilot setup exercises the real authenticated API, including uncertain writes."""

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from sre_suite.api import EvidenceContextApi
from sre_suite.api_storage import _Previews, sqlite_backend
from sre_suite.auth import Action, TokenAuthenticator
from sre_suite.demo import example_ticket
from sre_suite.execution.evidence_client import EvidenceClient
from sre_suite.execution.http import RemoteError
from sre_suite.execution.pilot import PilotManifest, PilotSetupError, setup_pilot
from sre_suite.execution.runtime import execution_key
from sre_suite.previews import PreviewRegistry, PreviewRequest
from tests.test_api import AUDIENCE, SECOND_TOKEN, TOKEN, credential, request
from tests.test_foundation import NOW


class Transport:
    def __init__(self, app, token):
        self.app, self.token = app, token
        self.calls = []
        self.after = lambda path, body: body

    def call(self, method, path, payload=None):
        assert method == "POST"
        self.calls.append(path)
        status, body, _ = request(self.app, path, payload, token=self.token)
        if status != 200:
            raise RemoteError(status)
        return self.after(path, body)


class PilotFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.scope = example_ticket(NOW).scope
        self.preview = PreviewRequest(1, 1, self.scope, NOW, NOW + timedelta(minutes=30))
        self.registration = {
            "schema_version": 2,
            "preview": asdict(self.preview),
            "expected_generation": 0,
        }
        self.worker = credential(self.scope)
        self.controller = credential(
            self.scope, (Action.PREVIEW_REGISTER,), SECOND_TOKEN, "controller"
        )
        self.auth = TokenAuthenticator(AUDIENCE, (self.worker, self.controller))
        self.path = str(self.root / "api.db")
        self.backend = sqlite_backend(self.path)
        self.app = EvidenceContextApi(
            self.auth, self.backend, clock=lambda: NOW, allow_preview_registration=True
        )
        self.controller_http = Transport(self.app, SECOND_TOKEN)
        self.worker_http = Transport(self.app, TOKEN)
        self.client = EvidenceClient("https://example.invalid", TOKEN, self.worker_http)
        self.registry = PreviewRegistry(self.path)
        self.addCleanup(self.registry.close)
        self.baseline = self.root / "baseline.bin"
        self.baseline.write_bytes(b"synthetic baseline")
        self.manifest = {
            "schema_version": 2,
            "registration": self.registration,
            "ticket_key": "DEMO-1",
            "run_id": "pilot-1",
            "baseline": {
                "sha256": hashlib.sha256(self.baseline.read_bytes()).hexdigest(),
                "observed_at": NOW.isoformat(),
                "expires_at": (NOW + timedelta(minutes=20)).isoformat(),
            },
        }
        self.manifest_path = self.root / "manifest.json"
        self.output = self.root / "prepared"
        self.env = {
            "SRE_EVIDENCE_API_URL": "https://example.invalid",
            "SRE_EVIDENCE_API_TOKEN": TOKEN,
            "SRE_PILOT_SETUP_TOKEN": SECOND_TOKEN,
        }

    def run_setup(self, **kwargs):
        self.manifest_path.write_text(json.dumps(self.manifest, default=str))
        return setup_pilot(
            self.env,
            self.manifest_path,
            self.baseline,
            self.output,
            clock=lambda: NOW,
            controller=self.controller_http,
            evidence=self.client,
            **kwargs,
        )

    def register(self, payload=None, token=SECOND_TOKEN, app=None):
        return request(
            app or self.app, "/v1/previews/register", payload or self.registration, token=token
        )

    def counts(self):
        with closing(sqlite3.connect(self.path)) as db:
            return tuple(
                db.execute("SELECT count(*) FROM " + table).fetchone()[0]
                for table in ("previews", "preview_events", "evidence_objects")
            )


class RegistrationTests(PilotFixture):
    def test_opt_in_and_separate_exact_scope_controller_grant(self):
        default = EvidenceContextApi(self.auth, self.backend, clock=lambda: NOW)
        self.assertEqual(self.register(app=default)[0], 404)
        self.assertEqual(self.register(token=TOKEN)[0], 403)
        self.assertEqual(self.counts(), (0, 0, 0))
        self.assertEqual(self.register()[0], 200)
        self.assertEqual(
            request(
                self.app,
                "/v1/context",
                {
                    "schema_version": 2,
                    "scope": asdict(self.scope),
                    "expected_version": None,
                },
                token=SECOND_TOKEN,
            )[0],
            403,
        )

    def test_registration_retries_and_concurrent_duplicates_create_one_event(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.register(), range(2)))
        self.assertEqual([r[0] for r in results], [200, 200])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(self.register()[0], 200)
        self.assertEqual(self.counts(), (1, 1, 0))

    def test_changed_window_closed_and_stale_generation_do_not_reopen(self):
        self.assertEqual(self.register()[0], 200)
        changed = {
            **self.registration,
            "preview": asdict(replace(self.preview, expires_at=NOW + timedelta(minutes=29))),
        }
        self.assertEqual(self.register(changed)[0], 409)
        self.registry.terminate(self.scope, "test closure", NOW)
        self.assertEqual(self.register()[0], 409)
        next_preview = replace(self.preview, scope=replace(self.scope, generation=2))
        next_request = {
            "schema_version": 2,
            "preview": asdict(next_preview),
            "expected_generation": 1,
        }
        self.assertEqual(self.register(next_request)[0], 403)
        self.auth.replace_credentials(
            (
                self.worker,
                self.controller,
                credential(
                    next_preview.scope, (Action.PREVIEW_REGISTER,), "z" * 43, "next-controller"
                ),
            )
        )
        self.assertEqual(self.register(next_request, token="z" * 43)[0], 200)
        self.assertEqual(self.register()[0], 409)

    def test_wrong_identity_types_fields_and_scope_rejected_before_database(self):
        for change in (
            {"pr_number": 2},
            {"repository_id": True},
            {"pr_number": "1"},
            {"requested_at": NOW.replace(tzinfo=None).isoformat()},
            {"scope": asdict(replace(self.scope, environment_id="preview-r2-pr1"))},
        ):
            with self.subTest(change=change):
                body = {**self.registration, "preview": {**asdict(self.preview), **change}}
                self.assertEqual(self.register(body)[0], 400)
        for expected in (True, -1, 1, "0"):
            self.assertEqual(
                self.register({**self.registration, "expected_generation": expected})[0], 400
            )
        for field, value in (("revision", "b" * 40), ("service", "another")):
            preview = replace(self.preview, scope=replace(self.scope, **{field: value}))
            self.assertEqual(
                self.register({**self.registration, "preview": asdict(preview)})[0], 403
            )
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_expired_future_and_production_requests_cannot_register(self):
        for changes in ({"expires_at": NOW}, {"requested_at": NOW + timedelta(seconds=1)}):
            body = {**self.registration, "preview": {**asdict(self.preview), **changes}}
            self.assertIn(self.register(body)[0], (400, 409))
        body = json.loads(json.dumps(self.registration, default=str))
        body["preview"]["scope"]["environment"] = "production"
        self.assertEqual(self.register(body)[0], 400)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_revocation_after_registration_rolls_back_preview_and_event(self):
        original = _Previews.start

        def revoke(store, *args):
            original(store, *args)
            self.auth.replace_credentials(())

        with patch.object(_Previews, "start", revoke):
            self.assertEqual(self.register()[0], 401)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_expiry_after_registration_rolls_back(self):
        original = _Previews.start

        def expire(store, *args):
            original(store, *args)
            self.app._clock = lambda: self.preview.expires_at

        with patch.object(_Previews, "start", expire):
            self.assertEqual(self.register()[0], 409)
        self.assertEqual(self.counts(), (0, 0, 0))


class SetupTests(PilotFixture):
    def test_full_setup_private_artifacts_and_idempotent_retry(self):
        result = self.run_setup()
        spec = json.loads((self.output / "execution.json").read_text())
        metadata = json.loads((self.output / "baseline-evidence.json").read_text())
        self.assertEqual(
            result,
            {"status": "prepared", "workflow_id": execution_key(spec), "execution_started": False},
        )
        self.assertEqual(spec["scope"], asdict(self.scope))
        self.assertEqual(metadata["evidence"]["producer"], "worker-one")
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.output.iterdir()}
        for p in self.output.iterdir():
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(TOKEN, p.read_text())
            self.assertNotIn(SECOND_TOKEN, p.read_text())
        self.assertEqual(self.run_setup(), result)
        self.assertEqual(
            before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.output.iterdir()}
        )
        self.assertEqual(self.counts(), (1, 1, 1))
        self.assertEqual(
            self.worker_http.calls, ["/v1/evidence", "/v1/evidence/read", "/v1/context"] * 2
        )

    def test_lost_registration_response_can_resume_same_manifest(self):
        def lose(*_):
            raise RemoteError(0)

        self.controller_http.after = lose
        with self.assertRaisesRegex(PilotSetupError, "registration"):
            self.run_setup()
        self.assertEqual(self.counts(), (1, 1, 0))
        self.assertFalse(self.output.exists())
        self.controller_http.after = lambda path, body: body
        self.run_setup()
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_lost_upload_response_can_resume_without_duplicate_evidence(self):
        def lose(path, body):
            if path == "/v1/evidence":
                raise RemoteError(0)
            return body

        self.worker_http.after = lose
        with self.assertRaisesRegex(PilotSetupError, "baseline_upload"):
            self.run_setup()
        self.assertEqual(self.counts(), (1, 1, 1))
        self.assertFalse(self.output.exists())
        self.worker_http.after = lambda path, body: body
        self.run_setup()
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_generation_changed_after_upload_prevents_spec_and_retry(self):
        def advance(path, body):
            if path == "/v1/evidence":
                self.registry.start(
                    replace(self.preview, scope=replace(self.scope, generation=2)), 1, NOW
                )
            return body

        self.worker_http.after = advance
        with self.assertRaisesRegex(PilotSetupError, "verification"):
            self.run_setup()
        self.assertFalse(self.output.exists())
        with self.assertRaisesRegex(PilotSetupError, "registration"):
            self.run_setup()
        self.assertEqual(self.counts(), (1, 2, 1))

    def test_revoked_worker_after_upload_prevents_output(self):
        def revoke(path, body):
            if path == "/v1/evidence":
                self.auth.replace_credentials((self.controller,))
            return body

        self.worker_http.after = revoke
        with self.assertRaisesRegex(PilotSetupError, "verification"):
            self.run_setup()
        self.assertFalse(self.output.exists())

    def test_bad_digest_or_oversize_content_makes_no_remote_calls(self):
        for data in (b"changed", b"x" * 1_048_577):
            self.baseline.write_bytes(data)
            with self.assertRaisesRegex(PilotSetupError, "validation"):
                self.run_setup()
        self.assertEqual(self.controller_http.calls, [])

    def test_strict_manifest_rejects_bad_identity_generation_window_and_version(self):
        original = json.loads(json.dumps(self.manifest, default=str))
        mutations = [
            lambda m: m.update(schema_version=True),
            lambda m: m.update(extra="unexpected"),
            lambda m: m.update(ticket_key="invalid"),
            lambda m: m.update(run_id="../unsafe"),
            lambda m: m["registration"].update(expected_generation=True),
            lambda m: m["registration"]["preview"].update(pr_number=2),
            lambda m: m["registration"]["preview"]["scope"].update(environment="production"),
            lambda m: m["baseline"].update(expires_at=(NOW + timedelta(hours=1)).isoformat()),
            lambda m: m["baseline"].update(observed_at=(NOW + timedelta(minutes=1)).isoformat()),
            lambda m: m["baseline"].update(expires_at=NOW.isoformat()),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                manifest = json.loads(json.dumps(original))
                mutate(manifest)
                with self.assertRaises((ValueError, TypeError)):
                    PilotManifest.parse(manifest, NOW)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_missing_same_or_invalid_credentials_do_not_register(self):
        for value in (None, TOKEN, "short"):
            if value is None:
                self.env.pop("SRE_PILOT_SETUP_TOKEN")
            else:
                self.env["SRE_PILOT_SETUP_TOKEN"] = value
            with self.assertRaisesRegex(PilotSetupError, "validation"):
                self.run_setup()
        self.assertEqual(self.controller_http.calls, [])

    def test_existing_output_for_different_request_is_not_overwritten(self):
        self.run_setup()
        before = (self.output / "execution.json").read_bytes()
        calls = len(self.controller_http.calls)
        self.manifest["run_id"] = "different"
        with self.assertRaisesRegex(PilotSetupError, "validation"):
            self.run_setup()
        self.assertEqual(len(self.controller_http.calls), calls)
        self.assertEqual((self.output / "execution.json").read_bytes(), before)

    def test_output_rejects_symlinks_public_permissions_and_git_checkout(self):
        real = self.root / "real"
        real.mkdir(mode=0o700)
        self.output.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(PilotSetupError, "validation"):
            self.run_setup()
        self.output.unlink()
        self.root.chmod(0o755)
        try:
            with self.assertRaisesRegex(PilotSetupError, "validation"):
                self.run_setup()
        finally:
            self.root.chmod(0o700)
        (self.root / ".git").mkdir()
        with self.assertRaisesRegex(PilotSetupError, "validation"):
            self.run_setup()
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_partial_directory_rejected_and_failed_publish_leaves_no_bundle(self):
        self.output.mkdir(mode=0o700)
        with self.assertRaisesRegex(PilotSetupError, "validation"):
            self.run_setup()
        self.output.rmdir()
        with patch("sre_suite.execution.pilot._publish", side_effect=OSError("private data")):
            with self.assertRaisesRegex(PilotSetupError, "output") as error:
                self.run_setup()
        self.assertNotIn("private data", str(error.exception))
        self.assertFalse(self.output.exists())
        self.run_setup()
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_substituted_registration_and_evidence_responses_fail_closed(self):
        self.controller_http.after = lambda path, body: {**body, "expected_generation": 1}
        with self.assertRaisesRegex(PilotSetupError, "registration"):
            self.run_setup()
        self.assertEqual(self.worker_http.calls, [])
        self.controller_http.after = lambda path, body: body

        def tamper(path, body):
            if path == "/v1/evidence":
                body["evidence"]["sha256"] = "0" * 64
            return body

        self.worker_http.after = tamper
        with self.assertRaisesRegex(PilotSetupError, "baseline_upload"):
            self.run_setup()
        self.assertFalse(self.output.exists())

    def test_concurrent_same_command_publishes_one_consistent_bundle(self):
        # Freeze the manifest before concurrent invocations; CLI inputs are immutable.
        self.manifest_path.write_text(json.dumps(self.manifest, default=str))

        def run(_):
            return setup_pilot(
                self.env,
                self.manifest_path,
                self.baseline,
                self.output,
                clock=lambda: NOW,
                controller=self.controller_http,
                evidence=self.client,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_cli_dispatch_without_temporal_jira_or_model_and_redacted_failure(self):
        import contextlib
        import io

        from sre_suite.execution.cli import main

        args = [
            "cli",
            "pilot-setup",
            str(self.manifest_path),
            "--baseline",
            str(self.baseline),
            "--output",
            str(self.output),
        ]
        with patch("sys.argv", args), patch.dict(os.environ, {}, clear=True):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as error:
                main()
            self.assertEqual(error.exception.code, 1)
            self.assertEqual(
                json.loads(out.getvalue()),
                {"status": "failed", "phase": "validation", "remote_changes_may_exist": False},
            )
        with (
            patch("sys.argv", args),
            patch(
                "sre_suite.execution.pilot.setup_pilot",
                return_value={
                    "status": "prepared",
                    "execution_started": False,
                },
            ) as setup,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            main()
            setup.assert_called_once()

    def test_read_only_preflight_transport_cannot_register(self):
        from sre_suite.execution.readiness import ReadOnlyHttp

        readonly = ReadOnlyHttp(self.controller_http, lambda: None, evidence=True)
        with self.assertRaises(ValueError):
            readonly.call("POST", "/v1/previews/register", self.registration)
        self.assertEqual(self.controller_http.calls, [])
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_changed_api_origin_cannot_reuse_output(self):
        self.run_setup()
        calls = len(self.controller_http.calls)
        self.env["SRE_EVIDENCE_API_URL"] = "https://different.example.invalid"
        with self.assertRaisesRegex(PilotSetupError, "validation"):
            self.run_setup()
        self.assertEqual(len(self.controller_http.calls), calls)

    def test_expiry_after_final_read_prevents_output(self):
        self.manifest_path.write_text(json.dumps(self.manifest, default=str))
        times = iter((NOW, NOW + timedelta(minutes=21)))
        with self.assertRaisesRegex(PilotSetupError, "verification"):
            setup_pilot(
                self.env,
                self.manifest_path,
                self.baseline,
                self.output,
                clock=lambda: next(times),
                controller=self.controller_http,
                evidence=self.client,
            )
        self.assertFalse(self.output.exists())
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_generated_evidence_fits_existing_jira_contract(self):
        from sre_suite.api_wire import evidence_from_wire
        from sre_suite.execution.wire import ticket_from_wire, ticket_json

        self.run_setup()
        evidence = evidence_from_wire(
            json.loads((self.output / "baseline-evidence.json").read_text())["evidence"]
        )
        ticket = replace(example_ticket(NOW), evidence=(evidence,))
        self.assertEqual(ticket_from_wire(ticket_json(ticket)), ticket)

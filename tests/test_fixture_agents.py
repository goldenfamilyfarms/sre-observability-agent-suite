"""Independent fixed expectations, bounded traffic, current scope and durable evidence."""

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from sre_suite.api_wire import evidence_from_wire
from sre_suite.execution.http import RemoteError
from sre_suite.execution.wire import canonical
from sre_suite.fixtures.config import environment
from sre_suite.fixtures.contracts import FixturePolicy, FixtureRequest
from tests.fixtures_fixture import FixtureAgentFixture


class FixtureAgentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.f = FixtureAgentFixture(Path(tmp.name).resolve())
        self.addCleanup(self.f.close)
        self.f.begin()

    def test_independent_runner_and_assessor_feed_all_120_checks_and_gate(self):
        reference, result = self.f.complete()
        doc = json.loads(self.f.v.assessor.read(evidence_from_wire(result["evidence"])))
        self.assertEqual(len(doc["checks"]), 120)
        self.assertEqual(doc["execution"]["evidence"], reference)
        self.assertEqual(reference["producer"], "runner")
        self.assertEqual(result["evidence"]["producer"], "assessor")
        receipt = self.f.engineering.certify()
        self.assertEqual(receipt["verdict"], "PASSED")
        report = self.f.engineering.runtime.finish(self.f.engineering.args)
        self.assertEqual(report["outcome"], "ready_for_review")
        certificate = json.loads(self.f.v.api.read(evidence_from_wire(receipt["evidence"])))
        self.assertIn(reference["id"], {e["id"] for e in certificate["evidence"]})
        self.assertEqual(len(self.f.target.responses), 3)

    def test_lost_target_response_retries_same_intent_without_duplicate_effects(self):
        self.f.target.lose_once = True
        with self.assertRaises(RemoteError):
            self.f.runner.execute(self.f.request)
        first = self.f.target.calls[0]
        artifact = self.f.runner.execute(self.f.request)
        self.assertEqual(first, self.f.target.calls[1])
        self.assertEqual(len(self.f.target.responses), 3)
        self.assertEqual(self.f.runner.execute(self.f.request), artifact)
        self.assertEqual(len(self.f.target.calls), 4)

    def test_lost_evidence_upload_and_assessment_acks_reuse_exact_bytes(self):
        artifact = self.f.runner.execute(self.f.request)
        self.f.now = self.f.end
        original = self.f.api.upload
        attempts = []

        def lose(*args):
            attempts.append(args)
            value = original(*args)
            if len(attempts) == 1:
                raise RemoteError(0)
            return value

        with patch.object(self.f.api, "upload", side_effect=lose):
            with self.assertRaises(RemoteError):
                self.f.runner.publish(self.f.request, artifact)
            reference = self.f.runner.publish(self.f.request, artifact)
        self.assertEqual(attempts[0], attempts[1])
        original = self.f.v.assessor.upload
        attempts = []
        with patch.object(self.f.v.assessor, "upload", side_effect=lose):
            with self.assertRaises(RemoteError):
                self.f.assessor.assess(self.f.request, reference)
            self.f.assessor.assess(self.f.request, reference)
        self.assertEqual(attempts[0], attempts[1])

    def test_failed_scenario_never_produces_fixture_assessment(self):
        self.f.target.mutate = lambda r: r["result"].update(ok=False)
        artifact = self.f.runner.execute(self.f.request)
        self.f.now = self.f.end
        reference = self.f.runner.publish(self.f.request, artifact)
        with patch.object(self.f.v.assessor, "upload") as upload, self.assertRaises(ValueError):
            self.f.assessor.assess(self.f.request, reference)
        upload.assert_not_called()

    def test_window_is_enforced_before_and_after_target_effect(self):
        self.f.now = self.f.start - timedelta(seconds=1)
        with self.assertRaises(ValueError):
            self.f.runner.execute(self.f.request)
        self.assertFalse(self.f.target.calls)
        self.f.now = self.f.start
        original = self.f.target.call

        def late(*args):
            value = original(*args)
            self.f.now = self.f.end + timedelta(seconds=1)
            return value

        with patch.object(self.f.target, "call", side_effect=late), self.assertRaises(ValueError):
            self.f.runner.execute(self.f.request)
        with self.assertRaises(ValueError):
            self.f.runner.execute(self.f.request)
        self.assertEqual(len(self.f.target.calls), 1)

    def test_target_binding_and_time_cannot_be_forged(self):
        for mutate in (
            lambda r: r["binding"]["release"].update(artifact_digest="sha256:" + "a" * 64),
            lambda r: r.update(idempotency_key="b" * 64),
            lambda r: r.update(executed_at=(self.f.end + timedelta(seconds=1)).isoformat()),
            lambda r: r.update(result={"huge": "x" * 17000}),
        ):
            self.f.target.responses.clear()
            self.f.target.mutate = mutate
            with self.assertRaises(ValueError):
                self.f.runner.execute(self.f.request)

    def test_policy_requires_complete_independent_reviewed_oracles_and_exact_origin(self):
        for mutate in (
            lambda p: p.update(runner="assessor"),
            lambda p: p["checks"].pop("LOGS-040"),
            lambda p: p["checks"]["TRACES-040"].update(expected=[]),
            lambda p: p["checks"]["PROMQL-040"].update(expected=[True]),
            lambda p: p["target"].update(base_url="http://fixture.example.invalid"),
            lambda p: p["target"].update(base_url="https://fixture.example.invalid/path"),
            lambda p: p["target"].update(authorization_env="SRE_JIRA_API_TOKEN"),
            lambda p: p["scenarios"][0].update(path="//evil.invalid"),
            lambda p: p["scenarios"][0].update(assertions={}),
            lambda p: p["checks"]["LOGS-001"].update(scenario="missing"),
            lambda p: p.update(assessor_queue=p["runner_queue"]),
        ):
            raw = copy.deepcopy(self.f.raw)
            mutate(raw)
            with self.assertRaises(ValueError):
                FixturePolicy.parse(raw)

    def test_missing_or_foreign_runner_proof_is_rejected(self):
        reference, _ = self.f.complete()
        for mutate in (
            lambda r: r.update(producer="assessor"),
            lambda r: r.update(sha256="b" * 64),
            lambda r: r["scope"].update(generation=2),
            lambda r: r["scope"].update(environment_id="preview-other"),
        ):
            ref = copy.deepcopy(reference)
            mutate(ref)
            with self.assertRaises((ValueError, RemoteError)):
                self.f.assessor.assess(self.f.request, ref)

    def test_generation_change_after_target_call_prevents_execution_record(self):
        original = self.f.target.call

        def change(*args):
            value = original(*args)
            self.f.f.registry.start(
                replace(self.f.f.preview, scope=replace(self.f.v.scope, generation=2)),
                1,
                self.f.now,
            )
            return value

        with (
            patch.object(self.f.target, "call", side_effect=change),
            self.assertRaises(RemoteError),
        ):
            self.f.runner.execute(self.f.request)

    def test_current_scope_and_expiration_rechecked_on_saved_proof(self):
        reference, _ = self.f.complete()
        self.f.now = self.f.end + timedelta(minutes=16)
        with self.assertRaises(ValueError):
            self.f.assessor.assess(self.f.request, reference)
        self.f.now = self.f.end
        self.f.f.registry.start(
            replace(self.f.f.preview, scope=replace(self.f.v.scope, generation=2)), 1, self.f.now
        )
        with self.assertRaises(RemoteError):
            self.f.assessor.assess(self.f.request, reference)

    def test_role_credentials_are_minimal_and_do_not_cross_boundaries(self):
        values = {
            "SRE_EVIDENCE_API_TOKEN": "role-token",
            "SRE_FIXTURE_TARGET_AUTH": "target-token",
            "SRE_JIRA_API_TOKEN": "jira",
            "ANTHROPIC_API_KEY": "model",
            "SRE_GITHUB_TOKEN": "github",
        }
        self.assertEqual(
            environment(self.f.policy, "runner", values),
            {"SRE_EVIDENCE_API_TOKEN": "role-token", "SRE_FIXTURE_TARGET_AUTH": "target-token"},
        )
        self.assertEqual(
            environment(self.f.policy, "assessor", values), {"SRE_EVIDENCE_API_TOKEN": "role-token"}
        )

    def test_wrong_plan_policy_window_or_production_dispatch_is_rejected(self):
        for mutate in (
            lambda r: r["plan"]["release"].update(plan_digest="sha256:" + "a" * 64),
            lambda r: r["dispatch"].update(policy_sha256="b" * 64),
            lambda r: r["plan"]["release"]["scope"].update(environment="production"),
            lambda r: r["plan"].update(window_end=self.f.start.isoformat()),
        ):
            raw = copy.deepcopy(self.f.request)
            mutate(raw)
            with self.assertRaises(ValueError):
                FixtureRequest.parse(raw).authorize(self.f.policy)
        self.assertFalse(self.f.target.calls)

    def test_verifier_rejects_tampered_runner_proof_and_gate_rechecks_it(self):
        reference, result = self.f.complete()
        receipt = self.f.engineering.certify()
        original = self.f.v.api.read

        def tamper(ref):
            raw = original(ref)
            if ref.id == reference["id"]:
                doc = json.loads(raw)
                doc["binding"]["release"]["plan_digest"] = "sha256:" + "a" * 64
                return canonical(doc).encode()
            return raw

        with patch.object(self.f.v.api, "read", side_effect=tamper):
            with self.assertRaises(ValueError):
                self.f.v.engine.run(self.f.engineering.args["certification"]["request"]["input"])
            with self.assertRaises(ValueError):
                self.f.v.engine.gate(
                    self.f.engineering.args["certification"]["request"]["input"], receipt
                )

    def test_not_applicable_requires_independent_executed_capability_probe(self):
        from sre_suite.execution.journal import Journal
        from sre_suite.fixtures.runtime import FixtureRuntime

        raw = copy.deepcopy(self.f.raw)
        raw["verification_policy"]["checks"]["TRACES-009"].update(
            applicable=False, source=None, selector={}, parameters={}
        )
        raw["checks"]["TRACES-009"].update(
            expected=[], justification="Reviewed capability probe confirms no gRPC server"
        )
        raw["scenarios"][0]["assertions"] = {"/grpc_supported": False}
        policy = FixturePolicy.parse(raw)
        request = copy.deepcopy(self.f.request)
        request["plan"].pop("digest")
        request["plan"].update(
            policy_sha256=policy.verification.fingerprint, fixture=policy.dispatch
        )
        request["dispatch"] = policy.dispatch
        self.f.target.mutate = lambda r: r["result"].update(grpc_supported=False)
        runner = FixtureRuntime(
            policy,
            self.f.api,
            Journal(self.f.f.root / "na-runner"),
            target=self.f.target,
            clock=lambda: self.f.now,
        )
        assessor = FixtureRuntime(
            policy,
            self.f.v.assessor,
            Journal(self.f.f.root / "na-assessor"),
            clock=lambda: self.f.now,
        )
        artifact = runner.execute(request)
        self.f.now = self.f.end
        result = assessor.assess(request, runner.publish(request, artifact))
        doc = json.loads(self.f.v.assessor.read(evidence_from_wire(result["evidence"])))
        self.assertFalse(doc["checks"]["TRACES-009"]["applicable"])
        self.assertTrue(doc["checks"]["TRACES-009"]["justification"])
        self.assertEqual(len(doc["checks"]), 120)

    def test_private_template_needs_review_and_never_starts_work(self):
        import io
        from contextlib import redirect_stdout

        from sre_suite.fixtures.__main__ import main

        source = self.f.f.root / "verification.json"
        source.write_text(canonical(self.f.v.policy_raw))
        source.chmod(0o600)
        output = self.f.f.root / "template.json"
        with (
            patch(
                "sys.argv",
                [
                    "fixtures",
                    "template",
                    "--verification-policy",
                    str(source),
                    "--output",
                    str(output),
                ],
            ),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(), 0)
            self.assertEqual(main(), 1)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        raw = json.loads(output.read_text())
        self.assertEqual(len(raw["checks"]), 120)
        with self.assertRaises(ValueError):
            FixturePolicy.parse(raw)
        self.assertFalse(self.f.target.calls)

    def test_runner_credential_cannot_publish_assessor_result(self):
        artifact = self.f.runner.execute(self.f.request)
        self.f.now = self.f.end
        reference = self.f.runner.publish(self.f.request, artifact)
        with self.assertRaises(ValueError):
            self.f.runner.assess(self.f.request, reference)

    def test_automatic_handoff_rejects_legacy_or_unrelated_assessment(self):
        self.f.engineering.collect()
        with self.assertRaises(ValueError):
            self.f.engineering.bind()
        self.assertEqual(self.f.f.jira_http.puts, 0)

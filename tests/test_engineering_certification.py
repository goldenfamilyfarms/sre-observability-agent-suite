"""Engineering certification cannot be bypassed by receipts, changed candidates or stale scope."""

import copy
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from sre_suite.execution.certification import EngineeringCertification
from sre_suite.execution.http import RemoteError
from sre_suite.execution.runtime import ExecutionRuntime
from sre_suite.execution.wire import canonical
from tests.engineering_certification_fixture import EngineeringCertificationFixture


class EngineeringCertificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.f = EngineeringCertificationFixture(Path(temporary.name).resolve())
        self.addCleanup(self.f.close)
        self.f.begin()
        self.f.collect()

    def test_real_120_check_certificate_gates_review_readiness(self):
        receipt = self.f.certify()
        report = self.f.runtime.finish(self.f.args)
        self.assertEqual(receipt["verdict"], "PASSED")
        self.assertEqual(report["outcome"], "ready_for_review")
        self.assertEqual(report["certification"]["evidence"], receipt["evidence"])
        self.assertEqual(
            report["certification"]["release"]["artifact_digest"],
            self.f.plan["binding"]["artifact_digest"],
        )
        self.assertEqual(
            report["certification"]["release"]["plan_digest"],
            "sha256:" + self.f.args["reference"]["manifest"],
        )
        self.assertEqual(self.f.runtime.finish(self.f.args), report)

    def test_missing_certificate_and_stripped_marker_cannot_bypass(self):
        self.f.bind()
        self.f.args["reference"].pop("certification_required")
        with self.assertRaises(ValueError):
            self.f.runtime.finish(self.f.args)
        self.assertEqual(self.f.f.jira_http.puts, 0)

    def test_failed_incomplete_and_fixture_timeout_never_become_ready(self):
        self.f.bind()
        for verdict in ("FAILED", "INCOMPLETE"):
            self.f.args["certification"] = {
                "request": None,
                "receipt": {"verdict": verdict, "evidence": None},
            }
            # Separate result journal for each outcome avoids overwriting immutable reports.
            with (
                patch.object(self.f.runtime.jira, "publish", return_value=False),
                patch.object(
                    self.f.runtime.journal, "compute", side_effect=lambda key, req, op, hb: op()
                ),
            ):
                report = self.f.runtime.finish(self.f.args)
                self.assertEqual(report["outcome"], "blocked")
                self.assertFalse(report["certification"]["ready"])

    def test_tampered_plan_input_receipt_and_fixture_fail_closed(self):
        self.f.certify()
        for change in (
            lambda a: a["certification_plan"]["release"].update(
                artifact_digest="sha256:" + "a" * 64
            ),
            lambda a: a["certification"]["request"]["input"]["release"].update(
                plan_digest="sha256:" + "b" * 64
            ),
            lambda a: a["certification"]["receipt"].update(report_sha256="c" * 64),
            lambda a: a["certification_fixture"].update(producer="worker-one"),
            lambda a: a["certification_fixture"]["scope"].update(environment_id="preview-other"),
            lambda a: a["certification"]["request"]["input"]["release"]["scope"].update(
                generation=2
            ),
            lambda a: a["certification_plan"].update(window_end="2026-01-01T00:00:00+00:00"),
        ):
            args = copy.deepcopy(self.f.args)
            change(args)
            with self.assertRaises((ValueError, RemoteError)):
                self.f.runtime.finish(args)
        self.assertEqual(self.f.f.jira_http.puts, 0)

    def test_unrelated_but_authentic_assessor_fixture_rejected(self):
        self.f.args["telemetry"] = {"plan": self.f.plan, "reports": self.f.reports}
        self.f.args["certification_fixture"] = self.f.fixture_ref
        original = self.f.runtime.evidence.read
        import json

        def change(ref):
            raw = original(ref)
            if ref.producer == "assessor":
                doc = json.loads(raw)
                doc["release"]["plan_digest"] = "sha256:" + "a" * 64
                return canonical(doc).encode()
            return raw

        with (
            patch.object(self.f.runtime.evidence, "read", side_effect=change),
            self.assertRaises(ValueError),
        ):
            self.f.bind()

    def test_policy_change_or_disabled_gate_cannot_reuse_execution(self):
        for settings in (replace(self.f.runtime.settings, certification_config=None),):
            r = ExecutionRuntime(
                settings,
                jira=self.f.runtime.jira,
                evidence=self.f.runtime.evidence,
                tests=self.f.runtime.tests,
                clock=self.f.runtime.clock,
            )
            with self.assertRaises(ValueError):
                r.prepare(self.f.f.spec)
        self.f.config["fixture_wait_seconds"] = 9
        self.f.path.write_text(canonical(self.f.config))
        r = ExecutionRuntime(
            self.f.runtime.settings,
            jira=self.f.runtime.jira,
            evidence=self.f.runtime.evidence,
            tests=self.f.runtime.tests,
            clock=self.f.runtime.clock,
        )
        with self.assertRaises(ValueError):
            r.prepare(self.f.f.spec)

    def test_private_config_requires_exact_sources_and_independent_identities(self):
        for change in (
            lambda c: c.update(task_queue="bad queue"),
            lambda c: c.update(fixture_wait_seconds=True),
            lambda c: c["policy"]["sources"][0].update(step_seconds=30),
            lambda c: c["policy"].update(collector="other"),
            lambda c: c["policy"].update(assessor=c["policy"]["collector"]),
            lambda c: c["policy"]["sources"][0]["profile"].update(query_template="changed${scope}"),
        ):
            c = copy.deepcopy(self.f.config)
            change(c)
            self.f.path.write_text(canonical(c))
            with self.assertRaises(ValueError):
                EngineeringCertification(self.f.path, self.f.runtime.telemetry)
        with self.assertRaises(ValueError):
            EngineeringCertification(self.f.path, None)

    def test_saved_input_retry_is_identical_and_fixture_substitution_conflicts(self):
        request = self.f.bind()
        self.assertEqual(self.f.bind(), request)
        self.f.fixture_ref = copy.deepcopy(self.f.fixture_ref)
        self.f.fixture_ref["sha256"] = "b" * 64
        with self.assertRaises((ValueError, RemoteError)):
            self.f.bind()

    def test_expiry_and_generation_are_rechecked_before_publication(self):
        self.f.certify()
        now = self.f.runtime.clock()
        self.f.runtime.clock = lambda: now + timedelta(minutes=16)
        with self.assertRaises((ValueError, RemoteError)):
            self.f.runtime.finish(self.f.args)
        self.f.runtime.clock = lambda: now
        self.f.f.registry.start(
            replace(self.f.f.preview, scope=replace(self.f.v.scope, generation=2)), 1, now
        )
        with self.assertRaises(RemoteError):
            self.f.runtime.finish(self.f.args)
        self.assertEqual(self.f.f.jira_http.puts, 0)

    def test_generation_changes_during_publication_guard_blocks_result(self):
        self.f.certify()

        def publish(key, execution, report, guard):
            self.f.f.registry.start(
                replace(self.f.f.preview, scope=replace(self.f.v.scope, generation=2)),
                1,
                self.f.runtime.clock(),
            )
            guard()
            return True

        with (
            patch.object(self.f.runtime.jira, "publish", side_effect=publish),
            self.assertRaises(RemoteError),
        ):
            self.f.runtime.finish(self.f.args)

    def test_lost_jira_ack_reuses_exact_certificate_and_report(self):
        self.f.certify()
        self.f.f.jira_http.lose_response = True
        with self.assertRaises(RemoteError):
            self.f.runtime.finish(self.f.args)
        report = self.f.runtime.finish(self.f.args)
        self.assertEqual(report["outcome"], "ready_for_review")
        self.assertEqual(self.f.f.jira_http.puts, 1)


class CertifiedPublicationTests(unittest.TestCase):
    def test_certification_gates_jira_review_transition_and_rechecks_draft_head(self):
        with tempfile.TemporaryDirectory() as temp:
            f = EngineeringCertificationFixture(Path(temp).resolve(), publication=True)
            self.addCleanup(f.close)
            f.begin()
            github, jira, _ = f.publication
            self.assertEqual(jira.status, "200")
            f.collect()
            f.certify()
            report = f.runtime.finish(f.args)
            self.assertEqual(report["outcome"], "ready_for_review")
            self.assertEqual(jira.status, "300")
            self.assertEqual(len(github.pulls), 1)
            github.refs[report["pull_request"]["branch"]] = "a" * 40
            with self.assertRaises(ValueError):
                f.runtime.finish(f.args)

    def test_actual_failed_predicate_blocks_jira_review_transition(self):
        import json
        from dataclasses import asdict
        from datetime import datetime

        with tempfile.TemporaryDirectory() as temp:
            f = EngineeringCertificationFixture(Path(temp).resolve(), publication=True)
            self.addCleanup(f.close)
            f.begin()
            f.collect()
            f.v.assessments["PROMQL-040"]["expected"] = [99]
            with (
                patch(
                    "tests.verification_fixture.START",
                    datetime.fromisoformat(f.cert_plan["window_start"]),
                ),
                patch(
                    "tests.verification_fixture.END",
                    datetime.fromisoformat(f.cert_plan["window_end"]),
                ),
                patch("tests.verification_fixture.NOW", f.runtime.clock()),
            ):
                f.fixture_ref = json.loads(canonical(asdict(f.v.make_input().fixture)))
            receipt = f.certify()
            self.assertEqual(receipt["verdict"], "FAILED")
            report = f.runtime.finish(f.args)
            self.assertEqual(report["outcome"], "blocked")
            _, jira, _ = f.publication
            self.assertNotEqual(jira.status, "300")

"""Every catalog predicate plus authenticated full-suite failure/recovery boundaries."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from sre_suite.certification import load_catalog
from sre_suite.execution.http import RemoteError
from sre_suite.execution.journal import Journal
from sre_suite.execution.wire import canonical
from sre_suite.telemetry.contracts import epoch_ns
from sre_suite.verification.__main__ import main
from sre_suite.verification.config import environment
from sre_suite.verification.contracts import VerificationInput, VerificationPolicy
from sre_suite.verification.predicates import evaluate, validate
from sre_suite.verification.runtime import CertificationEngine, identity
from tests.test_foundation import NOW
from tests.verification_fixture import END, START, VerificationFixture, case, negative


class CatalogPredicates(unittest.TestCase):
    pass


def predicate_test(check, bad):
    def test(self):
        p, rows, expected, context = case(check)
        operator = check.assertion.removesuffix(".v1")
        validate(operator, p)
        if bad:
            rows, expected, context = negative(check, p, rows, expected, context)
        self.assertIs(
            evaluate(operator, p, rows, expected, epoch_ns(START), epoch_ns(END), context), not bad
        )

    return test


for definition in load_catalog().checks:
    for bad in (False, True):
        setattr(
            CatalogPredicates,
            f"test_{definition.id.replace('-', '_')}_{'negative' if bad else 'positive'}",
            predicate_test(definition, bad),
        )


class VerificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.f = VerificationFixture(self.root)
        self.addCleanup(self.f.close)

    def test_120_bound_checks_pass_and_authenticated_report_roundtrips(self):
        report = self.f.engine.run(self.f.input.wire())
        self.assertEqual(report["verdict"], "PASSED")
        self.assertEqual(len(report["results"]), 120)
        self.assertTrue(
            all(r["outcome"] == "PASS" and r["assertion_passed"] is True for r in report["results"])
        )
        receipt = self.f.engine.run(self.f.input.wire(), publish=True)
        from sre_suite.api_wire import evidence_from_wire

        self.assertEqual(
            json.loads(self.f.api.read(evidence_from_wire(receipt["evidence"]))), report
        )
        self.assertEqual(receipt["evidence"]["producer"], "verifier")
        gate = self.f.engine.gate(self.f.input.wire(), receipt)
        self.assertTrue(gate.passed)
        self.assertEqual(gate.release, self.f.release)
        with self.assertRaises(ValueError):
            self.f.engine.gate(self.f.input.wire(), {**receipt, "report_sha256": "e" * 64})
        self.assertFalse(report["deployed"])
        self.assertFalse(report["merged"])

    def test_gate_rechecks_expiration_and_current_generation(self):
        receipt = self.f.engine.run(self.f.input.wire(), publish=True)
        self.f.engine.clock = lambda: NOW + timedelta(hours=1)
        with self.assertRaises(ValueError):
            self.f.engine.gate(self.f.input.wire(), receipt)
        self.f.engine.clock = lambda: NOW
        with patch.object(self.f.api, "context", side_effect=ValueError("stale generation")):
            with self.assertRaises(ValueError):
                self.f.engine.gate(self.f.input.wire(), receipt)

    def test_mapping_numeric_enum_links_and_async_span_timing(self):
        def run(op, p, rows, expected=(), context=()):
            validate(op, p)
            return evaluate(op, p, rows, expected, epoch_ns(START), epoch_ns(END), context)

        p = {
            "source_path": "/status",
            "target_path": "/severity",
            "pairs": [{"source": 500, "target": "ERROR"}],
        }
        self.assertTrue(run("mapping", p, [{"status": 500, "severity": "ERROR"}]))
        self.assertFalse(run("mapping", p, [{"status": 500, "severity": "INFO"}]))
        self.assertTrue(run("enum", {"path": "/kind", "values": [2]}, [{"kind": 2}]))
        self.assertFalse(run("enum", {"path": "/kind", "values": [1]}, [{"kind": True}]))
        link = {"trace_id": "a" * 32, "span_id": "b" * 16}
        self.assertTrue(run("links", {}, [{"links": [link]}], [link]))
        self.assertFalse(run("links", {}, [{"links": [{**link, "trace_id": "c" * 32}]}], [link]))
        parent = {
            "trace_id": "a",
            "span_id": "p",
            "parent_span_id": "",
            "start_ns": 10,
            "end_ns": 20,
        }
        child = {
            "trace_id": "a",
            "span_id": "c",
            "parent_span_id": "p",
            "start_ns": 15,
            "end_ns": 30,
        }
        self.assertTrue(
            run("timing", {"maximum_clock_skew_ns": 0}, [child], context=[parent, child])
        )
        child["start_ns"] = 9
        self.assertFalse(
            run("timing", {"maximum_clock_skew_ns": 0}, [child], context=[parent, child])
        )
        self.assertTrue(
            run("timing", {"maximum_clock_skew_ns": 1}, [child], context=[parent, child])
        )

    def test_lost_report_ack_reuses_exact_bytes_and_timestamps(self):
        original = self.f.api.upload
        calls = []

        def lose_once(*args):
            calls.append(args)
            result = original(*args)
            if len(calls) == 1:
                raise RemoteError(0)
            return result

        with patch.object(self.f.api, "upload", side_effect=lose_once):
            with self.assertRaises(RemoteError):
                self.f.engine.run(self.f.input.wire(), publish=True)
            self.f.engine.clock = lambda: NOW + timedelta(seconds=1)
            self.f.engine.run(self.f.input.wire(), publish=True)
        self.assertEqual(calls[0], calls[1])

    def test_mandatory_assertion_failure_produces_failed_report(self):
        self.f.assessments["PROMQL-040"]["expected"] = [99]
        changed = self.f.make_input()
        report = self.f.engine.run(changed.wire())
        self.assertEqual(report["verdict"], "FAILED")
        self.assertEqual(
            next(r for r in report["results"] if r["check_id"] == "PROMQL-040")["outcome"], "FAIL"
        )

    def test_missing_observations_account_for_all_slots_and_never_pass(self):
        report = self.f.engine.run(replace(self.f.input, observations=()).wire())
        self.assertEqual(report["verdict"], "INCOMPLETE")
        self.assertEqual(len(report["results"]), 120)
        self.assertEqual({r["outcome"] for r in report["results"]}, {"INCONCLUSIVE"})

    def test_partial_and_empty_observations_are_inconclusive(self):
        original = self.f.api.read
        for status in ("partial", "empty"):
            # Malformed downstream contents are still evaluated conservatively even with a
            # trusted test transport; real EvidenceClient additionally checks content hashes.
            def change(ref, status=status):
                raw = original(ref)
                value = json.loads(raw)
                if value.get("kind") == "telemetry-observation":
                    value["result"]["status"] = status
                return canonical(value).encode()

            with patch.object(self.f.api, "read", side_effect=change):
                engine = CertificationEngine(
                    self.f.policy, self.f.api, Journal(self.root / status), clock=lambda: NOW
                )
                report = engine.run(self.f.input.wire())
                self.assertEqual(report["verdict"], "INCOMPLETE")
                self.assertFalse(any(r["outcome"] == "PASS" for r in report["results"]))

    def test_policy_catalog_and_slot_corruption_rejected_before_reads(self):
        for mutate in (
            lambda p: p["checks"].pop("LOGS-040"),
            lambda p: p["checks"].update(UNKNOWN=copy.deepcopy(p["checks"]["LOGS-001"])),
            lambda p: p.update(catalog_sha256="a" * 64),
            lambda p: p.update(assessor=p["collector"]),
            lambda p: p["checks"]["LOGS-001"].update(applicable=False, source=None, parameters={}),
            lambda p: p["checks"]["PROMQL-001"]["parameters"].update(minimum=True),
            lambda p: p["checks"]["PROMQL-001"].update(source="loki"),
            lambda p: p["checks"]["PROMQL-001"]["parameters"].update(code="eval('bad')"),
        ):
            raw = copy.deepcopy(self.f.policy_raw)
            mutate(raw)
            with self.assertRaises(ValueError):
                VerificationPolicy.parse(raw)

    def test_applicability_requires_independent_bound_fixture(self):
        self.f.policy_raw["checks"]["TRACES-009"] = {
            "applicable": False,
            "source": None,
            "selector": {},
            "parameters": {},
            "minimum_records": 1,
        }
        self.f.policy = VerificationPolicy.parse(self.f.policy_raw)
        self.f.assessments["TRACES-009"].update(
            applicable=False, justification="Inventory confirms no gRPC capability"
        )
        value = self.f.make_input()
        engine = CertificationEngine(
            self.f.policy, self.f.api, Journal(self.root / "na"), clock=lambda: NOW
        )
        report = engine.run(value.wire())
        self.assertEqual(report["verdict"], "PASSED")
        self.assertEqual(
            next(r for r in report["results"] if r["check_id"] == "TRACES-009")["outcome"],
            "NOT_APPLICABLE",
        )
        self.f.assessments["TRACES-009"]["justification"] = ""
        self.assertEqual(engine.run(self.f.make_input().wire())["verdict"], "INCOMPLETE")

    def test_wrong_candidate_plan_window_or_fixture_producer_cannot_pass(self):
        for change in (
            {"release": replace(self.f.release, artifact_digest="sha256:" + "e" * 64)},
            {"release": replace(self.f.release, plan_digest="sha256:" + "f" * 64)},
            {"window_start": START - timedelta(seconds=1)},
            {"fixture": replace(self.f.input.fixture, producer="worker-one")},
        ):
            value = replace(self.f.input, **change)
            self.assertEqual(self.f.engine.run(value.wire())["verdict"], "INCOMPLETE")
        with self.assertRaises(ValueError):
            self.f.engine.run(replace(self.f.input, policy_sha256="f" * 64).wire())

    def test_query_hash_returned_labels_and_observed_time_tampering_cannot_pass(self):
        original = self.f.api.read
        for index, mutate in enumerate(
            (
                lambda d: d["result"].update(query_sha256="f" * 64),
                lambda d: d["request"]["binding"].update(plan_digest="sha256:" + "e" * 64),
                lambda d: d.update(observed_at=START.isoformat()),
            )
        ):

            def change(ref, mutate=mutate):
                value = json.loads(original(ref))
                if value.get("kind") == "telemetry-observation":
                    mutate(value)
                return canonical(value).encode()

            with patch.object(self.f.api, "read", side_effect=change):
                engine = CertificationEngine(
                    self.f.policy,
                    self.f.api,
                    Journal(self.root / f"tamper-{index}"),
                    clock=lambda: NOW,
                )
                self.assertEqual(engine.run(self.f.input.wire())["verdict"], "INCOMPLETE")

    def test_cached_certificate_rechecks_expiry_generation_and_context(self):
        self.f.engine.run(self.f.input.wire())
        self.f.engine.clock = lambda: NOW + timedelta(minutes=16)
        with self.assertRaises(ValueError):
            self.f.engine.run(self.f.input.wire(), publish=True)
        self.f.engine.clock = lambda: NOW
        self.f.fixture.registry.start(
            replace(self.f.fixture.preview, scope=replace(self.f.scope, generation=2)), 1, NOW
        )
        with self.assertRaises(RemoteError):
            self.f.engine.run(self.f.input.wire(), publish=True)

    def test_unknown_duplicate_source_and_budget_stop(self):
        raw = self.f.input.wire()
        raw["observations"].append(raw["observations"][0])
        with self.assertRaises(ValueError):
            VerificationInput.parse(raw)
        raw = self.f.input.wire()
        raw["observations"][0]["source"] = "unknown"
        with self.assertRaises(ValueError):
            self.f.engine.run(raw)
        ticks = iter([0, 0, 2])
        engine = CertificationEngine(
            self.f.policy,
            self.f.api,
            Journal(self.root / "deadline"),
            clock=lambda: NOW,
            monotonic=lambda: next(ticks),
            budget_seconds=1,
        )
        with self.assertRaises(TimeoutError):
            engine.run(self.f.input.wire())

    def test_transient_evidence_error_retries_instead_of_freezing_incomplete(self):
        with patch.object(self.f.api, "read", side_effect=RemoteError(503)):
            with self.assertRaises(RemoteError):
                self.f.engine.run(self.f.input.wire())
        self.assertEqual(self.f.engine.run(self.f.input.wire())["verdict"], "PASSED")

    def test_safe_template_private_policy_and_minimal_process_environment(self):
        path = self.root / "template.json"
        with (
            patch("sys.argv", ["verify", "template", "--output", str(path)]),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(), 0)
            self.assertEqual(main(), 1)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            VerificationPolicy.parse(json.loads(path.read_text()))
        self.assertEqual(
            environment(
                {
                    "SRE_EVIDENCE_API_TOKEN": "x",
                    "SRE_JIRA_API_TOKEN": "y",
                    "SRE_TELEMETRY_TEMPO_AUTH": "z",
                    "PYTHONPATH": "bad",
                }
            ),
            {"SRE_EVIDENCE_API_TOKEN": "x"},
        )

    def test_local_cli_writes_the_supplied_bundle_report_privately(self):
        policy = self.root / "policy.json"
        bundle = self.root / "input.json"
        output = self.root / "report.json"
        policy.write_text(canonical(self.f.policy_raw))
        bundle.write_text(canonical(self.f.input.wire()))
        policy.chmod(0o600)
        bundle.chmod(0o600)
        journal = self.root / "cli-journal"
        journal.mkdir(mode=0o700)
        with (
            patch(
                "sys.argv",
                [
                    "verify",
                    "run",
                    "--policy",
                    str(policy),
                    "--input",
                    str(bundle),
                    "--journal",
                    str(journal),
                    "--output",
                    str(output),
                ],
            ),
            patch("sre_suite.verification.__main__.client", return_value=self.f.api),
            patch(
                "sre_suite.verification.__main__.CertificationEngine", return_value=self.f.engine
            ),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(), 0)
        self.assertEqual(json.loads(output.read_text())["verdict"], "PASSED")
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_unicode_and_escaped_sensitive_canaries_are_checked_as_text(self):
        self.assertFalse(
            evaluate(
                "excluded",
                {"paths": ["/body"], "forbidden": ["sëcret"]},
                [{"body": {"nested": ["prefix sëcret suffix"]}}],
                [],
                0,
                1,
                [],
            )
        )
        self.assertTrue(
            evaluate(
                "excluded",
                {"paths": ["/body"], "forbidden": ["sëcret"]},
                [{"body": {"nested": ["redacted"]}}],
                [],
                0,
                1,
                [],
            )
        )

    def test_context_version_change_and_invalid_measurements_never_pass(self):
        with self.assertRaises(RemoteError):
            self.f.engine.run(replace(self.f.input, context_version=1).wire())
        for parameters in (
            {"minimum": float("nan"), "maximum": 2, "minimum_samples": 2},
            {"minimum": 0, "maximum": 2, "minimum_samples": True},
        ):
            with self.assertRaises(ValueError):
                validate("metric_range", parameters)
        with self.assertRaises(ValueError):
            validate("monotonic", {"maximum_resets": 0, "minimum_samples": 1})
        self.assertFalse(
            evaluate(
                "available", {"minimum_samples": 1}, [{"samples": [{"value": 0}]}], [], 0, 1, []
            )
        )

    def test_input_identity_changes_with_evidence_or_binding(self):
        altered = replace(
            self.f.input, release=replace(self.f.release, plan_digest="sha256:" + "e" * 64)
        )
        self.assertNotEqual(identity(self.f.input), identity(altered))
        self.assertEqual(VerificationInput.parse(self.f.input.wire()), self.f.input)

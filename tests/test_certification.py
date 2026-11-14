import unittest
from dataclasses import replace
from datetime import timedelta

from sre_suite.certification import Catalog, CheckResult, Outcome, certify, load_catalog
from sre_suite.contracts import Release
from sre_suite.demo import example_ticket
from tests.test_foundation import NOW


class CertificationTests(unittest.TestCase):
    def setUp(self):
        ticket = example_ticket(NOW)
        self.release = Release(ticket.scope, "sha256:" + "c" * 64, "sha256:" + "d" * 64, ("ci",))
        executable = load_catalog()
        self.draft = Catalog(
            "test-draft",
            tuple(replace(c, query_template=None, assertion=None) for c in executable.checks),
        )
        # Fake adapter definitions exercise aggregation only, not real telemetry checks.
        self.catalog = Catalog(
            "test-only",
            tuple(
                replace(check, query_template="synthetic fixture", assertion="fixture is true")
                for check in self.draft.checks
            ),
        )
        self.start, self.end = NOW - timedelta(minutes=2), NOW
        self.results = tuple(
            CheckResult(
                check.id,
                self.catalog.version,
                self.release,
                self.start,
                self.end,
                Outcome.PASS,
                ticket.evidence,
                True,
            )
            for check in self.catalog.checks
        )

    def report(self, results=None, catalog=None):
        return certify(
            catalog or self.catalog,
            self.release,
            self.start,
            self.end,
            self.results if results is None else results,
            NOW,
        )

    def test_catalog_has_120_unique_slots_and_exact_category_counts(self):
        self.assertEqual(len(self.draft.checks), 120)
        self.assertFalse(any(check.implemented for check in self.draft.checks))
        with self.assertRaises(ValueError):
            replace(self.catalog, checks=self.catalog.checks[:-1])
        with self.assertRaises(ValueError):
            replace(self.catalog, checks=self.catalog.checks[:-1] + (self.catalog.checks[0],))

    def test_synthetic_complete_report_passes(self):
        self.assertEqual(self.report().verdict, "PASSED")

    def test_draft_catalog_never_produces_certificate(self):
        results = tuple(
            replace(result, catalog_version=self.draft.version) for result in self.results
        )
        self.assertEqual(self.report(results, self.draft).verdict, "INCOMPLETE")
        not_applicable = tuple(
            replace(
                result,
                outcome=Outcome.NOT_APPLICABLE,
                justification="synthetic unsupported capability",
            )
            for result in results
        )
        self.assertEqual(self.report(not_applicable, self.draft).verdict, "INCOMPLETE")

    def test_missing_duplicate_and_unknown_results_incomplete(self):
        for results in (
            self.results[:-1],
            self.results + self.results[:1],
            (replace(self.results[0], check_id="UNKNOWN"),) + self.results[1:],
        ):
            with self.subTest(count=len(results)):
                self.assertEqual(self.report(results).verdict, "INCOMPLETE")

    def test_query_success_without_assertion_is_not_pass(self):
        results = (replace(self.results[0], assertion_passed=None),) + self.results[1:]
        self.assertEqual(self.report(results).verdict, "INCOMPLETE")

    def test_mandatory_failure_fails_certification(self):
        results = (
            replace(self.results[0], outcome=Outcome.FAIL, assertion_passed=False),
        ) + self.results[1:]
        self.assertEqual(self.report(results).verdict, "FAILED")

    def test_error_and_inconclusive_do_not_pass(self):
        for outcome in (Outcome.ERROR, Outcome.INCONCLUSIVE):
            with self.subTest(outcome=outcome):
                results = (replace(self.results[0], outcome=outcome),) + self.results[1:]
                self.assertEqual(self.report(results).verdict, "INCOMPLETE")

    def test_not_applicable_requires_justification_and_evidence(self):
        first = replace(self.results[0], outcome=Outcome.NOT_APPLICABLE, assertion_passed=None)
        self.assertEqual(self.report((first,) + self.results[1:]).verdict, "INCOMPLETE")
        justified = replace(first, justification="Capability absent in scoped inventory")
        self.assertEqual(self.report((justified,) + self.results[1:]).verdict, "PASSED")
        self.assertEqual(
            self.report((replace(justified, evidence=()),) + self.results[1:]).verdict, "INCOMPLETE"
        )

    def test_wrong_artifact_window_version_and_evidence_rejected(self):
        first = self.results[0]
        bad = (
            replace(first, catalog_version="old"),
            replace(first, release=replace(self.release, artifact_digest="sha256:" + "f" * 64)),
            replace(first, window_start=self.start - timedelta(minutes=1)),
            replace(first, evidence=()),
            replace(first, evidence=(replace(first.evidence[0], expires_at=NOW),)),
            replace(
                first,
                evidence=(
                    replace(first.evidence[0], observed_at=self.start - timedelta(seconds=1)),
                ),
            ),
        )
        for result in bad:
            with self.subTest(result=result):
                self.assertEqual(self.report((result,) + self.results[1:]).verdict, "INCOMPLETE")

    def test_future_window_rejected(self):
        with self.assertRaises(ValueError):
            certify(
                self.catalog,
                self.release,
                self.start,
                NOW + timedelta(seconds=1),
                self.results,
                NOW,
            )

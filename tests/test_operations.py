"""Independent assertions, complete regression accounting and exact source citations."""

import copy
import unittest
from dataclasses import asdict

from sre_suite.certification import load_catalog
from sre_suite.demo import example_ticket
from sre_suite.operations.runtime import regression, topology
from sre_suite.operations.standards import inspect
from sre_suite.verification.contracts import catalog_digest
from tests.test_foundation import NOW


class OperationsTests(unittest.TestCase):
    def test_ast_resolves_import_aliases_and_reports_static_limits(self):
        result = inspect(
            {
                "app.py": "import requests as client\nclient.get('https://example.invalid')\n",
                "bad.py": "broken(",
            },
            {"files": [{"path": p, "sha256": "a" * 64} for p in ("app.py", "bad.py")]},
        )
        self.assertEqual(result["calls"][0]["symbol"], "requests.get")
        self.assertEqual(result["calls"][0]["line"], 2)
        self.assertEqual(result["parse_errors"], ["bad.py"])
        self.assertFalse(result["runtime_verified"])
        self.assertEqual(result["controls"]["tracing"]["status"], "not_observed")

    def test_topology_requires_parent_causality_and_never_infers_missing_edges(self):
        parent = {
            "trace_id": "a",
            "span_id": "b",
            "parent_span_id": None,
            "resource": {"service.name": "frontend"},
            "_in_scope": True,
        }
        child = {
            "trace_id": "a",
            "span_id": "c",
            "parent_span_id": "b",
            "resource": {"service.name": "backend"},
            "_in_scope": False,
        }
        expected = [{"source": "frontend", "target": "backend"}]
        self.assertTrue(topology({"trace": [parent, child]}, expected)["passed"])
        self.assertFalse(topology({"trace": [parent]}, expected)["passed"])
        changed = {**child, "trace_id": "other"}
        self.assertFalse(topology({"trace": [parent, changed]}, expected)["passed"])
        with self.assertRaises(ValueError):
            topology({"trace": [parent, child, {**child, "parent_span_id": "z"}]}, expected)

    def reports(self):
        scope = example_ticket(NOW).scope
        report = {
            "schema_version": 2,
            "kind": "telemetry-certification",
            "catalog_sha256": catalog_digest(load_catalog()),
            "policy_sha256": "p",
            "artifact_kind": "container-image-v1",
            "release": {
                "scope": asdict(scope),
                "artifact_digest": "sha256:" + "a" * 64,
                "plan_digest": "sha256:" + "b" * 64,
            },
            "results": [{"check_id": c.id, "outcome": "PASS"} for c in load_catalog().checks],
        }
        return (
            scope,
            {"baseline": report, "candidate": copy.deepcopy(report)},
            {
                "baseline_artifact_digest": report["release"]["artifact_digest"],
                "baseline_plan_digest": report["release"]["plan_digest"],
            },
        )

    def test_regression_counts_all_120_and_na_cannot_hide_a_previous_pass(self):
        scope, reports, parameters = self.reports()
        args = (scope, "sha256:" + "a" * 64, "sha256:" + "b" * 64, parameters)
        self.assertTrue(regression(reports, *args)["passed"])
        reports["candidate"]["results"][0]["outcome"] = "NOT_APPLICABLE"
        result = regression(reports, *args)
        self.assertFalse(result["passed"])
        self.assertEqual(len(result["regressions"]), 1)
        reports["candidate"]["results"].pop()
        with self.assertRaises(ValueError):
            regression(reports, *args)

    def test_regression_refuses_other_generation_artifact_or_policy(self):
        for mutation in ("generation", "artifact_digest", "policy_sha256"):
            scope, reports, parameters = self.reports()
            if mutation == "generation":
                reports["candidate"]["release"]["scope"]["generation"] += 1
            elif mutation == "artifact_digest":
                reports["candidate"]["release"][mutation] = "sha256:" + "f" * 64
            else:
                reports["candidate"][mutation] = "changed"
            with self.assertRaises(ValueError):
                regression(reports, scope, "sha256:" + "a" * 64, "sha256:" + "b" * 64, parameters)

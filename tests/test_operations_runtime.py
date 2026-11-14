"""Analysis outputs use real authenticated storage and remain bound to the preview generation."""

import copy
import json
import unittest
from dataclasses import asdict
from datetime import timedelta
from unittest.mock import patch

from sre_suite.certification import load_catalog
from sre_suite.discovery.source import snapshot
from sre_suite.execution.evidence_client import EvidenceClient
from sre_suite.execution.journal import Journal
from sre_suite.execution.wire import canonical, ticket_json
from sre_suite.operations.runtime import AnalysisAgent
from sre_suite.operations.standards import POLICY
from sre_suite.verification.contracts import catalog_digest
from tests import test_control
from tests.test_foundation import NOW
from tests.verification_fixture import TokenTransport


class AnalysisRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.f = test_control.ControlTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.agent = AnalysisAgent(self.f.api, Journal(self.f.root / "analyses"))
        self.raw = {
            "schema_version": 2,
            "role": "standards",
            "scope": asdict(self.f.scope),
            "context_version": 0,
            "artifact_digest": "sha256:" + "a" * 64,
            "plan_digest": "sha256:" + "b" * 64,
            "producer": "worker-one",
            "inputs": {},
            "parameters": {},
            "expires_at": (NOW + timedelta(minutes=25)).isoformat(),
        }
        clock = patch("sre_suite.operations.runtime.datetime")
        clock.start().now.return_value = NOW
        self.addCleanup(clock.stop)

    def upload(self, name, doc):
        token = self.f.tokens["agent"]
        client = EvidenceClient(
            "https://evidence.invalid", token, TokenTransport(self.f.app, token)
        )
        ref = client.upload(
            name, self.f.scope, canonical(doc).encode(), NOW, NOW + timedelta(minutes=30)
        )
        return json.loads(canonical({"producer": "agent", "evidence": asdict(ref)}))

    def test_standards_reads_exact_git_objects_and_retries_same_evidence(self):
        f = self.f.f
        inventory, _ = snapshot(
            f.settings.repository_path,
            f.ticket.repository,
            f.ticket.scope.revision,
            ("app.py", "test_app.py"),
        )
        self.raw["artifact_digest"] = inventory["artifact_digest"]
        self.raw["parameters"] = {
            "repository_dir": str(f.settings.repository_path),
            "repository": f.ticket.repository,
            "files": ["app.py", "test_app.py"],
            "policy_sha256": POLICY,
        }
        result = self.agent.run(self.raw)
        self.assertEqual(result["evidence"], self.agent.run(self.raw)["evidence"])
        self.assertFalse(result["passed"])
        self.raw["artifact_digest"] = "sha256:" + "c" * 64
        with self.assertRaises(ValueError):
            self.agent.run(self.raw)

    def test_failed_certificate_generates_scoped_reviewable_remediation_plan(self):
        report = {
            "schema_version": 2,
            "kind": "telemetry-certification",
            "catalog_sha256": catalog_digest(load_catalog()),
            "policy_sha256": "p",
            "artifact_kind": "container-image-v1",
            "release": {
                "scope": self.raw["scope"],
                "artifact_digest": self.raw["artifact_digest"],
                "plan_digest": self.raw["plan_digest"],
            },
            "results": [{"check_id": c.id, "outcome": "PASS"} for c in load_catalog().checks],
        }
        report["results"][0]["outcome"] = "INCONCLUSIVE"
        requirements = ticket_json(self.f.f.ticket)
        for key in ("key", "scope", "repository", "dependencies"):
            requirements.pop(key)
        template = {
            "schema_version": 2,
            "id": "original",
            "scope": self.raw["scope"],
            "project": "DEMO",
            "repository": self.f.f.ticket.repository,
            "nodes": [
                {
                    "id": "repair",
                    "summary": "Repair gaps",
                    "depends_on": [],
                    "requirements": requirements,
                }
            ],
        }
        self.raw.update(
            role="remediation",
            inputs={"candidate": self.upload("failed-report", report)},
            parameters={"template_plan": template},
        )
        result = self.agent.run(self.raw)
        self.assertFalse(result["passed"])
        proposal = result["result"]
        self.assertEqual(proposal["outcome"], "remediation_proposed")
        self.assertFalse(proposal["jira_published"])
        self.assertEqual(proposal["plan"]["scope"], self.raw["scope"])
        self.assertEqual(len(proposal["plan"]["nodes"]), 2)

    def test_runtime_maturity_requires_all_independent_exact_artifact_inputs(self):
        docs = {
            "standards": {"controls": {"tracing": {"status": "observed_static"}}},
            "topology": {"passed": True},
            "pipeline-health": {"passed": True},
        }
        inputs = {}
        for name, result in docs.items():
            inputs[name] = self.upload(
                name,
                {
                    "kind": name + "-analysis",
                    "scope": self.raw["scope"],
                    "artifact_digest": self.raw["artifact_digest"],
                    "plan_digest": self.raw["plan_digest"],
                    "result": result,
                },
            )
        self.raw.update(
            role="assessment", inputs=inputs, parameters={"required_controls": ["tracing"]}
        )
        result = self.agent.run(self.raw)
        self.assertEqual(result["result"]["maturity_score"], 2)
        self.assertFalse(result["result"]["certified"])
        changed = copy.deepcopy(self.raw)
        changed["plan_digest"] = "sha256:" + "f" * 64
        with self.assertRaises(ValueError):
            self.agent.run(changed)

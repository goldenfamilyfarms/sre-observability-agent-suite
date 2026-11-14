"""Real control/evidence storage for private graph dispatch and correction receipts."""

import copy
import json
import unittest
from dataclasses import asdict
from unittest.mock import patch

from sre_suite.control.client import ControlClient
from sre_suite.coordination.contracts import SuitePlan
from sre_suite.coordination.runtime import SuiteRuntime
from sre_suite.execution.journal import Journal
from sre_suite.execution.wire import digest
from sre_suite.local_pilot.config import write_json
from tests import test_control
from tests.coordination_fixture import plan, rebind
from tests.test_foundation import NOW
from tests.verification_fixture import TokenTransport


class SuiteRuntimeTests(unittest.TestCase):
    def setUp(self):
        f = test_control.ControlTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.f = f
        raw = plan(corrections=True)
        raw["binding"]["scope"] = asdict(f.scope)
        request = {
            "scope": asdict(f.scope),
            "payload": {
                "schema_version": 2,
                "scope": asdict(f.scope),
                "ticket_key": f.f.ticket.key,
                "run_id": "one",
            },
        }
        correction = copy.deepcopy(request)
        correction["payload"]["run_id"] = "two"
        raw["nodes"][0]["request_sha256"] = "sha256:" + digest(request)
        raw["nodes"][0]["corrections"] = ["sha256:" + digest(correction)]
        self.plan = SuitePlan.parse(rebind(raw))
        self.path = f.root / "suite-worker.json"
        plan_path = f.root / "suite-plan.json"
        write_json(plan_path, self.plan.raw)
        paths = {}
        for i, value in enumerate((request, correction)):
            path = f.root / f"input-{i}.json"
            write_json(path, value)
            paths["sha256:" + digest(value)] = str(path)
        config = {
            "plan": str(plan_path),
            "requests": paths,
            "journal": str(f.root / "suite-journal"),
            "role": "controller",
            "github_url": "https://api.github.com",
        }
        write_json(self.path, config)
        agent = SuiteRuntime.__new__(SuiteRuntime)
        agent.path, agent.config, agent.plan, agent.pinned = (
            self.path,
            config,
            self.plan,
            digest(config),
        )
        agent.evidence = f.api
        agent.control = ControlClient(TokenTransport(f.app, f.tokens["worker-one"]))
        agent.journal = Journal(f.root / "suite-journal")

        class Github:
            def call(self, method, path):
                return {"object": {"sha": f.scope.revision}}

        agent.github = Github()
        self.agent = agent
        f.approve(binding=self.plan.binding)
        self.raw = {
            "plan": self.plan.identity,
            "node": "first",
            "request": raw["nodes"][0]["request_sha256"],
            "previous": None,
            "attempt": 0,
        }
        clock = patch("sre_suite.coordination.runtime.datetime")
        patched = clock.start()
        patched.now.return_value = NOW
        self.addCleanup(clock.stop)

    def test_prepare_rechecks_approval_scope_content_and_source(self):
        self.assertEqual(self.agent.prepare(self.raw)["input"]["run_id"], "one")
        self.f.call("revoke", {"binding": self.plan.binding.wire()})
        with self.assertRaises(ValueError):
            self.agent.prepare(self.raw)

    def test_changed_request_or_source_never_dispatches(self):
        request_path = self.agent.config["requests"][self.raw["request"]]
        from pathlib import Path

        changed = json.loads(Path(request_path).read_text())
        changed["payload"]["scope"]["generation"] = 2
        Path(request_path).write_text(json.dumps(changed))
        with self.assertRaises(ValueError):
            self.agent.prepare(self.raw)
        self.agent.github.call = lambda *a: {"object": {"sha": "f" * 40}}
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.agent.guard()

    def test_failed_attempt_receipt_required_and_correction_budget_cannot_expand(self):
        receipt = self.agent.record({**self.raw, "result": {"outcome": "blocked"}})
        self.assertEqual(receipt, self.agent.record({**self.raw, "result": {"outcome": "blocked"}}))
        next_run = {
            **self.raw,
            "request": self.plan.nodes[0]["corrections"][0],
            "attempt": 1,
            "previous": receipt,
        }
        self.assertEqual(self.agent.prepare(next_run)["input"]["run_id"], "two")
        with self.assertRaises(ValueError):
            self.agent.prepare({**next_run, "attempt": 2})
        with self.assertRaises(ValueError):
            self.agent.prepare({**next_run, "previous": None})

    def test_passed_attempt_does_not_authorize_another_implementation(self):
        receipt = self.agent.record(
            {**self.raw, "result": {"outcome": "ready_for_review", "jira_reported": True}}
        )
        with self.assertRaisesRegex(ValueError, "failed attempt"):
            self.agent.prepare(
                {
                    **self.raw,
                    "request": self.plan.nodes[0]["corrections"][0],
                    "attempt": 1,
                    "previous": receipt,
                }
            )

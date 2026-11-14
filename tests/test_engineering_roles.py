"""Independent workers cannot execute another role or accept self-approved artifacts."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from sre_suite.auth import TokenAuthenticator
from sre_suite.execution.evidence_client import EvidenceClient
from sre_suite.execution.roles import ROLES, RolePolicy
from sre_suite.execution.runtime import ExecutionRuntime
from sre_suite.local_pilot.config import write_json
from tests.execution_fixture import Fixture
from tests.test_api import AUDIENCE, credential
from tests.test_foundation import NOW
from tests.verification_fixture import TokenTransport


class EngineeringRolesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.f = Fixture(root)
        self.addCleanup(self.f.close)
        self.path = root / "roles.json"
        self.policy = {
            "schema_version": 2,
            "roles": {role: {"subject": role + "-actor", "queue": "sre-" + role} for role in ROLES},
        }
        write_json(self.path, self.policy)
        self.tokens = {role: "synthetic-" + role + "x" * 43 for role in ROLES}
        self.f.app._auth = TokenAuthenticator(
            AUDIENCE,
            tuple(
                credential(self.f.ticket.scope, token=token, subject=role + "-actor")
                for role, token in self.tokens.items()
            ),
        )
        self.workers = {}
        for role, token in self.tokens.items():
            settings = replace(
                self.f.settings,
                roles_config=str(self.path),
                worker_role=role,
                evidence_token=token,
                jira_writes=role == "controller",
            )
            evidence = EvidenceClient(
                "https://evidence.invalid", token, TokenTransport(self.f.app, token)
            )
            self.workers[role] = ExecutionRuntime(
                settings,
                jira=self.f.jira,
                evidence=evidence,
                tests=self.f.runtime.tests,
                clock=lambda: NOW,
            )

    def test_distinct_workers_complete_and_attest_each_stage(self):
        controller = self.workers["controller"]
        ref = controller.prepare(self.f.spec)
        self.assertEqual(set(ref["role_dispatch"]["queues"]), set(ROLES))
        results = []
        for role in ("implement", "review", "test", "qa"):
            result = self.workers[role].stage({"reference": ref, "role": role, "results": results})
            self.assertEqual(result["evidence"]["producer"], role + "-actor")
            results.append(result)
        self.assertEqual(
            controller.finish({"reference": ref, "results": results})["outcome"], "ready_for_review"
        )

    def test_cross_role_execution_and_dispatch_substitution_fail_before_model(self):
        ref = self.workers["controller"].prepare(self.f.spec)
        worker = self.workers["review"]
        with patch.object(worker.worker, "execute") as model:
            with self.assertRaises(ValueError):
                worker.stage({"reference": ref, "role": "implement", "results": []})
            with self.assertRaises(ValueError):
                worker.prepare(self.f.spec)
            with self.assertRaises(ValueError):
                worker.finish({"reference": ref, "results": []})
            model.assert_not_called()
        changed = {**ref, "role_dispatch": {"policy": "forged", "queues": {}}}
        with self.assertRaises(ValueError):
            self.workers["implement"].stage(
                {"reference": changed, "role": "implement", "results": []}
            )

    def test_wrong_token_cannot_attest_and_role_changes_invalidate_saved_work(self):
        ref = self.workers["controller"].prepare(self.f.spec)
        worker = self.workers["implement"]
        worker.evidence = self.workers["review"].evidence
        with self.assertRaises(ValueError):
            worker.stage({"reference": ref, "role": "implement", "results": []})
        self.policy["roles"]["qa"]["subject"] = "replacement-qa"
        self.path.write_text(__import__("json").dumps(self.policy))
        with self.assertRaises(ValueError):
            self.workers["controller"].finish({"reference": ref, "results": []})

    def test_role_aliases_and_publication_authority_rejected(self):
        self.policy["roles"]["qa"]["subject"] = "implement-actor"
        self.path.write_text(__import__("json").dumps(self.policy))
        with self.assertRaises(ValueError):
            RolePolicy.load(self.path)

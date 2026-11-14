"""Jira publication checks server-side approval before every external write."""

from dataclasses import replace
from unittest.mock import Mock

from sre_suite.jira_planning.client import Planner
from tests import test_jira_planning


class ApprovalPlanningTests(test_jira_planning.PlanningFixture):
    def test_revocation_after_create_prevents_contract_write_and_dependent_ticket(self):
        control = Mock()
        active = True

        def require(binding):
            if not active:
                raise ValueError("revoked")
            return {"active": True}

        control.require_approval.side_effect = require
        self.planner = Planner(
            replace(self.planner.config, authenticated_approval=True),
            self.http,
            self.fixture.evidence,
            self.planner.clock,
            control=control,
        )
        report = self.planner.dry_run(self.plan)
        self.assertEqual(report["authorization"]["scope"], self.raw["scope"])

        def revoke():
            nonlocal active
            active = False

        self.http.after_create = revoke
        with self.assertRaisesRegex(ValueError, "revoked"):
            self.publish()
        self.assertEqual((self.http.creates, self.http.puts), (1, 0))
        self.assertGreater(control.require_approval.call_count, 1)

    def test_server_approval_required_even_with_matching_local_digest(self):
        self.planner.config = replace(self.planner.config, authenticated_approval=True)
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.http.creates, 0)

"""Dispatch cannot enlarge an approved graph or treat absence as a passed agent."""

import copy
import unittest

from sre_suite.coordination.contracts import SuitePlan, passed
from tests.coordination_fixture import plan, rebind


class CoordinationTests(unittest.TestCase):
    def test_full_graph_identity_binds_queue_scope_and_attempt_budget(self):
        raw = plan(corrections=True)
        self.assertEqual(len(SuitePlan.parse(raw).nodes), 1)
        changed = copy.deepcopy(raw)
        changed["nodes"][0]["task_queue"] = "other"
        with self.assertRaises(ValueError):
            SuitePlan.parse(changed)
        self.assertNotEqual(
            SuitePlan.parse(rebind(changed)).identity, SuitePlan.parse(raw).identity
        )
        raw["binding"]["action"] = "execute"
        with self.assertRaises(ValueError):
            SuitePlan.parse(raw)

    def test_cycles_unresolved_dependencies_and_duplicate_attempts_rejected(self):
        for deps in (["first"], ["missing"], ["first", "first"]):
            raw = plan()
            raw["nodes"][0]["depends_on"] = deps
            with self.assertRaises(ValueError):
                SuitePlan.parse(rebind(raw))
        raw = plan(corrections=True)
        raw["nodes"][0]["corrections"] *= 3
        with self.assertRaises(ValueError):
            SuitePlan.parse(rebind(raw))

    def test_receipt_success_is_specific_to_each_agent(self):
        for kind in (
            "engineering",
            "discovery",
            "telemetry",
            "certification",
            "fixtures",
            "delivery",
            "publish",
            "handoff",
            "shipping",
            "analysis",
        ):
            self.assertFalse(passed(kind, {}))
        self.assertFalse(passed("certification", {"verdict": "INCOMPLETE", "evidence": {}}))
        self.assertFalse(
            passed("delivery", {"ready": True, "accepted": False, "outcome": "release_ready"})
        )
        self.assertFalse(passed("telemetry", {"outcome": "collected", "collections": []}))
        self.assertTrue(
            passed(
                "delivery", {"ready": True, "accepted": False, "outcome": "ready_for_verification"}
            )
        )

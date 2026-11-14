"""Service epic and child issue identity, including ambiguous and lost-create responses."""

import copy
from dataclasses import replace
from unittest.mock import Mock

from sre_suite.control.contracts import Binding
from sre_suite.execution.http import RemoteError
from sre_suite.jira_planning.epic import EpicAgent
from tests import test_jira_planning


class EpicTests(test_jira_planning.PlanningFixture):
    def setUp(self):
        super().setUp()
        self.planner.control = Mock()
        self.binding = Binding(
            self.plan.scope,
            0,
            "sha256:" + "a" * 64,
            "sha256:" + self.plan.fingerprint,
            "plan",
            {
                "environment": str(self.plan.scope.environment),
                "url": self.planner.config.url,
                "project": self.plan.project,
                "repository": self.plan.repository,
                "issue_type": "10002",
                "summary": "Service instrumentation",
                "parent_issue": None,
                "epic_name_field": None,
            },
        )
        self.epic = EpicAgent(self.planner, self.binding)
        self.rows = [
            {
                "fieldId": name,
                "required": name in {"project", "issuetype", "summary"},
                "operations": ["set"],
            }
            for name in ("project", "issuetype", "summary", "labels")
        ]
        self.created = None
        self.posts = 0
        self.lost = False
        self.visible = True
        self.planner.pages = (
            lambda path, field: [{"id": "10002", "hierarchyLevel": 1, "subtask": False}]
            if field == "issueTypes"
            else self.rows
        )
        self.planner.http.call = self.call

    def call(self, method, path, payload=None):
        if path == "/rest/api/3/issuetype/10002":
            return {"id": "10002", "hierarchyLevel": 1}
        if method == "POST":
            self.posts += 1
            self.created = {
                "key": "DEMO-500",
                "fields": copy.deepcopy(payload["fields"]),
                "properties": payload["properties"],
            }
            if self.lost:
                self.lost = False
                raise RemoteError(0)
            return {"key": "DEMO-500"}
        if "/search/jql?" in path:
            return {
                "isLast": True,
                "issues": [{"key": "DEMO-500"}] if self.created and self.visible else [],
            }
        if "/properties/" in path:
            return {"value": self.created["properties"][0]["value"]}
        if "/issue/DEMO-500?" in path:
            return copy.deepcopy(self.created)
        raise AssertionError(path)

    def test_epic_has_no_executable_ticket_and_lost_ack_is_reconciled(self):
        self.lost = True
        with self.assertRaises(RemoteError):
            self.epic.publish(self.journal)
        receipt = self.epic.publish(self.journal)
        self.assertEqual(receipt["key"], "DEMO-500")
        self.assertEqual(self.epic.publish(self.journal), receipt)
        self.assertNotIn(self.planner.config.contract_field, self.created["fields"])
        self.assertEqual(self.posts, 1)

    def test_uncertain_invisible_epic_is_never_recreated(self):
        self.lost = True
        with self.assertRaises(RemoteError):
            self.epic.publish(self.journal)
        self.visible = False
        with self.assertRaisesRegex(ValueError, "uncertain epic"):
            self.epic.publish(self.journal)
        self.assertEqual(self.posts, 1)

    def test_missing_fields_or_changed_remote_epic_stop_publication(self):
        self.rows.append({"fieldId": "customfield_999", "required": True})
        with self.assertRaises(ValueError):
            self.epic.publish(self.journal)
        self.assertEqual(self.posts, 0)
        self.rows.pop()
        self.epic.publish(self.journal)
        self.created["fields"]["summary"] = "Someone else's epic"
        with self.assertRaises(ValueError):
            self.epic.publish(self.journal)


class ChildParentTests(test_jira_planning.PlanningFixture):
    def test_parent_epic_is_in_approval_creation_and_subsequent_verification(self):
        original = self.http.call
        self.http.rows.insert(0, {"fieldId": "parent", "required": False, "operations": ["set"]})

        def call(method, path, payload=None):
            if path.startswith("/rest/api/3/issue/DEMO-50?"):
                return {
                    "key": "DEMO-50",
                    "fields": {"project": {"key": "DEMO"}, "issuetype": {"hierarchyLevel": 1}},
                }
            return original(method, path, payload)

        self.http.call = call
        before = self.planner.approval(self.plan)
        self.planner.config = replace(self.planner.config, parent_key="DEMO-50")
        self.assertNotEqual(before, self.planner.approval(self.plan))
        self.publish()
        self.assertEqual(self.http.issues["DEMO-101"]["fields"]["parent"], {"key": "DEMO-50"})
        self.http.issues["DEMO-101"]["fields"]["parent"] = {"key": "DEMO-51"}
        with self.assertRaises(ValueError):
            self.publish()

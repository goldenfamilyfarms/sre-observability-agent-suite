"""Real scoped evidence API with synthetic Jira requests and lost responses."""

import copy
import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from sre_suite.api_wire import decode
from sre_suite.execution.http import RemoteError
from sre_suite.execution.jira import JiraClient
from sre_suite.execution.jira_text import document, plain
from sre_suite.execution.wire import canonical, ticket_envelope, ticket_json
from sre_suite.jira_planning.__main__ import main
from sre_suite.jira_planning.client import MARKER, TEXTAREA, Config, Planner
from sre_suite.jira_planning.contracts import Plan
from tests.execution_fixture import Fixture
from tests.test_foundation import NOW


class PlanningTransport:
    def __init__(self):
        self.calls = []
        self.issues = {}
        self.creates = self.puts = 0
        self.lose_create = self.lose_edit = self.hide_search = self.reject_create = False
        self.after_create = lambda: None
        self.before_edit = lambda: None
        self.permissions = True
        self.subtask = False
        self.page_size = 2
        self.rows = [
            {
                "fieldId": name,
                "required": name in {"project", "issuetype", "summary"},
                "operations": ["set"],
                "schema": {"custom": TEXTAREA} if name.startswith("custom") else {},
            }
            for name in ("project", "issuetype", "summary", "labels", "customfield_12345")
        ]

    def call(self, method, path, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if path.startswith("/rest/api/3/project/"):
            return {"key": "DEMO"}
        if path.startswith("/rest/api/3/mypermissions?"):
            return {
                "permissions": {
                    name: {"havePermission": self.permissions}
                    for name in ("BROWSE_PROJECTS", "CREATE_ISSUES", "EDIT_ISSUES")
                }
            }
        if "/createmeta/" in path:
            query = parse_qs(urlsplit(path).query)
            start = int(query["startAt"][0])
            name = "issueTypes" if "/issuetypes?" in path else "fields"
            items = (
                [{"id": "10001", "subtask": self.subtask}] if name == "issueTypes" else self.rows
            )
            return {
                "startAt": start,
                "total": len(items),
                name: copy.deepcopy(items[start : start + self.page_size]),
            }
        if "/search/jql?" in path:
            label = parse_qs(urlsplit(path).query)["jql"][0].split('"')[-2]
            keys = (
                []
                if self.hide_search
                else [k for k, v in self.issues.items() if label in v["fields"]["labels"]]
            )
            return {"issues": [{"key": k} for k in keys], "isLast": True}
        if method == "POST" and path == "/rest/api/3/issue":
            self.creates += 1
            if self.reject_create:
                raise RemoteError(400)
            key = "DEMO-" + str(100 + self.creates)
            fields = copy.deepcopy(payload["fields"])
            fields["status"] = {"name": "Open", "statusCategory": {"key": "new"}}
            self.issues[key] = {
                "key": key,
                "fields": fields,
                "marker": payload["properties"][0]["value"],
            }
            self.after_create()
            if self.lose_create:
                self.lose_create = False
                raise RemoteError(0)
            return {"key": key, "id": str(self.creates)}
        key = path.split("/issue/")[1].split("/")[0].split("?")[0]
        issue = self.issues[key]
        if path.endswith("/properties/" + MARKER):
            return {"value": copy.deepcopy(issue["marker"])}
        if path.endswith("/editmeta"):
            return {"fields": {"customfield_12345": copy.deepcopy(self.rows[-1])}}
        if method == "PUT":
            self.puts += 1
            self.before_edit()
            issue["fields"].update(copy.deepcopy(payload["fields"]))
            if self.lose_edit:
                self.lose_edit = False
                raise RemoteError(0)
            return None
        return copy.deepcopy(issue)


class PlanningTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.fixture = Fixture(self.root)
        self.addCleanup(self.fixture.close)
        self.env = {
            **self.fixture.env,
            "SRE_JIRA_ISSUE_TYPE_ID": "10001",
            "SRE_JIRA_PLANNING_WRITES_ENABLED": "true",
        }
        self.http = PlanningTransport()
        self.planner = Planner(Config.load(self.env), self.http, self.fixture.evidence, lambda: NOW)
        requirements = ticket_json(self.fixture.ticket)
        for name in ("key", "scope", "repository", "dependencies"):
            del requirements[name]
        self.raw = {
            "schema_version": 2,
            "id": "pilot-one",
            "project": "DEMO",
            "repository": self.fixture.ticket.repository,
            "scope": asdict(self.fixture.ticket.scope),
            "nodes": [
                {
                    "id": "base",
                    "summary": "Prepare synthetic base",
                    "depends_on": [],
                    "requirements": requirements,
                },
                {
                    "id": "followup",
                    "summary": "Implement synthetic followup",
                    "depends_on": ["base"],
                    "requirements": copy.deepcopy(requirements),
                },
            ],
        }
        self.plan = Plan.parse(self.raw)
        self.journal = self.root / "plan-journal"
        self.journal.mkdir(mode=0o700)

    def publish(self, plan=None):
        plan = plan or self.plan
        return self.planner.publish(plan, self.planner.approval(plan), self.journal)

    def supersede(self):
        self.fixture.registry.start(
            replace(self.fixture.preview, scope=replace(self.fixture.ticket.scope, generation=2)),
            1,
            NOW,
        )

    def test_discovery_paginates_and_dry_run_only_reads(self):
        report = self.planner.dry_run(self.plan)
        self.assertEqual(report["order"], ["base", "followup"])
        self.assertEqual(report["plan"]["scope"]["generation"], 1)
        self.assertTrue(all(method == "GET" for method, _, _ in self.http.calls))
        self.assertEqual(list(self.journal.iterdir()), [])
        self.assertEqual(self.http.creates, 0)

    def test_dag_rejects_cycles_unresolved_duplicates_and_identity_override(self):
        changes = [
            lambda p: p["nodes"][0].update(depends_on=["followup"]),
            lambda p: p["nodes"][1].update(depends_on=["missing"]),
            lambda p: p["nodes"][1].update(id="base"),
            lambda p: p["nodes"][1].update(depends_on=["base", "base"]),
            lambda p: p["nodes"][0]["requirements"].update(key="DEMO-999"),
            lambda p: p["scope"].update(environment="production"),
            lambda p: p["scope"].update(generation=True),
        ]
        for mutate in changes:
            raw = copy.deepcopy(self.raw)
            mutate(raw)
            with self.subTest(raw=raw["nodes"][0]["id"]), self.assertRaises(ValueError):
                Plan.parse(raw)

    def test_success_maps_dependencies_into_readable_runtime_contracts(self):
        result = self.publish()
        self.assertEqual(result["issues"], {"base": "DEMO-101", "followup": "DEMO-102"})
        for key, issue in self.http.issues.items():
            value = issue["fields"]["customfield_12345"]
            ticket = ticket_envelope(decode(plain(value).encode()))
            self.assertEqual(ticket.key, key)
            self.assertEqual(ticket.scope, self.plan.scope)
        followup = ticket_envelope(
            decode(plain(self.http.issues["DEMO-102"]["fields"]["customfield_12345"]).encode())
        )
        self.assertEqual(followup.dependencies, ("DEMO-101",))
        # Existing runtime can ingest the plain ADF field; no planning-specific ticket type.
        ticket = JiraClient(self.fixture.settings, self.http).get_ticket("DEMO-101")
        self.assertEqual(ticket.key, "DEMO-101")
        self.assertEqual(self.publish(), result)
        self.assertEqual((self.http.creates, self.http.puts), (2, 2))
        self.assertFalse(result["execution_started"])

    def test_lost_create_response_reconciles_without_second_post(self):
        self.http.lose_create = True
        with self.assertRaises(RemoteError):
            self.publish()
        self.assertEqual(self.http.creates, 1)
        self.publish()
        self.assertEqual(self.http.creates, 2)

    def test_uncertain_invisible_create_never_repeats_even_after_restart(self):
        self.http.lose_create = True
        with self.assertRaises(RemoteError):
            self.publish()
        self.http.hide_search = True
        self.planner = Planner(Config.load(self.env), self.http, self.fixture.evidence, lambda: NOW)
        with self.assertRaisesRegex(ValueError, "uncertain create"):
            self.publish()
        self.assertEqual(self.http.creates, 1)
        self.http.hide_search = False
        self.publish()
        self.assertEqual(self.http.creates, 2)

    def test_explicit_create_error_is_also_not_blindly_retried(self):
        self.http.reject_create = True
        with self.assertRaises(RemoteError):
            self.publish()
        self.http.reject_create = False
        with self.assertRaisesRegex(ValueError, "uncertain create"):
            self.publish()
        self.assertEqual(self.http.creates, 1)

    def test_lost_contract_write_response_reconciles_without_rewrite(self):
        self.http.lose_edit = True
        with self.assertRaises(RemoteError):
            self.publish()
        self.publish()
        self.assertEqual((self.http.creates, self.http.puts), (2, 2))

    def test_concurrent_publication_serializes_and_reuses_issues(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.publish(), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual((self.http.creates, self.http.puts), (2, 2))

    def test_changed_plan_same_id_and_changed_target_approval_are_rejected(self):
        self.publish()
        raw = copy.deepcopy(self.raw)
        raw["nodes"][0]["summary"] = "changed"
        changed = Plan.parse(raw)
        with self.assertRaises(ValueError):
            self.planner.publish(changed, self.planner.approval(self.plan), self.journal)
        with self.assertRaisesRegex(ValueError, "journal conflict"):
            self.publish(changed)
        self.planner.config = replace(self.planner.config, issue_type="10002")
        with self.assertRaises(ValueError):
            self.planner.publish(self.plan, self.planner.approval(changed), self.journal)
        self.assertEqual(self.http.creates, 2)

    def test_write_flag_defaults_off_and_wrong_digest_never_writes(self):
        config = Config.load(
            {k: v for k, v in self.env.items() if k != "SRE_JIRA_PLANNING_WRITES_ENABLED"}
        )
        self.assertFalse(config.enabled)
        self.planner.config = config
        with self.assertRaises(ValueError):
            self.publish()
        self.planner.config = replace(config, enabled=True)
        with self.assertRaises(ValueError):
            self.planner.publish(self.plan, "f" * 64, self.journal)
        self.assertEqual(self.http.calls, [])

    def test_project_repository_scope_and_evidence_fail_before_create(self):
        for change in ({"project": "OTHER"}, {"repository": "other/repo"}):
            raw = copy.deepcopy(self.raw)
            raw.update(change)
            with self.assertRaises(ValueError):
                self.publish(Plan.parse(raw))
        self.planner.clock = lambda: NOW + timedelta(days=1)
        with self.assertRaises(ValueError):
            self.publish()
        self.planner.clock = lambda: NOW
        self.supersede()
        with self.assertRaises(RemoteError):
            self.publish()
        self.assertEqual(self.http.creates, 0)

    def test_generation_change_after_create_retains_issue_and_stops_contract_write(self):
        self.http.after_create = self.supersede
        with self.assertRaises(RemoteError):
            self.publish()
        self.assertEqual((self.http.creates, self.http.puts), (1, 0))
        with self.assertRaises(RemoteError):
            self.publish()
        self.assertEqual(self.http.creates, 1)

    def test_generation_change_during_edit_stops_remaining_issues(self):
        self.http.before_edit = self.supersede
        with self.assertRaises(RemoteError):
            self.publish()
        self.assertEqual((self.http.creates, self.http.puts), (1, 1))

    def test_foreign_marker_duplicate_matches_and_human_edits_block_reconciliation(self):
        self.publish()
        original = copy.deepcopy(self.http.issues)
        mutations = [
            lambda: self.http.issues["DEMO-101"]["marker"].update(plan_id="other"),
            lambda: self.http.issues["DEMO-101"]["fields"].update(summary="human edit"),
            lambda: self.http.issues["DEMO-101"]["fields"].update(customfield_12345=None),
            lambda: self.http.issues["DEMO-101"]["fields"].update(
                customfield_12345=document("different")
            ),
            lambda: self.http.issues["DEMO-101"]["fields"]["status"].update(
                statusCategory={"key": "done"}
            ),
        ]
        for mutate in mutations:
            self.http.issues = copy.deepcopy(original)
            mutate()
            with self.assertRaises(ValueError):
                self.publish()
        self.http.issues = copy.deepcopy(original)
        self.http.issues["DEMO-999"] = copy.deepcopy(original["DEMO-101"])
        with self.assertRaises(ValueError):
            self.planner.search(self.plan, self.plan.nodes[0])
        self.assertEqual((self.http.creates, self.http.puts), (2, 2))

    def test_discovery_rejects_permissions_subtasks_required_fields_and_wrong_field_type(self):
        for case in ("permissions", "subtask", "required", "field"):
            transport = PlanningTransport()
            if case == "permissions":
                transport.permissions = False
            elif case == "subtask":
                transport.subtask = True
            elif case == "required":
                transport.rows.append({"fieldId": "unknown", "required": True})
            else:
                transport.rows[-1]["schema"]["custom"] = "textfield"
            self.planner.http = transport
            with self.subTest(case=case), self.assertRaises(ValueError):
                self.publish()
            self.assertEqual(transport.creates, 0)

    def test_journal_tampering_and_unsafe_paths_fail_closed(self):
        self.publish()
        state = next(self.journal.glob("*.json"))
        data = json.loads(state.read_text())
        data["state"]["approval"] = "0" * 64
        state.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            self.publish()
        alias = self.root / "alias"
        alias.symlink_to(self.journal)
        with self.assertRaises(ValueError):
            self.planner.publish(self.plan, self.planner.approval(self.plan), alias)
        self.journal.chmod(0o755)
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.http.creates, 2)

    def test_plain_adf_roundtrip_and_unsupported_formatting_rejected(self):
        value = canonical({"schema_version": 2, "ticket": ticket_json(self.fixture.ticket)})
        self.assertEqual(plain(document(value)), value)
        self.assertEqual(plain(value), value)
        for mutate in (
            lambda d: d.update(version=True),
            lambda d: d["content"][0]["content"][0].update(marks=[{"type": "link"}]),
            lambda d: d["content"][0].update(type="codeBlock"),
        ):
            data = document(value)
            mutate(data)
            with self.assertRaises(ValueError):
                plain(data)

    def test_cli_errors_redact_private_configuration(self):
        stream = io.StringIO()
        with (
            patch("sys.argv", ["planning", "discover"]),
            patch.dict("os.environ", {}, clear=True),
            redirect_stderr(stream),
            self.assertRaises(SystemExit),
        ):
            main()
        self.assertNotIn("TOKEN", stream.getvalue())
        self.assertNotIn(str(self.root), stream.getvalue())

    def test_crash_after_durable_intent_does_not_retry_create(self):
        from sre_suite.jira_planning.client import save

        def crash(path, state):
            save(path, state)
            raise RuntimeError("synthetic crash before POST")

        with patch("sre_suite.jira_planning.client.save", crash), self.assertRaises(RuntimeError):
            self.publish()
        self.assertEqual(self.http.creates, 0)
        with self.assertRaisesRegex(ValueError, "uncertain create"):
            self.publish()
        self.assertEqual(self.http.creates, 0)

    def test_incomplete_pagination_never_allows_partial_discovery(self):
        self.http.page_size = 0
        with self.assertRaisesRegex(ValueError, "incomplete metadata"):
            self.publish()
        self.assertEqual(self.http.creates, 0)

    def test_final_fence_detects_earlier_contract_change(self):
        def change():
            if self.http.puts == 2:
                self.http.issues["DEMO-101"]["fields"]["customfield_12345"] = document("changed")

        self.http.before_edit = change
        with self.assertRaisesRegex(ValueError, "final verification"):
            self.publish()
        self.assertEqual((self.http.creates, self.http.puts), (2, 2))

    def test_cli_dry_run_writes_private_review_without_jira_writes(self):
        from contextlib import redirect_stdout

        source, output = self.root / "plan.json", self.root / "review"
        source.write_text(canonical(self.plan.json()))
        with (
            patch("sys.argv", ["planning", "dry-run", str(source), "--output", str(output)]),
            patch("sre_suite.jira_planning.__main__.Planner.from_env", return_value=self.planner),
            redirect_stdout(io.StringIO()),
        ):
            main()
        report = json.loads((output / "review.json").read_text())
        self.assertEqual(report["approval_digest"], self.planner.approval(self.plan))
        self.assertEqual((output / "review.json").stat().st_mode & 0o077, 0)
        self.assertEqual(self.http.creates, 0)

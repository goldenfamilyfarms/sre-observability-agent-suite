import copy
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from sre_suite.execution.config import Settings
from sre_suite.execution.http import RemoteError
from tests.execution_fixture import Fixture
from tests.publication_fixture import RULES, publication_runtime
from tests.test_foundation import NOW


class PublicationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fixture = Fixture(Path(directory.name))
        self.addCleanup(self.fixture.close)
        self.runtime, self.github, self.jira, self.events = publication_runtime(self.fixture)

    def ready(self):
        reference = self.runtime.prepare(self.fixture.spec)
        results = []
        for role in ("implement", "review", "test", "qa"):
            results.append(
                self.runtime.stage({"reference": reference, "role": role, "results": results})
            )
        return {"reference": reference, "results": results}

    def supersede(self):
        self.fixture.registry.start(
            replace(self.fixture.preview, scope=replace(self.fixture.ticket.scope, generation=2)),
            1,
            NOW,
        )

    def test_complete_flow_creates_exact_git_candidate_then_draft_pr_then_review_transition(self):
        args = self.ready()
        report = self.runtime.finish(args)
        self.assertEqual(report["outcome"], "ready_for_review")
        self.assertTrue(report["jira_transitioned"])
        self.assertTrue(report["pull_request"]["draft"])
        self.assertEqual(self.jira.transitions, ["10", "20"])
        self.assertLess(self.events.index("github:/pulls"), self.events.index("jira:20"))
        head = report["pull_request"]["head"]
        self.assertEqual(
            self.github.git("show", head + ":app.py"), b"def answer():\n    return 2\n"
        )
        self.assertEqual(
            self.github.git("rev-parse", head + "^").decode().strip(),
            self.fixture.ticket.scope.revision,
        )
        self.assertEqual(
            self.github.git("show", head + ":test_app.py"),
            (self.fixture.repo / "test_app.py").read_bytes(),
        )
        self.assertIn("return 1", (self.fixture.repo / "app.py").read_text())
        self.assertEqual(self.runtime.finish(args), report)
        self.assertEqual(len(self.github.pulls), 1)
        self.assertEqual(self.jira.transitions, ["10", "20"])
        self.assertEqual(self.jira.puts, 1)
        self.assertTrue(all(method == "POST" for method, _, _ in self.github.writes))
        self.assertEqual(
            {path for _, path, _ in self.github.writes},
            {"/git/trees", "/git/commits", "/git/refs", "/pulls"},
        )
        self.assertNotIn(self.fixture.ticket.objective, self.github.pulls[0]["body"])

    def test_lost_git_object_ref_and_pr_responses_reconcile_without_duplicate_prs(self):
        args = self.ready()
        for endpoint in ("/git/trees", "/git/commits", "/git/refs", "/pulls"):
            self.github.lose_once = endpoint
            with self.assertRaises(RemoteError):
                self.runtime.finish(args)
        result = self.runtime.finish(args)
        self.assertEqual(result["pull_request"]["number"], 1)
        self.assertEqual(len(self.github.pulls), 1)
        self.assertEqual(sum(path == "/git/refs" for _, path, _ in self.github.writes), 1)
        self.assertEqual(sum(path == "/pulls" for _, path, _ in self.github.writes), 1)

    def test_lost_transition_responses_reconcile_status_and_marker(self):
        self.jira.lose_transition = True
        with self.assertRaises(RemoteError):
            self.runtime.prepare(self.fixture.spec)
        args = self.ready()
        self.assertEqual(self.jira.transitions, ["10"])
        self.jira.lose_transition = True
        with self.assertRaises(RemoteError):
            self.runtime.finish(args)
        result = self.runtime.finish(args)
        self.assertTrue(result["jira_transitioned"])
        self.assertEqual(self.jira.transitions, ["10", "20"])
        self.assertEqual(len(self.github.pulls), 1)

    def test_lost_jira_report_response_reconciles_pr_transition_and_property(self):
        args = self.ready()
        self.jira.lose_response = True
        with self.assertRaises(RemoteError):
            self.runtime.finish(args)
        self.runtime.finish(args)
        self.assertEqual(len(self.github.pulls), 1)
        self.assertEqual(self.jira.transitions, ["10", "20"])
        self.assertEqual(self.jira.puts, 1)

    def test_failed_review_blocks_jira_without_publishing_code(self):
        reference = self.runtime.prepare(self.fixture.spec)
        results = [self.runtime.stage({"reference": reference, "role": "implement", "results": []})]
        with patch.object(
            self.runtime.worker,
            "execute",
            return_value={"verdict": "blocked", "summary": "reject", "edits": []},
        ):
            results.append(
                self.runtime.stage({"reference": reference, "role": "review", "results": results})
            )
        result = self.runtime.finish({"reference": reference, "results": results})
        self.assertEqual(result["outcome"], "blocked")
        self.assertIsNone(result["pull_request"])
        self.assertEqual(self.github.writes, [])
        self.assertEqual(self.jira.transitions, ["10", "30"])

    def test_generation_change_before_publication_blocks_all_new_external_writes(self):
        args = self.ready()
        self.supersede()
        with self.assertRaises(RemoteError):
            self.runtime.finish(args)
        self.assertEqual(self.github.writes, [])
        self.assertEqual(self.jira.transitions, ["10"])
        self.assertEqual(self.jira.puts, 0)

    def test_generation_change_between_git_object_writes_stops_publication(self):
        args = self.ready()
        self.github.after_write = lambda path: self.supersede() if path == "/git/trees" else None
        with self.assertRaises(RemoteError):
            self.runtime.finish(args)
        self.assertEqual([p for _, p, _ in self.github.writes], ["/git/trees"])
        self.assertEqual(self.jira.transitions, ["10"])

    def test_generation_change_after_pr_creation_prevents_jira_review_transition(self):
        args = self.ready()
        self.github.after_write = lambda path: self.supersede() if path == "/pulls" else None
        with self.assertRaises(RemoteError):
            self.runtime.finish(args)
        self.assertEqual(len(self.github.pulls), 1)
        self.assertEqual(self.jira.transitions, ["10"])
        self.assertEqual(self.jira.puts, 0)
        with self.assertRaises(RemoteError):
            self.runtime.finish(args)
        self.assertEqual(len(self.github.pulls), 1)

    def test_generation_change_after_jira_transition_prevents_success_report(self):
        args = self.ready()
        self.jira.before_transition = self.supersede
        with self.assertRaises(RemoteError):
            self.runtime.finish(args)
        self.assertEqual(self.jira.puts, 0)
        self.assertEqual(self.jira.transitions, ["10", "20"])

    def test_different_preview_id_and_generation_remain_distinct(self):
        from sre_suite.execution.runtime import execution_key

        for field, value in (("environment_id", "another-preview"), ("generation", 2)):
            spec = copy.deepcopy(self.fixture.spec)
            spec["scope"][field] = value
            self.assertNotEqual(execution_key(spec), execution_key(self.fixture.spec))
            with self.assertRaises(ValueError):
                self.runtime.prepare(spec)
        self.assertEqual(self.jira.transitions, [])
        self.assertEqual(self.github.writes, [])

    def test_base_movement_requires_new_tested_execution(self):
        args = self.ready()
        self.github.refs["main"] = "f" * 40
        with self.assertRaises(ValueError):
            self.runtime.finish(args)
        self.assertEqual(self.github.writes, [])
        self.assertEqual(self.jira.transitions, ["10"])

    def test_changed_branch_is_never_overwritten(self):
        args = self.ready()
        branch = "sre/run-" + args["reference"]["key"][4:]
        self.github.refs[branch] = "f" * 40
        with self.assertRaises(ValueError):
            self.runtime.finish(args)
        self.assertEqual(self.github.refs[branch], "f" * 40)
        self.assertEqual(self.github.pulls, [])
        self.assertNotIn("/git/refs", [p for _, p, _ in self.github.writes])

    def test_closed_retargeted_or_changed_pr_is_not_reopened_or_duplicated(self):
        args = self.ready()
        self.runtime.finish(args)
        original = copy.deepcopy(self.github.pulls[0])
        for change in (
            {"state": "closed"},
            {"draft": False},
            {"body": "edited"},
            {"base": {"ref": "other"}},
            {"merged_at": "synthetic-time"},
        ):
            self.github.pulls[0] = {**copy.deepcopy(original), **change}
            with self.assertRaises(ValueError):
                self.runtime.finish(args)
        self.assertEqual(len(self.github.pulls), 1)
        self.assertEqual(sum(p == "/pulls" for _, p, _ in self.github.writes), 1)

    def test_terminal_destination_or_required_fields_block_transition(self):
        for attr, value in (("target_category", "done"), ("required_fields", True)):
            with self.subTest(attr=attr):
                old = getattr(self.jira, attr)
                setattr(self.jira, attr, value)
                with self.assertRaises(ValueError):
                    self.runtime.prepare(self.fixture.spec)
                setattr(self.jira, attr, old)
        self.assertEqual(self.jira.transitions, [])

    def test_unowned_target_status_is_not_claimed_or_transitioned(self):
        self.jira.status = "200"
        with self.assertRaises(ValueError):
            self.runtime.prepare(self.fixture.spec)
        self.assertEqual(self.jira.transitions, [])

    def test_changed_status_after_acknowledged_transition_does_not_regress(self):
        args = self.ready()
        self.runtime.finish(args)
        self.jira.status = "400"
        with self.assertRaises(ValueError):
            self.runtime.finish(args)
        self.assertEqual(self.jira.transitions, ["10", "20"])

    def test_concurrent_duplicate_finishes_publish_once(self):
        args = self.ready()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.runtime.finish(args), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.github.pulls), 1)
        self.assertEqual(self.jira.transitions, ["10", "20"])
        self.assertEqual(self.jira.puts, 1)

    def test_publication_configuration_requires_explicit_opt_ins_and_safe_mapping(self):
        settings = Settings.from_env(self.fixture.env)
        self.assertFalse(settings.github_prs)
        self.assertEqual(settings.jira_transitions, ())
        self.assertNotIn("synthetic-github-token", repr(self.runtime.settings))
        for values in (
            {"SRE_GITHUB_PRS_ENABLED": "true"},
            {"SRE_JIRA_TRANSITIONS_ENABLED": "true"},
            {"SRE_GITHUB_BASE_BRANCH": "../main"},
            {"SRE_GITHUB_BASE_BRANCH": "sre/run-existing"},
        ):
            with self.assertRaises(ValueError):
                Settings.from_env({**self.fixture.env, **values})
        env = {
            **self.fixture.env,
            "SRE_GITHUB_PRS_ENABLED": "true",
            "SRE_GITHUB_TOKEN": "fixture",
            "SRE_JIRA_TRANSITIONS_ENABLED": "true",
            "SRE_JIRA_TRANSITIONS_JSON": json.dumps(RULES),
        }
        for rules in (
            {},
            {**RULES, "done": RULES["blocked"]},
            {**RULES, "in_review": {**RULES["in_review"], "from_status_ids": ["300"]}},
        ):
            with self.assertRaises(ValueError):
                Settings.from_env({**env, "SRE_JIRA_TRANSITIONS_JSON": json.dumps(rules)})

    def test_remote_symlinks_and_unapproved_edits_are_rejected(self):
        args = self.ready()
        self.github.entries_override = [{"path": "app.py", "type": "blob", "mode": "120000"}]
        with self.assertRaises(ValueError):
            self.runtime.finish(args)
        self.assertEqual(self.github.writes, [])

    def test_executable_file_mode_is_preserved_in_remote_candidate(self):
        args = self.ready()
        self.github.entries_override = [{"path": "app.py", "type": "blob", "mode": "100755"}]
        report = self.runtime.finish(args)
        mode = (
            self.github.git("ls-tree", report["pull_request"]["head"], "app.py").decode().split()[0]
        )
        self.assertEqual(mode, "100755")

    def test_changed_pr_head_during_jira_validation_blocks_review_transition(self):
        args = self.ready()
        original = self.jira.call

        def change_head(method, path, payload=None):
            result = original(method, path, payload)
            if method == "GET" and "/transitions?" in path:
                branch = self.github.pulls[0]["head"]["ref"]
                self.github.refs[branch] = "f" * 40
            return result

        with patch.object(self.jira, "call", side_effect=change_head):
            with self.assertRaises(ValueError):
                self.runtime.finish(args)
        self.assertEqual(self.jira.transitions, ["10"])
        self.assertEqual(self.jira.puts, 0)

    def test_transition_marker_conflict_is_not_overwritten(self):
        args = self.ready()
        self.runtime.finish(args)
        key = next(k for k, v in self.jira.properties.items() if v.get("milestone") == "in_review")
        self.jira.properties[key] = {"execution_id": "someone-else"}
        with self.assertRaises(ValueError):
            self.runtime.finish(args)
        self.assertEqual(self.jira.transitions, ["10", "20"])

    def test_publication_token_is_not_forwarded_to_claude_process(self):
        with patch.dict(os.environ, {"SRE_GITHUB_TOKEN": "private-fixture-token"}):
            reference = self.runtime.prepare(self.fixture.spec)
            result = self.runtime.stage(
                {"reference": reference, "role": "implement", "results": []}
            )
        self.assertEqual(result["verdict"], "passed")

    def test_expired_preview_blocks_pr_and_status_writes(self):
        args = self.ready()
        self.fixture.app._clock = lambda: NOW + timedelta(hours=2)
        with self.assertRaises(RemoteError):
            self.runtime.finish(args)
        self.assertEqual(self.github.writes, [])
        self.assertEqual(self.jira.transitions, ["10"])

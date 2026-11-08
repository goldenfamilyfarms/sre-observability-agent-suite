import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from sre_suite.execution.claude import ClaudeCodeWorker, validate_output
from sre_suite.execution.config import Settings
from sre_suite.execution.http import JsonHttp, NoRedirect, RemoteError
from sre_suite.execution.jira import JiraClient
from sre_suite.execution.journal import Journal
from sre_suite.execution.process import ExecutionBlocked, minimal_environment, run_process
from sre_suite.execution.runtime import ExecutionRuntime, execution_key
from sre_suite.execution.wire import digest, ticket_envelope, ticket_json
from sre_suite.execution.workspace import make_patch
from tests.execution_fixture import Fixture
from tests.test_foundation import NOW


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fixture = Fixture(Path(temporary.name))
        self.addCleanup(self.fixture.close)
        self.runtime = self.fixture.runtime

    def test_real_cli_fixture_and_all_runtime_stages_reach_review_ready(self):
        reference = self.runtime.prepare(self.fixture.spec)
        results = []
        for role in ("implement", "review", "test", "qa"):
            results.append(
                self.runtime.stage({"reference": reference, "role": role, "results": results})
            )
        report = self.runtime.finish({"reference": reference, "results": results})
        self.assertEqual(report["outcome"], "ready_for_review")
        self.assertTrue(report["jira_reported"])
        self.assertEqual(self.fixture.jira_http.puts, 1)
        self.runtime.finish({"reference": reference, "results": results})
        self.assertEqual(self.fixture.jira_http.puts, 1)
        artifact = self.runtime.journal.read(
            reference["key"] + ":implement", results[0]["artifact"]
        )
        self.assertIn("+    return 2", artifact["patch"])
        self.assertIn("return 1", (self.fixture.repo / "app.py").read_text())
        subprocess.run(
            ["git", "apply", "--check", "-"],
            cwd=self.fixture.repo,
            input=artifact["patch"].encode(),
            check=True,
            capture_output=True,
        )

    def test_retry_reuses_persisted_cli_result_without_running_model_twice(self):
        reference = self.runtime.prepare(self.fixture.spec)
        args = {"reference": reference, "role": "implement", "results": []}
        with patch.object(
            self.runtime.worker, "execute", wraps=self.runtime.worker.execute
        ) as worker:
            first = self.runtime.stage(args)
            second = self.runtime.stage(args)
            self.assertEqual(first, second)
            self.assertEqual(worker.call_count, 1)
        restarted = ExecutionRuntime(
            self.fixture.settings,
            jira=self.fixture.jira,
            evidence=self.fixture.evidence,
            tests=self.runtime.tests,
            clock=lambda: NOW,
        )
        with patch.object(restarted.worker, "execute", side_effect=AssertionError("must not run")):
            self.assertEqual(restarted.stage(args), first)

    def test_lost_jira_write_response_reconciles_remote_property(self):
        reference = self.runtime.prepare(self.fixture.spec)
        self.fixture.jira_http.lose_response = True
        args = {"reference": reference, "results": []}
        with self.assertRaises(RemoteError):
            self.runtime.finish(args)
        self.assertTrue(self.runtime.finish(args)["jira_reported"])
        self.assertEqual(self.fixture.jira_http.puts, 1)

    def test_jira_writes_and_claude_execution_are_explicit_opt_ins(self):
        env = dict(self.fixture.env)
        del env["SRE_JIRA_WRITES_ENABLED"]
        del env["SRE_CLAUDE_ENABLED"]
        settings = Settings.from_env(env)
        self.assertFalse(settings.jira_writes)
        self.assertFalse(JiraClient(settings, self.fixture.jira_http).publish("DEMO-1", "run", {}))
        with self.assertRaises(ExecutionBlocked):
            ClaudeCodeWorker(settings).execute("implement", {})
        self.assertEqual(self.fixture.jira_http.puts, 0)

    def test_jira_ticket_and_repository_binding_and_dependencies(self):
        self.fixture.jira_http.ticket = replace(self.fixture.ticket, repository="other/repo")
        with self.assertRaises(ValueError):
            self.fixture.jira.get_ticket("DEMO-1")
        with self.assertRaises(ValueError):
            self.fixture.jira.get_ticket("OTHER-1")
        self.fixture.jira_http.ticket = replace(self.fixture.ticket, dependencies=("DEMO-2",))
        with self.assertRaises(ValueError):
            self.fixture.jira.get_ticket("DEMO-1")

    def test_changed_jira_contract_blocks_cached_execution(self):
        reference = self.runtime.prepare(self.fixture.spec)
        self.fixture.jira_http.ticket = replace(self.fixture.ticket, objective="changed objective")
        with self.assertRaises(ValueError):
            self.runtime.stage({"reference": reference, "role": "implement", "results": []})

    def test_stale_preview_generation_blocks_workers_and_jira_writeback(self):
        reference = self.runtime.prepare(self.fixture.spec)
        next_preview = replace(
            self.fixture.preview, scope=replace(self.fixture.ticket.scope, generation=2)
        )
        self.fixture.registry.start(next_preview, 1, NOW)
        with self.assertRaises(RemoteError):
            self.runtime.stage({"reference": reference, "role": "implement", "results": []})
        with self.assertRaises(RemoteError):
            self.runtime.finish({"reference": reference, "results": []})
        self.assertEqual(self.fixture.jira_http.puts, 0)

    def test_stage_order_blocked_review_and_test_failure_cannot_pass(self):
        reference = self.runtime.prepare(self.fixture.spec)
        with self.assertRaises(ValueError):
            self.runtime.stage({"reference": reference, "role": "qa", "results": []})
        implementation = self.runtime.stage(
            {"reference": reference, "role": "implement", "results": []}
        )
        with patch.object(
            self.runtime.worker,
            "execute",
            return_value={"verdict": "blocked", "summary": "reject", "edits": []},
        ):
            review = self.runtime.stage(
                {"reference": reference, "role": "review", "results": [implementation]}
            )
        self.assertEqual(
            self.runtime.finish({"reference": reference, "results": [implementation, review]})[
                "outcome"
            ],
            "blocked",
        )
        with self.assertRaises(ValueError):
            self.runtime.stage(
                {"reference": reference, "role": "test", "results": [implementation, review]}
            )

    def test_model_edits_and_output_types_are_fail_closed(self):
        good = {
            "verdict": "passed",
            "summary": "change",
            "edits": [{"path": "app.py", "content": "x"}],
        }
        for path in (
            "../secret",
            "/tmp/x",
            ".github/workflows/ci.yml",
            ".env",
            ".claude/settings.json",
            "other.py",
        ):
            with self.assertRaises(ValueError):
                validate_output(
                    {**good, "edits": [{"path": path, "content": "x"}]},
                    "implement",
                    self.fixture.settings.allowed_files,
                )
        for value in (
            {**good, "verdict": True},
            {**good, "extra": "ignored"},
            {**good, "edits": []},
        ):
            with self.assertRaises(ValueError):
                validate_output(value, "implement", self.fixture.settings.allowed_files)
        with self.assertRaises(ValueError):
            validate_output(good, "review", self.fixture.settings.allowed_files)

    def test_process_timeout_output_limit_and_cancellation(self):
        for script, options in (
            ("import time; time.sleep(5)", {"timeout": 1}),
            ("print('x'*10000)", {"max_output": 100}),
        ):
            with self.assertRaises(ExecutionBlocked):
                run_process(
                    [sys.executable, "-c", script],
                    self.fixture.root,
                    minimal_environment(),
                    **options,
                )

        def cancel():
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            run_process(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                self.fixture.root,
                minimal_environment(),
                heartbeat=cancel,
            )
        result = run_process(
            [sys.executable, "-c", "import sys; print('out'); print('err',file=sys.stderr)"],
            self.fixture.root,
            minimal_environment(),
        )
        self.assertEqual((result.code, result.output, result.stderr), (0, b"out\n", b"err\n"))

    def test_artifact_tampering_changed_input_and_policy_are_rejected(self):
        journal = Journal(self.fixture.root / "other-private")
        result = journal.compute("key", {"a": 1}, lambda: {"ok": True})
        with self.assertRaises(ValueError):
            journal.compute("key", {"a": 2}, lambda: {})
        path = journal.root / (digest("key") + ".json")
        raw = json.loads(path.read_text())
        raw["result"] = {"ok": False}
        path.write_text(json.dumps(raw))
        with self.assertRaises(ValueError):
            journal.read("key", digest(result))
        reference = self.runtime.prepare(self.fixture.spec)
        self.runtime.policy = "changed"
        with self.assertRaises(ValueError):
            self.runtime.stage({"reference": reference, "role": "implement", "results": []})

    def test_patch_handles_new_files_empty_files_and_no_final_newline(self):
        patch_text = make_patch(
            {"app.py": "def answer():\n    return 1\n", "new.py": None},
            [{"path": "app.py", "content": "answer = 2"}, {"path": "new.py", "content": "x = 1\n"}],
        )
        subprocess.run(
            ["git", "apply", "--check", "-"],
            cwd=self.fixture.repo,
            input=patch_text.encode(),
            check=True,
            capture_output=True,
        )
        with self.assertRaises(ExecutionBlocked):
            make_patch({"app.py": "same"}, [{"path": "app.py", "content": "same"}])

    def test_configuration_does_not_leak_secrets_and_rejects_unsafe_values(self):
        self.assertNotIn("synthetic-private-token", repr(self.fixture.settings))
        self.assertNotIn("synthetic-model-key", repr(self.fixture.settings))
        for changes in (
            {"SRE_JIRA_BASE_URL": "http://host"},
            {"SRE_TEST_IMAGE": "python:latest"},
            {"SRE_ALLOWED_FILES_JSON": '["../outside"]'},
            {"SRE_CLAUDE_ENABLED": "yes"},
            {"SRE_WORK_ROOT": str(self.fixture.repo / "private")},
            {"SRE_CLAUDE_BUDGET_USD": "nan"},
        ):
            with self.assertRaises(ValueError):
                Settings.from_env({**self.fixture.env, **changes})
        with self.assertRaises(ValueError):
            Settings.from_env({})

    def test_production_and_mismatched_start_scope_are_rejected(self):
        from sre_suite.contracts import Environment

        scope = replace(self.fixture.ticket.scope, environment=Environment.PRODUCTION)
        with self.assertRaises(ValueError):
            execution_key({**self.fixture.spec, "scope": asdict(scope)})
        with self.assertRaises(ValueError):
            self.runtime.prepare(
                {
                    **self.fixture.spec,
                    "scope": asdict(replace(self.fixture.ticket.scope, generation=2)),
                }
            )

    def test_remote_client_refuses_redirect_and_credential_urls(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", None, "https://other"))
        with self.assertRaises(ValueError):
            JsonHttp("https://user:secret@host", "Bearer fixture")
        with self.assertRaises(ValueError):
            JsonHttp("https://host", "Bearer fixture").call("GET", "//other/private")

    def test_strict_ticket_decoder_rejects_unknown_fields_and_legacy_scope(self):
        ticket = ticket_json(self.fixture.ticket)
        with self.assertRaises(ValueError):
            ticket_envelope({"schema_version": 2, "ticket": {**ticket, "role": "admin"}})
        del ticket["scope"]["generation"]
        with self.assertRaises(ValueError):
            ticket_envelope({"schema_version": 2, "ticket": ticket})

    def test_remote_temporal_requires_tls_including_ipv6(self):
        from sre_suite.execution.config import temporal_address

        for address in ("localhost:7233", "127.0.0.1:7233", "[::1]:7233"):
            self.assertEqual(temporal_address(address, False), address)
        for address in ("remote.example:7233", "[2001:db8::1]:7233"):
            with self.assertRaises(ValueError):
                temporal_address(address, False)
            self.assertEqual(temporal_address(address, True), address)
        for address in ("user:secret@host:7233", "host", "host:7233/path", "host:7233?q=1"):
            with self.assertRaises(ValueError):
                temporal_address(address, True)

    def test_failed_test_execution_blocks_qa_and_review_ready(self):
        reference = self.runtime.prepare(self.fixture.spec)
        results = []
        for role in ("implement", "review"):
            results.append(
                self.runtime.stage({"reference": reference, "role": role, "results": results})
            )
        with patch.object(
            self.runtime.tests, "execute", return_value={"passed": False, "exit_code": 1}
        ):
            results.append(
                self.runtime.stage({"reference": reference, "role": "test", "results": results})
            )
        self.assertEqual(
            self.runtime.finish({"reference": reference, "results": results})["outcome"], "blocked"
        )
        with self.assertRaises(ValueError):
            self.runtime.stage({"reference": reference, "role": "qa", "results": results})

    def test_stage_evidence_must_attest_exact_saved_artifact(self):
        reference = self.runtime.prepare(self.fixture.spec)
        result = self.runtime.stage({"reference": reference, "role": "implement", "results": []})
        result["evidence"] = json.loads(
            json.dumps(asdict(self.fixture.ticket.evidence[0]), default=str)
        )
        with self.assertRaises(ValueError):
            self.runtime.stage({"reference": reference, "role": "review", "results": [result]})

    def test_generation_changed_during_model_call_blocks_evidence_upload(self):
        reference = self.runtime.prepare(self.fixture.spec)
        original = self.runtime.worker.execute

        def change_generation(*args):
            result = original(*args)
            self.fixture.registry.start(
                replace(
                    self.fixture.preview, scope=replace(self.fixture.ticket.scope, generation=2)
                ),
                1,
                NOW,
            )
            return result

        with patch.object(self.runtime.worker, "execute", side_effect=change_generation):
            with self.assertRaises(RemoteError):
                self.runtime.stage({"reference": reference, "role": "implement", "results": []})
        self.assertEqual(self.fixture.jira_http.puts, 0)

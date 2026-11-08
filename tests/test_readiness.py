import asyncio
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, patch

from sre_suite.execution.cli import main
from sre_suite.execution.http import JsonHttp, RemoteError
from sre_suite.execution.process import ProcessResult
from sre_suite.execution.readiness import (
    Check,
    Readiness,
    exit_code,
    report,
    run_preflight,
    temporal_config,
)
from tests.execution_fixture import ApiTransport, Fixture
from tests.publication_fixture import RULES, publication_runtime
from tests.test_foundation import NOW

HELP = (
    b"--restricted --tools --strict-mcp-config --json-schema "
    b"--max-budget-usd --setting-sources --no-session-persistence"
)


class ProbeHttp:
    def __init__(self, kind, fixture, delegate=None):
        self.kind, self.fixture, self.delegate = kind, fixture, delegate
        self.calls = []
        self.overrides = {}
        self.after = lambda path: None

    def call(self, method, path, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        assert method == "GET" or (
            self.kind == "evidence"
            and method == "POST"
            and path in {"/v1/context", "/v1/evidence/read"}
        )
        if path in self.overrides:
            result = self.overrides[path]
            if isinstance(result, Exception):
                raise result
        elif path == "/rest/api/3/myself":
            result = {"active": True, "accountId": "private-fixture-account"}
        elif path.startswith("/rest/api/3/mypermissions?"):
            result = {
                "permissions": {
                    p: {"havePermission": True}
                    for p in ("BROWSE_PROJECTS", "EDIT_ISSUES", "TRANSITION_ISSUES")
                }
            }
        elif path.startswith("/rest/api/3/status/"):
            result = {"id": path.split("/")[-1], "statusCategory": {"key": "indeterminate"}}
        elif path == "/repos/" + self.fixture.settings.repository:
            result = {
                "full_name": self.fixture.settings.repository,
                "archived": False,
                "disabled": False,
                "permissions": {"push": True},
            }
        elif self.kind == "model":
            assert path.startswith("/v1/models/")
            result = {"id": "fixture-model", "type": "model"}
        else:
            result = self.delegate.call(method, path, payload)
        self.after(path)
        return copy.deepcopy(result)


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fixture = Fixture(Path(directory.name))
        self.addCleanup(self.fixture.close)
        runtime, self.github, self.jira, self.events = publication_runtime(self.fixture)
        self.settings = runtime.settings
        self.env = {
            **self.fixture.env,
            "SRE_GITHUB_PRS_ENABLED": "true",
            "SRE_GITHUB_TOKEN": "private-github-token",
            "SRE_JIRA_TRANSITIONS_ENABLED": "true",
            "SRE_JIRA_TRANSITIONS_JSON": json.dumps(RULES),
        }
        self.transports = {
            k: ProbeHttp(k, self.fixture, v)
            for k, v in {
                "jira": self.jira,
                "github": self.github,
                "evidence": ApiTransport(self.fixture.app),
                "model": None,
            }.items()
        }
        self.runner = Readiness(
            self.settings, self.fixture.spec, self.env, transports=self.transports
        )
        self.processes = []
        self.process_patch = patch(
            "sre_suite.execution.readiness.run_process", side_effect=self.process
        )
        self.process_patch.start()
        self.addCleanup(self.process_patch.stop)

    def process(self, argv, cwd, env, **kwargs):
        self.processes.append(argv)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("SRE_GITHUB_TOKEN", env)
        self.assertNotIn("SRE_JIRA_API_TOKEN", env)
        if argv[0] == "docker":
            self.assertEqual(argv[1:3], ["image", "inspect"])
            return ProcessResult(0, json.dumps([self.settings.test_image]).encode())
        self.assertEqual(argv[1:], ["--bare", "--help"])
        return ProcessResult(0, HELP)

    def run_checks(self):
        self.runner.inspect()
        return self.runner.finish()

    def statuses(self, result):
        return {c["name"]: c["status"] for c in result["checks"]}

    def test_full_read_only_checks_leave_jira_git_journal_and_evidence_unchanged(self):
        before = sorted(self.settings.work_root.iterdir())
        result = self.run_checks()
        self.assertEqual(result["status"], "needs_validation")
        self.assertEqual(exit_code(result), 2)
        self.assertNotIn("fail", self.statuses(result).values())
        self.assertEqual(self.statuses(result)["final_scope_fence"], "pass")
        self.assertEqual(sorted(self.settings.work_root.iterdir()), before)
        self.assertEqual(self.github.writes, [])
        self.assertEqual(self.jira.transitions, [])
        self.assertEqual(self.jira.puts, 0)
        self.assertEqual(self.jira.status, "100")
        self.assertEqual(self.events, [])
        for method, path, _ in self.transports["evidence"].calls:
            self.assertEqual(method, "POST")
            self.assertIn(path, {"/v1/context", "/v1/evidence/read"})

    def test_read_transport_rejects_all_mutating_methods_even_when_flags_are_enabled(self):
        for kind, http in self.runner.http.items():
            for method, path in (
                ("POST", "/pulls"),
                ("POST", "/v1/evidence"),
                ("PUT", "/properties/x"),
                ("DELETE", "/branch"),
                ("POST", "/v1/messages"),
                ("PATCH", "/git/refs/x"),
            ):
                with self.assertRaises(ValueError, msg=kind):
                    http.call(method, path, {})
        self.assertTrue(all(not t.calls for t in self.transports.values()))

    def test_configuration_and_bad_spec_fail_before_any_service_calls(self):
        path = self.fixture.root / "start.json"
        path.write_text(json.dumps(self.fixture.spec))
        result = asyncio.run(run_preflight({}, path))
        self.assertEqual(result["status"], "blocked")
        self.assertIn("SRE_JIRA_API_TOKEN", result["checks"][0]["detail"])
        for raw in (b"{}", b"x" * 65537, b'{"schema_version":2,"schema_version":2}'):
            path.write_bytes(raw)
            result = asyncio.run(run_preflight(self.env, path))
            self.assertEqual(result["status"], "blocked")
        self.assertEqual(
            asyncio.run(run_preflight(self.env, path.with_name("missing")))["status"], "blocked"
        )
        self.assertTrue(all(not t.calls for t in self.transports.values()))

    def test_production_or_missing_generation_spec_is_rejected(self):
        for edit in ({"environment": "production"}, {"generation": 0}):
            spec = copy.deepcopy(self.fixture.spec)
            spec["scope"].update(edit)
            with self.assertRaises(ValueError):
                Readiness(self.settings, spec, self.env, transports=self.transports)
        spec = copy.deepcopy(self.fixture.spec)
        del spec["scope"]["generation"]
        with self.assertRaises(ValueError):
            Readiness(self.settings, spec, self.env, transports=self.transports)

    def test_different_preview_id_is_not_adopted_from_the_ticket(self):
        spec = copy.deepcopy(self.fixture.spec)
        spec["scope"]["environment_id"] = "different-preview"
        self.runner = Readiness(self.settings, spec, self.env, transports=self.transports)
        result = self.run_checks()
        self.assertEqual(self.statuses(result)["jira_ticket"], "fail")
        self.assertEqual(self.transports["evidence"].calls, [])

    def test_replaced_generation_blocks_preview_authorization(self):
        self.supersede()
        result = self.run_checks()
        self.assertEqual(self.statuses(result)["preview_context_evidence"], "fail")
        self.assertEqual(result["status"], "blocked")

    def supersede(self):
        self.fixture.registry.start(
            replace(self.fixture.preview, scope=replace(self.fixture.ticket.scope, generation=2)),
            1,
            NOW,
        )

    def test_generation_changed_after_other_checks_is_detected_by_final_fence(self):
        self.runner.inspect()
        self.supersede()
        self.assertEqual(self.statuses(self.runner.finish())["final_scope_fence"], "fail")

    def test_expired_preview_or_evidence_cannot_pass(self):
        self.fixture.app._clock = lambda: NOW + timedelta(hours=2)
        self.assertEqual(self.statuses(self.run_checks())["preview_context_evidence"], "fail")

    def test_modified_jira_contract_is_detected_at_end(self):
        self.runner.inspect()
        self.jira.ticket = replace(self.fixture.ticket, objective="changed")
        self.assertEqual(self.statuses(self.runner.finish())["final_scope_fence"], "fail")

    def test_jira_auth_failure_skips_dependent_ticket_and_evidence_calls(self):
        self.transports["jira"].overrides["/rest/api/3/myself"] = RemoteError(401)
        result = self.run_checks()
        statuses = self.statuses(result)
        self.assertEqual(statuses["jira_identity"], "fail")
        self.assertEqual(statuses["jira_ticket"], "skipped")
        self.assertEqual(self.transports["evidence"].calls, [])
        self.assertIn("access_denied", json.dumps(result))

    def test_missing_jira_transition_permission_is_blocked(self):
        original = self.transports["jira"].call

        def denied(method, path, payload=None):
            result = original(method, path, payload)
            if "mypermissions" in path:
                result["permissions"]["TRANSITION_ISSUES"]["havePermission"] = False
            return result

        with patch.object(self.transports["jira"], "call", side_effect=denied):
            self.assertEqual(self.statuses(self.run_checks())["jira_permissions"], "fail")

    def test_terminal_target_and_unavailable_initial_transition_are_blocked(self):
        self.transports["jira"].overrides["/rest/api/3/status/300"] = {
            "id": "300",
            "statusCategory": {"key": "done"},
        }
        self.jira.required_fields = True
        statuses = self.statuses(self.run_checks())
        self.assertEqual(statuses["jira_transition_targets"], "fail")
        self.assertEqual(statuses["jira_initial_transition"], "fail")
        self.assertEqual(self.jira.transitions, [])

    def test_github_disabled_archived_wrong_repo_and_denied_role_fail(self):
        path = "/repos/" + self.settings.repository
        original = self.transports["github"].call("GET", path)
        for values in (
            {"archived": True},
            {"disabled": True},
            {"full_name": "other/repo"},
            {"permissions": {"push": False}},
        ):
            self.transports["github"].overrides[path] = {**original, **values}
            with self.assertRaises(ValueError):
                self.runner._github()

    def test_missing_github_role_is_unverified_and_never_assumed_write_permission(self):
        path = "/repos/" + self.settings.repository
        self.transports["github"].overrides[path] = {
            "full_name": self.settings.repository,
            "archived": False,
            "disabled": False,
        }
        result = self.run_checks()
        self.assertEqual(self.statuses(result)["github_repository_role"], "unverified")
        self.assertEqual(result["status"], "needs_validation")

    def test_moved_base_branch_blocks_readiness(self):
        self.github.refs["main"] = "f" * 40
        self.assertEqual(self.statuses(self.run_checks())["github_base"], "fail")

    def test_optional_publication_is_skipped_in_patch_only_mode(self):
        settings = self.fixture.settings
        self.runner = Readiness(
            settings, self.fixture.spec, self.fixture.env, transports=self.transports
        )
        result = self.run_checks()
        self.assertEqual(self.statuses(result)["github_publication"], "skipped")
        self.assertEqual(self.transports["github"].calls, [])

    def test_missing_or_public_journal_is_not_created_or_repaired(self):
        root = self.fixture.root / "nonexistent-private-journal"
        self.runner.settings = replace(self.settings, work_root=root)
        with self.assertRaises(ValueError):
            self.runner._journal()
        self.assertFalse(root.exists())
        root.mkdir(mode=0o755)
        with self.assertRaises(ValueError):
            self.runner._journal()
        self.assertEqual(root.stat().st_mode & 0o777, 0o755)

    def test_no_cli_prompt_image_pull_or_container_run_is_used(self):
        self.run_checks()
        self.assertEqual(len(self.processes), 2)
        self.assertTrue(any("--help" in p for p in self.processes))
        self.assertTrue(any(p[:3] == ["docker", "image", "inspect"] for p in self.processes))

    def test_incompatible_cli_and_missing_image_are_reported_without_output_leak(self):
        with patch(
            "sre_suite.execution.readiness.run_process",
            return_value=ProcessResult(1, b"private-secret", b"token"),
        ):
            result = self.run_checks()
        self.assertEqual(self.statuses(result)["claude_cli"], "fail")
        self.assertEqual(self.statuses(result)["test_image"], "fail")
        self.assertNotIn("private-secret", json.dumps(result))

    def test_raw_errors_credentials_and_private_metadata_are_not_reported(self):
        secret = "private-token-and-company-url"
        self.transports["model"].overrides["/v1/models/" + self.settings.claude_model] = ValueError(
            secret
        )
        result = self.run_checks()
        text = json.dumps(result)
        for value in (
            secret,
            self.settings.jira_token,
            self.settings.claude_key,
            "private-fixture-account",
            str(self.settings.repository_path),
        ):
            self.assertNotIn(value, text)
        self.assertEqual(self.statuses(result)["model_access"], "fail")

    def test_deadline_prevents_further_requests(self):
        self.runner.deadline = 0
        result = self.run_checks()
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(all(not t.calls for t in self.transports.values()))
        self.assertEqual(self.processes, [])

    def test_temporal_configuration_rejects_remote_plaintext_or_empty_queue(self):
        for env in (
            {"SRE_TEMPORAL_ADDRESS": "remote.example:7233"},
            {"SRE_TEMPORAL_ADDRESS": "[2001:db8::1]:7233"},
            {"SRE_TEMPORAL_TASK_QUEUE": ""},
            {"SRE_TEMPORAL_NAMESPACE": "bad\nname"},
        ):
            with self.assertRaises(ValueError):
                temporal_config(env)

    def test_complete_cli_emits_json_and_nonzero_for_unverified_capabilities(self):
        path = self.fixture.root / "start.json"
        path.write_text(json.dumps(self.fixture.spec))
        module = ModuleType("sre_suite.execution.readiness_temporal")
        module.probe = AsyncMock(return_value=[Check("temporal_namespace", "pass", "Fixture")])
        with (
            patch("sre_suite.execution.readiness.Readiness", return_value=self.runner),
            patch.dict("sys.modules", {module.__name__: module}),
            patch.dict("os.environ", self.env),
            patch("sys.argv", ["sre", "preflight", str(path)]),
            redirect_stdout(io.StringIO()) as stdout,
        ):
            with self.assertRaises(SystemExit) as stopped:
                main()
        self.assertEqual(stopped.exception.code, 2)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["status"], "needs_validation")
        module.probe.assert_awaited_once()

    def test_report_exit_codes_and_failed_checks_take_precedence(self):
        self.assertEqual(exit_code(report([Check("x", "pass", "ok")])), 0)
        self.assertEqual(exit_code(report([Check("x", "unverified", "unknown")])), 2)
        self.assertEqual(
            exit_code(report([Check("x", "unverified", "unknown"), Check("y", "fail", "bad")])), 1
        )

    def test_http_model_headers_and_timeout_never_enter_payload_or_response_report(self):
        seen = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        class Opener:
            def open(self, request, timeout):
                seen.append((request, timeout))
                return Response(b'{"type":"model","id":"fixture"}')

        http = JsonHttp(
            "https://api.anthropic.com",
            "Bearer private-token",
            headers={"anthropic-version": "2026-10-21"},
            timeout=5,
        )
        http._opener = Opener()
        self.assertEqual(http.call("GET", "/v1/models/fixture")["id"], "fixture")
        request, timeout = seen[0]
        self.assertEqual(timeout, 5)
        self.assertIsNone(request.data)
        self.assertEqual(request.get_header("Authorization"), "Bearer private-token")
        self.assertEqual(request.get_header("Anthropic-version"), "2026-10-21")

    def test_existing_execution_journal_blocks_fresh_start_without_modifying_it(self):
        self.fixture.runtime.prepare(self.fixture.spec)
        before = {p.name: p.read_bytes() for p in self.settings.work_root.iterdir()}
        result = self.run_checks()
        self.assertEqual(self.statuses(result)["execution_journal"], "fail")
        self.assertEqual(
            {p.name: p.read_bytes() for p in self.settings.work_root.iterdir()}, before
        )

    def test_retired_model_metadata_is_not_treated_as_readiness(self):
        self.transports["model"].overrides["/v1/models/" + self.settings.claude_model] = {
            "type": "model",
            "id": "fixture",
            "lifecycle": "retired",
        }
        self.assertEqual(self.statuses(self.run_checks())["model_access"], "fail")

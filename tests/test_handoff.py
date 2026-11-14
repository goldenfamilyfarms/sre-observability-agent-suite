"""Published plan to preflight with real scoped evidence and zero external writes."""

import asyncio
import copy
import io
import json
from contextlib import redirect_stdout
from dataclasses import replace
from types import ModuleType
from unittest.mock import AsyncMock, patch

from sre_suite.execution.http import RemoteError
from sre_suite.execution.process import ProcessResult
from sre_suite.execution.readiness import Check, Readiness, report, run_preflight
from sre_suite.execution.runtime import execution_key
from sre_suite.execution.wire import canonical, digest
from sre_suite.jira_planning.__main__ import main
from sre_suite.jira_planning.handoff import handoff
from tests.test_jira_planning import PlanningFixture
from tests.test_local_pilot import LocalFixture
from tests.test_readiness import HELP, ProbeHttp


class HandoffTests(PlanningFixture):
    def setUp(self):
        super().setUp()
        self.published = self.publish()
        self.plan_path = self.root / "plan.json"
        self.publication_path = self.root / "published.json"
        self.output = self.root / "handoff"
        self.plan_path.write_text(canonical(self.plan.json()))
        self.publication_path.write_text(canonical(self.published))
        self.calls_before = len(self.http.calls)
        self.observed_specs = []

    async def preflight(self, env, path):
        spec = json.loads(path.read_text())
        self.observed_specs.append(spec)
        return report(
            [Check("inference", "unverified", "Synthetic unverified capability")],
            execution_key(spec),
        )

    def transfer(self, **changes):
        values = {
            "env": self.env,
            "plan_path": self.plan_path,
            "publication_path": self.publication_path,
            "journal": self.journal,
            "node_id": "base",
            "run_id": "handoff-one",
            "output": self.output,
            "planner": self.planner,
            "preflight": self.preflight,
        }
        values.update(changes)
        return asyncio.run(handoff(**values))

    def test_generates_exact_spec_and_preflight_without_jira_or_journal_writes(self):
        before = {p.name: p.read_bytes() for p in self.journal.iterdir()}
        result = self.transfer()
        spec = json.loads((self.output / "execution.json").read_text())
        self.assertEqual(
            spec,
            {
                "schema_version": 2,
                "ticket_key": "DEMO-101",
                "scope": json.loads(canonical(self.plan.scope.__dict__)),
                "run_id": "handoff-one",
            },
        )
        self.assertEqual(result["workflow_id"], execution_key(spec))
        self.assertEqual(result["preflight_exit_code"], 2)
        self.assertFalse(result["execution_started"])
        self.assertEqual(result["execution_sha256"], digest(spec))
        self.assertEqual(
            result["preflight_sha256"],
            digest(json.loads((self.output / "preflight.json").read_text())),
        )
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.journal.iterdir()})
        self.assertTrue(
            all(method == "GET" for method, _, _ in self.http.calls[self.calls_before :])
        )
        for path in self.output.iterdir():
            self.assertEqual(path.stat().st_mode & 0o077, 0)
            self.assertNotIn(self.env["SRE_JIRA_API_TOKEN"], path.read_text())

    def test_receipt_tampering_missing_duplicate_or_foreign_keys_rejected(self):
        for mutate in (
            lambda p: p.update(approval_digest="0" * 64),
            lambda p: p.update(execution_started=True),
            lambda p: p["issues"].update(base="DEMO-999"),
            lambda p: p["issues"].update(base="OTHER-1"),
            lambda p: p["issues"].pop("followup"),
            lambda p: p["issues"].update(followup="DEMO-101"),
        ):
            raw = copy.deepcopy(self.published)
            mutate(raw)
            self.publication_path.write_text(canonical(raw))
            with self.assertRaises(ValueError):
                self.transfer()
            self.assertFalse(self.output.exists())
        self.assertEqual(self.observed_specs, [])

    def test_changed_plan_or_runtime_target_cannot_handoff(self):
        self.planner.config = replace(self.planner.config, contract_field="customfield_999")
        with self.assertRaises(ValueError):
            self.transfer()
        self.assertEqual(self.observed_specs, [])

    def test_incomplete_or_corrupt_journal_cannot_handoff(self):
        path = next(self.journal.glob("*.json"))
        original = path.read_bytes()
        for field, value in (("ready", False), ("key", "DEMO-999")):
            raw = json.loads(original)
            raw["state"]["nodes"]["base"][field] = value
            raw["checksum"] = digest(raw["state"])
            path.write_text(canonical(raw))
            with self.assertRaises(ValueError):
                self.transfer()
        path.write_bytes(original + b"invalid")
        with self.assertRaises(ValueError):
            self.transfer()
        self.assertEqual(self.observed_specs, [])

    def test_unknown_node_and_bad_run_id_never_probe(self):
        for changes in ({"node_id": "missing"}, {"run_id": "../escape"}):
            with self.assertRaises(ValueError):
                self.transfer(**changes)
        self.assertEqual(self.observed_specs, [])

    def test_changed_remote_contract_marker_and_terminal_selected_issue_block(self):
        original = copy.deepcopy(self.http.issues)
        for mutate in (
            lambda: self.http.issues["DEMO-101"]["marker"].update(node_id="other"),
            lambda: self.http.issues["DEMO-101"]["fields"].update(customfield_12345="{}"),
            lambda: self.http.issues["DEMO-101"]["fields"]["status"].update(
                statusCategory={"key": "done"}
            ),
        ):
            self.http.issues = copy.deepcopy(original)
            mutate()
            with self.assertRaises(ValueError):
                self.transfer()
            self.assertFalse(self.output.exists())

    def test_dependencies_must_be_accepted_before_selecting_followup(self):
        with self.assertRaises(ValueError):
            self.transfer(node_id="followup")
        self.http.issues["DEMO-101"]["fields"]["status"] = {
            "name": "Done",
            "statusCategory": {"key": "done"},
        }
        self.transfer(node_id="followup")
        self.assertEqual(self.observed_specs[0]["ticket_key"], "DEMO-102")

    def test_replaced_preview_blocks_before_probe(self):
        self.supersede()
        with self.assertRaises(RemoteError):
            self.transfer()
        self.assertEqual(self.observed_specs, [])
        self.assertFalse(self.output.exists())

    def test_generation_change_during_preflight_discards_outputs(self):
        async def changed(env, path):
            result = await self.preflight(env, path)
            self.supersede()
            return result

        with self.assertRaises(RemoteError):
            self.transfer(preflight=changed)
        self.assertFalse(self.output.exists())

    def test_journal_change_during_preflight_discards_outputs(self):
        async def changed(env, path):
            result = await self.preflight(env, path)
            source = next(self.journal.glob("*.json"))
            source.write_text("{}")
            return result

        with self.assertRaises(ValueError):
            self.transfer(preflight=changed)
        self.assertFalse(self.output.exists())

    def test_remote_change_during_preflight_discards_outputs(self):
        async def changed(env, path):
            result = await self.preflight(env, path)
            self.http.issues["DEMO-101"]["fields"]["summary"] = "changed"
            return result

        with self.assertRaises(ValueError):
            self.transfer(preflight=changed)
        self.assertFalse(self.output.exists())

    def test_probe_exception_mutated_spec_and_mismatched_report_fail_closed(self):
        async def failure(env, path):
            raise RuntimeError("synthetic")

        async def changed(env, path):
            result = await self.preflight(env, path)
            path.write_text("{}")
            return result

        async def foreign(env, path):
            result = await self.preflight(env, path)
            result["execution_id"] = "wrong"
            return result

        for probe in (failure, changed, foreign):
            with self.assertRaises((ValueError, RuntimeError)):
                self.transfer(preflight=probe)
            self.assertFalse(self.output.exists())

    def test_blocked_preflight_is_saved_and_never_becomes_execution_permission(self):
        async def blocked(env, path):
            return report(
                [Check("docker", "fail", "Synthetic unavailable image")],
                execution_key(json.loads(path.read_text())),
            )

        result = self.transfer(preflight=blocked)
        self.assertEqual(result["preflight_exit_code"], 1)
        self.assertFalse(result["execution_started"])
        self.assertEqual((self.http.creates, self.http.puts), (2, 2))

    def test_existing_unsafe_or_symlink_outputs_are_not_overwritten(self):
        self.transfer()
        before = (self.output / "handoff.json").read_bytes()
        with self.assertRaises(ValueError):
            self.transfer()
        self.assertEqual(before, (self.output / "handoff.json").read_bytes())
        with self.assertRaises(ValueError):
            self.transfer(output=self.fixture.repo / "output")
        alias = self.root / "alias"
        alias.symlink_to(self.output)
        with self.assertRaises(ValueError):
            self.transfer(output=alias)

    def test_cli_propagates_preflight_exit_code_without_starting_work(self):
        with (
            patch(
                "sys.argv",
                [
                    "planning",
                    "handoff",
                    str(self.plan_path),
                    "--publication",
                    str(self.publication_path),
                    "--journal",
                    str(self.journal),
                    "--node",
                    "base",
                    "--run-id",
                    "one",
                    "--output",
                    str(self.output),
                ],
            ),
            patch(
                "sre_suite.jira_planning.handoff.handoff",
                AsyncMock(return_value={"preflight_exit_code": 2, "execution_started": False}),
            ) as operation,
            redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit) as stopped,
        ):
            main()
        self.assertEqual(stopped.exception.code, 2)
        operation.assert_awaited_once()

    def test_real_preflight_consumes_generated_ticket_with_read_only_transports(self):
        transports = {
            "jira": ProbeHttp("jira", self.fixture, self.http),
            "evidence": self.fixture.evidence.http,
            "model": ProbeHttp("model", self.fixture),
        }

        def readiness(settings, spec, env):
            return Readiness(settings, spec, env, transports=transports)

        def process(argv, *args, **kwargs):
            return ProcessResult(
                0,
                json.dumps([self.fixture.settings.test_image]).encode()
                if argv[0] == "docker"
                else HELP,
            )

        temporal = ModuleType("sre_suite.execution.readiness_temporal")
        temporal.probe = AsyncMock(
            return_value=[Check("temporal", "pass", "Synthetic observed pollers")]
        )
        with (
            patch("sre_suite.execution.readiness.Readiness", side_effect=readiness),
            patch("sre_suite.execution.readiness.run_process", side_effect=process),
            patch.dict("sys.modules", {"sre_suite.execution.readiness_temporal": temporal}),
        ):
            self.transfer(preflight=run_preflight)
        result = json.loads((self.output / "preflight.json").read_text())
        self.assertEqual(result["status"], "needs_validation", result)
        self.assertTrue(
            all(method == "GET" for method, _, _ in self.http.calls[self.calls_before :])
        )

    def test_read_only_transport_rejects_accidental_writing_adapter_call(self):
        def accidental_write(planner, *args, **kwargs):
            return planner.http.call("POST", "/rest/api/3/issue", {})

        with (
            patch("sre_suite.jira_planning.client.Planner.issue", accidental_write),
            self.assertRaises(ValueError),
        ):
            self.transfer()
        self.assertEqual((self.http.creates, self.http.puts), (2, 2))
        self.assertFalse(self.output.exists())


class LocalHandoffTests(LocalFixture):
    def test_local_command_reuses_scoped_worker_configuration_without_starting_processes(self):
        from sre_suite.local_pilot.__main__ import main as local_main
        from sre_suite.local_pilot.config import token, write_json

        self.initialize()
        worker = self.root / "worker.json"
        write_json(worker, {"SRE_JIRA_API_TOKEN": "private-fixture-jira"})
        argv = [
            "local",
            "handoff",
            "--directory",
            str(self.stack),
            "--worker-env",
            str(worker),
            "--plan",
            str(self.root / "plan.json"),
            "--publication",
            str(self.root / "published.json"),
            "--planning-journal",
            str(self.root / "planning"),
            "--issue-type-id",
            "10001",
            "--node",
            "base",
            "--run-id",
            "one",
            "--output",
            str(self.root / "handoff"),
        ]
        for code in (0, 1, 2):
            output = io.StringIO()
            with (
                patch("sys.argv", argv),
                patch.dict(
                    "os.environ",
                    {"SRE_PILOT_SETUP_TOKEN": "ambient-controller", "UNRELATED_SECRET": "ambient"},
                ),
                patch("sre_suite.local_pilot.__main__.require_local_docker"),
                patch("sre_suite.local_pilot.__main__.subprocess.run") as process,
                patch(
                    "sre_suite.jira_planning.handoff.handoff",
                    AsyncMock(
                        return_value={"preflight_exit_code": code, "execution_started": False}
                    ),
                ) as operation,
                redirect_stdout(output),
                self.assertRaises(SystemExit) as stopped,
            ):
                local_main()
            self.assertEqual(stopped.exception.code, code)
            process.assert_not_called()
            env = operation.await_args.args[0]
            self.assertEqual(env["SRE_JIRA_ISSUE_TYPE_ID"], "10001")
            self.assertEqual(env["SRE_EVIDENCE_API_TOKEN"], token(self.stack, "worker.token"))
            self.assertNotIn("SRE_PILOT_SETUP_TOKEN", env)
            self.assertNotIn("UNRELATED_SECRET", env)
            self.assertNotIn("private-fixture-jira", output.getvalue())
            self.assertFalse(json.loads(output.getvalue())["execution_started"])

"""Engineering -> managed target -> real collectors -> isolated full 120-check verifier."""

import asyncio
import copy
import json
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

from temporalio import activity
from temporalio.worker import Replayer

from backend_pilot_tests.reviewed_policy import reviewed
from backend_pilot_tests.test_backends import BackendHarness
from sre_suite.api_wire import evidence_from_wire
from sre_suite.demo_fixture.managed import ManagedPolicy
from sre_suite.execution.http import RemoteError
from sre_suite.execution.journal import Journal
from sre_suite.execution.runtime import ExecutionRuntime, execution_key
from sre_suite.execution.temporal_activities import Activities
from sre_suite.execution.temporal_workflow import TicketExecution
from sre_suite.local_pilot.config import write_json
from sre_suite.local_telemetry.certification import prepare
from sre_suite.previews import PreviewRegistry
from sre_suite.telemetry.temporal_activities import TelemetryActivities
from sre_suite.telemetry.temporal_workflow import TelemetryCollection
from sre_suite.verification.runtime import CertificationEngine
from sre_suite.verification.temporal import ServiceCertification, VerificationActivities
from tests.test_api import TOKEN
from tests.verification_fixture import THIRD


class FullCertificationTests(BackendHarness):
    async def configure_backends(self):
        await super().configure_backends()
        raw = reviewed(self.f.config)
        self.input_policy = self.root / "reviewed-managed.json"
        write_json(self.input_policy, raw)
        self.bundle = self.root / "full-certification"
        prepare(self.stack, self.input_policy, self.bundle, window_seconds=15)
        self.f.config = json.loads((self.bundle / "managed-demo.json").read_text())
        self.f.path = self.bundle / "managed-demo.json"
        self.f.policy = ManagedPolicy(self.f.config)
        self.f.d.policy = self.f.policy.fixture
        self.f.d.runner.policy = self.f.d.assessor.policy = self.f.d.policy
        self.f.request["plan"].update(
            policy_sha256=self.f.d.policy.verification.fingerprint,
            fixture=self.f.d.policy.dispatch,
            demo=self.f.policy.dispatch,
        )
        self.f.request["dispatch"] = self.f.d.policy.dispatch

    async def asyncSetUp(self):
        await super().asyncSetUp()
        f = self.f.d.f.f
        engineering = self.f.d.f.engineering
        worker = Mock()
        worker.execute.side_effect = lambda role, payload, heartbeat: {
            "verdict": "passed",
            "summary": "synthetic engineering stage; no model call",
            "edits": copy.deepcopy(self.f.policy.recipe["edits"]) if role == "implement" else [],
        }
        self.runtime = ExecutionRuntime(
            replace(
                engineering.runtime.settings,
                repository=self.f.policy.recipe["repository"],
                allowed_files=("demo_service.py",),
                work_root=self.root / "full-engineering-journal",
                telemetry_config=str(self.bundle / "engineering-telemetry.json"),
                certification_config=str(self.bundle / "engineering-certification.json"),
            ),
            jira=engineering.runtime.jira,
            evidence=engineering.runtime.evidence,
            tests=engineering.runtime.tests,
            worker=worker,
            clock=lambda: datetime.now(UTC),
        )
        self.activities = Activities(self.runtime)
        env = dict(self.f.env, **self.collector_env)
        env["SRE_EVIDENCE_API_TOKEN"] = TOKEN
        journal = self.root / "real-collector"
        journal.mkdir(mode=0o700)
        self.collector = TelemetryActivities(self.bundle / "query-policy.json", journal, env)
        env["SRE_EVIDENCE_API_TOKEN"] = THIRD
        journal = self.root / "real-verifier"
        journal.mkdir(mode=0o700)
        self.verifier = VerificationActivities(
            self.bundle / "verification-policy.json", journal, env
        )
        self.spec = {**f.spec, "run_id": "real-full-certification"}
        self.verification_input = self.verification_receipt = None

    @activity.defn(name="verify_certification")
    def verify(self, raw):
        self.verification_input = copy.deepcopy(raw)
        self.verification_receipt = self.verifier.verify(raw)
        return self.verification_receipt

    async def test_full_engineering_workflow_evaluates_120_real_checks_and_blocks_gaps(self):
        a = self.activities
        workers = [
            *self.workers(),
            self.worker(
                "real-engineering",
                [TicketExecution],
                [a.prepare, a.stage, a.telemetry, a.certification, a.certification_input, a.finish],
            ),
            self.worker("sre-local-telemetry-v1", [TelemetryCollection], [self.collector.collect]),
            self.worker("sre-local-certification-v1", [ServiceCertification], [self.verify]),
        ]
        async with AsyncExitStack() as stack:
            for worker in workers:
                await stack.enter_async_context(worker)
            handle = await self.temporal.client.start_workflow(
                TicketExecution.run,
                self.spec,
                id=execution_key(self.spec),
                task_queue="real-engineering",
                execution_timeout=timedelta(minutes=4),
            )
            result = await handle.result()
        self.assertEqual(result["outcome"], "blocked", result)
        self.assertIsNotNone(self.verification_receipt, result)
        self.assert_stopped()
        ref = evidence_from_wire(self.verification_receipt["evidence"])
        report = json.loads(self.f.d.f.v.api.read(ref))
        self.assertEqual(len(report["results"]), 120)
        for category in ("TRACES", "LOGS", "PROMQL"):
            self.assertEqual(sum(r["check_id"].startswith(category) for r in report["results"]), 40)
        checks = {r["check_id"]: r["outcome"] for r in report["results"]}
        # Meaningful real-data assertions: identity, correlation, counts and histogram coverage.
        for key in (
            "TRACES-027",
            "TRACES-028",
            "TRACES-029",
            "LOGS-001",
            "LOGS-010",
            "LOGS-035",
            "LOGS-039",
            "PROMQL-003",
            "PROMQL-006",
            "PROMQL-008",
            "PROMQL-013",
            "PROMQL-014",
            "PROMQL-037",
        ):
            self.assertEqual(checks[key], "PASS", (key, checks[key]))
        for rule in self.f.d.policy.verification.rules:
            if not rule.applicable:
                expected = "NOT_APPLICABLE"
            elif "/unavailable_measurement" in rule.selector:
                expected = "INCONCLUSIVE"
            elif rule.id == "LOGS-021":
                expected = "FAIL"
            else:
                expected = "PASS"
            self.assertEqual(checks[rule.id], expected, rule.id)
        from collections import Counter

        print("Real backend certification outcomes:", dict(Counter(checks.values())))
        self.assertEqual(sum(v == "NOT_APPLICABLE" for v in checks.values()), 44)
        self.assertEqual(checks["LOGS-021"], "FAIL")  # No exception stack instrumentation.
        self.assertEqual(checks["PROMQL-009"], "INCONCLUSIVE")  # No bound scrape-health source.
        self.assertNotEqual(report["verdict"], "PASSED")
        self.assertFalse(report["merged"] or report["deployed"])
        self.assertFalse(result["certification"]["ready"])
        self.assertEqual(len(self.verification_input["observations"]), 4)
        await Replayer(workflows=[TicketExecution]).replay_workflow(await handle.fetch_history())
        # Exact source-candidate and plan substitutions cannot produce a certificate.
        engine = CertificationEngine(
            self.f.d.policy.verification, self.f.d.f.v.api, Journal(self.root / "negative-verifier")
        )
        for field in ("artifact_digest", "plan_digest"):
            changed = copy.deepcopy(self.verification_input)
            changed["release"][field] = "sha256:" + "e" * 64
            receipt = await asyncio.to_thread(engine.run, changed, publish=True)
            self.assertNotEqual(receipt["verdict"], "PASSED")
        for field, value in (("environment_id", "other-preview"), ("generation", 2)):
            changed = copy.deepcopy(self.verification_input)
            changed["release"]["scope"][field] = value
            with self.assertRaises(ValueError):
                await asyncio.to_thread(engine.run, changed)
        missing = copy.deepcopy(self.verification_input)
        missing["observations"] = missing["observations"][:-1]
        missing_receipt = await asyncio.to_thread(engine.run, missing, publish=True)
        self.assertNotEqual(missing_receipt["verdict"], "PASSED")
        recovered = CertificationEngine(
            self.f.d.policy.verification, self.f.d.f.v.api, Journal(self.verifier.journal)
        )
        replayed_receipt = await asyncio.to_thread(
            recovered.run, self.verification_input, publish=True
        )
        self.assertEqual(replayed_receipt, self.verification_receipt)
        registry = PreviewRegistry(str(self.root / "evidence.db"))
        try:
            registry.start(
                replace(self.f.d.f.f.preview, scope=replace(self.config.scope, generation=2)),
                1,
                datetime.now(UTC),
            )
        finally:
            registry.close()
        with self.assertRaises(RemoteError):
            await asyncio.to_thread(recovered.run, self.verification_input, publish=True)

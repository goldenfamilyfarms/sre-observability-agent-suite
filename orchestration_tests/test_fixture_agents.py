"""Real Temporal independent fixture, assessment, telemetry and certification workers."""

import asyncio
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from sre_suite.demo_fixture.managed_contracts import ready_receipt
from sre_suite.execution.runtime import ExecutionRuntime, execution_key
from sre_suite.execution.temporal_activities import Activities, heartbeat
from sre_suite.execution.temporal_workflow import TicketExecution
from sre_suite.execution.wire import canonical
from sre_suite.fixtures.contracts import FixtureRequest
from sre_suite.fixtures.temporal import FixtureAssessment
from sre_suite.previews import PreviewRegistry
from sre_suite.telemetry.schedule_contracts import TelemetrySchedule
from sre_suite.telemetry.temporal_workflow import TelemetryCollection
from sre_suite.verification.temporal import ServiceCertification
from tests.fixtures_fixture import FixtureAgentFixture


class FixtureWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        now = datetime.now(UTC).replace(microsecond=0)
        with (
            patch("tests.verification_fixture.NOW", now),
            patch("tests.execution_fixture.NOW", now),
            patch("tests.test_api.NOW", now),
            patch("tests.verification_fixture.START", now - timedelta(seconds=120)),
            patch("tests.verification_fixture.END", now - timedelta(seconds=1)),
        ):
            self.f = FixtureAgentFixture(self.root, clock=lambda: datetime.now(UTC))
        self.addCleanup(self.f.close)
        self.live = lambda: datetime.now(UTC)
        self.f.runner.clock = self.f.assessor.clock = self.f.target.clock = self.f.f.app._clock = (
            self.live
        )
        self.engineering = self.f.engineering
        self.activities = Activities(self.engineering.runtime)
        self.executor = ThreadPoolExecutor(max_workers=8)
        self.addCleanup(self.executor.shutdown)
        cache = Path(tempfile.gettempdir()) / "sre-temporal-test-bin"
        cache.mkdir(exist_ok=True)
        self.env = await WorkflowEnvironment.start_local(
            download_dest_dir=str(cache), dev_server_log_level="error", ui=False
        )
        self.addAsyncCleanup(self.env.shutdown)
        self.queue = "fixture-parent-" + uuid4().hex
        self.collected = False
        self.lock = threading.Lock()
        self.running = False
        self.block = False
        self.lose = False
        self.invalidate = False
        self.run_attempts = 0
        self.assess_attempts = 0
        self.certificate = None
        self.managed_mode = False
        self.export_failed = False
        self.export_complete = False
        self.target_stopped = False

    def worker(self, queue, workflows, activities):
        return Worker(
            self.env.client,
            task_queue=queue,
            workflows=workflows,
            activities=activities,
            activity_executor=self.executor,
            max_heartbeat_throttle_interval=timedelta(seconds=1),
            default_heartbeat_throttle_interval=timedelta(seconds=1),
        )

    def parent(self):
        return self.worker(
            self.queue,
            [TicketExecution],
            [
                self.activities.prepare,
                self.activities.stage,
                self.activities.telemetry,
                self.activities.certification,
                self.activities.certification_input,
                self.activities.finish,
            ],
        )

    def runner(self):
        return self.worker(self.f.policy.runner_queue, [], [self.execute, self.publish])

    def assessor(self):
        return self.worker(self.f.policy.assessor_queue, [FixtureAssessment], [self.assess])

    def telemetry(self):
        return self.worker("synthetic-telemetry", [TelemetryCollection], [self.collect])

    def verifier(self):
        return self.worker("synthetic-certification", [ServiceCertification], [self.verify])

    @activity.defn(name="execute_fixture")
    def execute(self, raw):
        self.running = True
        self.run_attempts += 1
        while self.block:
            heartbeat()
            time.sleep(0.02)
        result = self.f.runner.execute(raw["request"], heartbeat)
        if self.lose and self.run_attempts == 1:
            raise ApplicationError("synthetic lost runner acknowledgement")
        return result

    @activity.defn(name="publish_fixture_run")
    def publish(self, raw):
        result = self.f.runner.publish(raw["request"], raw["reference"], heartbeat)
        if self.invalidate:
            registry = PreviewRegistry(str(self.root / "evidence.db"))
            try:
                registry.start(
                    replace(self.f.f.preview, scope=replace(self.f.v.scope, generation=2)),
                    1,
                    self.live(),
                )
            finally:
                registry.close()
        return result

    @activity.defn(name="assess_fixture")
    def assess(self, raw):
        self.assess_attempts += 1
        try:
            result = self.f.assessor.assess(raw["request"], raw["reference"], heartbeat)
        except Exception:
            raise ApplicationError(
                "synthetic assessor rejected evidence", non_retryable=True
            ) from None
        if self.lose and self.assess_attempts == 1:
            raise ApplicationError("synthetic lost assessor acknowledgement")
        return result

    @activity.defn(name="collect_telemetry")
    def collect(self, raw):
        heartbeat()
        if self.managed_mode:
            self.assertTrue(self.export_complete)
            self.assertGreater(self.assess_attempts, 0)
            self.assertTrue(self.target_stopped)
        schedule = TelemetrySchedule.parse(raw["schedule"])
        with self.lock:
            if not self.collected:
                self.engineering.collect(self.plan)
                self.engineering.runtime.clock = self.f.v.engine.clock = self.f.f.app._clock = (
                    self.live
                )
                self.collected = True
            for entry, report in zip(self.plan["schedules"], self.engineering.reports, strict=True):
                if entry["id"] == schedule.workflow_id:
                    return report["collections"][0]
        raise ValueError("unknown telemetry child")

    @activity.defn(name="verify_certification")
    def verify(self, raw):
        self.certificate = self.f.v.engine.run(raw, publish=True, heartbeat=heartbeat)
        return self.certificate

    def enable_managed(self):
        self.managed_mode = True
        self.engineering.config["demo"] = {
            "policy_sha256": "d" * 64,
            "task_queue": "synthetic-managed",
            "startup_seconds": 5,
            "settle_seconds": 1,
        }
        self.engineering.telemetry["delay_seconds"] = 5
        self.engineering.path.write_text(canonical(self.engineering.config))
        self.engineering.telemetry_path.write_text(canonical(self.engineering.telemetry))
        old = self.engineering.runtime
        self.engineering.runtime = ExecutionRuntime(
            old.settings,
            jira=old.jira,
            github=old.github,
            evidence=old.evidence,
            tests=old.tests,
            clock=self.live,
        )
        self.activities = Activities(self.engineering.runtime)

    def managed(self):
        return self.worker(
            "synthetic-managed", [], [self.serve_demo, self.ready_demo, self.export_demo]
        )

    @activity.defn(name="serve_demo_target")
    def serve_demo(self, raw):
        try:
            while True:
                heartbeat()
                time.sleep(0.02)
        finally:
            self.target_stopped = True

    @activity.defn(name="ready_demo_target")
    def ready_demo(self, raw):
        request = FixtureRequest.parse(raw)
        return ready_receipt(request.identity, request.demo, request.binding)

    @activity.defn(name="export_demo_telemetry")
    def export_demo(self, raw):
        if self.export_failed:
            raise ApplicationError("synthetic permanent ingest failure", non_retryable=True)
        request = FixtureRequest.parse(raw)
        self.export_complete = True
        return {
            "workflow_id": request.identity,
            "policy_sha256": request.demo["policy_sha256"],
            "outbox_sha256": "e" * 64,
            "delivered_batches": 2,
            "completed_at": self.live().isoformat(),
        }

    async def test_managed_export_and_assessment_precede_engineering_collection(self):
        self.enable_managed()
        async with (
            self.parent(),
            self.runner(),
            self.assessor(),
            self.managed(),
            self.telemetry(),
            self.verifier(),
        ):
            h = await self.start()
            result = await h.result()
        self.assertEqual(result["outcome"], "ready_for_review")
        self.assertEqual(self.certificate["verdict"], "PASSED")
        self.assertTrue(self.collected)
        self.assertTrue(self.target_stopped)
        await Replayer(workflows=[TicketExecution]).replay_workflow(await h.fetch_history())

    async def test_managed_export_failure_blocks_collection_and_certification(self):
        self.enable_managed()
        self.export_failed = True
        async with (
            self.parent(),
            self.runner(),
            self.assessor(),
            self.managed(),
            self.telemetry(),
            self.verifier(),
        ):
            h = await self.start()
            result = await h.result()
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(result["failed_stage"], "managed_demo")
        self.assertFalse(self.collected)
        self.assertEqual(self.assess_attempts, 0)
        self.assertIsNone(self.certificate)
        self.assertTrue(self.target_stopped)

    async def start(self):
        h = await self.env.client.start_workflow(
            TicketExecution.run,
            self.f.f.spec,
            id=execution_key(self.f.f.spec),
            task_queue=self.queue,
            execution_timeout=timedelta(minutes=2),
        )
        for _ in range(500):
            self.plan = await h.query(TicketExecution.telemetry_plan)
            if self.plan:
                return h
            await asyncio.sleep(0.01)
        self.fail("engineering plan unavailable")

    async def test_complete_120_check_pipeline_retry_and_replay_without_manual_fixture(self):
        self.lose = True
        async with self.parent(), self.runner(), self.assessor(), self.telemetry(), self.verifier():
            h = await self.start()
            # A manual signal cannot replace the required automatic assessor.
            await h.signal(TicketExecution.submit_certification_fixture, {"id": "untrusted"})
            result = await h.result()
            cert_plan = await h.query(TicketExecution.certification_plan)
        self.assertEqual(result["outcome"], "ready_for_review")
        self.assertEqual(self.certificate["verdict"], "PASSED")
        self.assertEqual(self.run_attempts, 2)
        self.assertEqual(self.assess_attempts, 2)
        self.assertEqual(len(self.f.target.calls), 3)
        await Replayer(workflows=[TicketExecution]).replay_workflow(await h.fetch_history())
        child_id = FixtureRequest.parse(
            {"plan": cert_plan, "dispatch": self.f.policy.dispatch}
        ).identity
        child = self.env.client.get_workflow_handle(child_id)
        await Replayer(workflows=[FixtureAssessment]).replay_workflow(await child.fetch_history())

    async def test_failed_fixture_assertion_blocks_certification(self):
        self.f.target.mutate = lambda r: r["result"].update(ok=False)
        async with self.parent(), self.runner(), self.assessor(), self.telemetry(), self.verifier():
            h = await self.start()
            result = await h.result()
        self.assertEqual(result["outcome"], "blocked")
        self.assertIsNone(self.certificate)

    async def test_generation_change_between_runner_and_assessor_cannot_pass(self):
        self.invalidate = True
        async with self.parent(), self.runner(), self.assessor(), self.telemetry(), self.verifier():
            h = await self.start()
            result = await h.result()
        self.assertEqual(result["outcome"], "blocked")
        self.assertIsNone(self.certificate)
        self.assertEqual(self.f.f.jira_http.puts, 0)

    async def test_parent_cancellation_reaches_active_runner(self):
        self.block = True
        async with self.parent(), self.runner(), self.assessor(), self.telemetry(), self.verifier():
            h = await self.start()
            for _ in range(500):
                if self.running:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(self.running)
            plan = await h.query(TicketExecution.certification_plan)
            await h.cancel()
            with self.assertRaises(WorkflowFailureError):
                await h.result()
            child = self.env.client.get_workflow_handle(
                FixtureRequest.parse({"plan": plan, "dispatch": self.f.policy.dispatch}).identity
            )
            with self.assertRaises(WorkflowFailureError):
                await child.result()
        self.assertEqual(self.f.target.calls, [])

    async def test_parent_restart_keeps_automatic_fixture_plan(self):
        async with self.runner(), self.assessor(), self.telemetry(), self.verifier():
            async with self.parent():
                h = await self.start()
                for _ in range(300):
                    plan = await h.query(TicketExecution.certification_plan)
                    if plan:
                        break
                    await asyncio.sleep(0.01)
            async with self.parent():
                result = await h.result()
                self.assertEqual(await h.query(TicketExecution.certification_plan), plan)
        self.assertEqual(result["outcome"], "ready_for_review")
        self.assertEqual(len(self.f.target.calls), 3)


class FixtureProcessTests(unittest.TestCase):
    def test_process_boundary_receipts_credential_isolation_and_timeout(self):
        from sre_suite.execution.process import ExecutionBlocked, ProcessResult
        from sre_suite.execution.wire import canonical
        from sre_suite.fixtures.temporal import FixtureActivities

        with tempfile.TemporaryDirectory() as temp:
            f = FixtureAgentFixture(Path(temp).resolve())
            self.addCleanup(f.close)
            f.begin()
            reference, receipt = f.complete()
            queue = Path(temp).resolve() / "process-journal"
            queue.mkdir(mode=0o700)
            dispatcher = FixtureActivities(
                f.path,
                queue,
                "assessor",
                {"SRE_FIXTURE_TARGET_AUTH": "no-forward", "SRE_EVIDENCE_API_TOKEN": "synthetic"},
            )
            value = {"request": f.request, "reference": reference}
            with (
                patch("sre_suite.fixtures.temporal.heartbeat"),
                patch(
                    "sre_suite.fixtures.temporal.run_process",
                    return_value=ProcessResult(0, canonical(receipt).encode()),
                ) as run,
            ):
                self.assertEqual(dispatcher.assess(value), receipt)
                self.assertNotIn("SRE_FIXTURE_TARGET_AUTH", run.call_args.args[2])
                self.assertEqual(run.call_args.kwargs["timeout"], 300)
                with self.assertRaises(ApplicationError):
                    dispatcher.execute(value)
            with (
                patch("sre_suite.fixtures.temporal.heartbeat"),
                patch("sre_suite.fixtures.temporal.run_process", side_effect=ExecutionBlocked),
                self.assertRaises(ApplicationError),
            ):
                dispatcher.assess(value)


class FixtureCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_authorization_and_duplicate_safe_standalone_dispatch(self):
        import argparse
        import io
        from contextlib import redirect_stdout
        from unittest.mock import AsyncMock

        from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

        from sre_suite.execution.wire import canonical
        from sre_suite.fixtures.__main__ import run

        with tempfile.TemporaryDirectory() as temp:
            f = FixtureAgentFixture(Path(temp).resolve())
            self.addCleanup(f.close)
            f.begin()
            request = Path(temp).resolve() / "request.json"
            request.write_text(canonical(f.request))
            request.chmod(0o600)
            args = argparse.Namespace(
                command="start", policy=f.path, request=request, authorize_fixtures=False
            )
            client = AsyncMock()
            with (
                patch("sre_suite.execution.cli.connect", AsyncMock(return_value=client)) as connect,
                redirect_stdout(io.StringIO()),
            ):
                with self.assertRaises(ValueError):
                    await run(args)
                connect.assert_not_awaited()
                args.authorize_fixtures = True
                await run(args)
                options = client.start_workflow.call_args.kwargs
                self.assertEqual(options["id"], FixtureRequest.parse(f.request).identity)
                self.assertEqual(options["task_queue"], f.policy.assessor_queue)
                self.assertEqual(options["id_reuse_policy"], WorkflowIDReusePolicy.REJECT_DUPLICATE)
                self.assertEqual(options["id_conflict_policy"], WorkflowIDConflictPolicy.FAIL)

                # Managed start must be authorized before any Temporal connection.
                from sre_suite.execution.wire import digest

                f.request["plan"]["demo"] = {
                    "policy_sha256": "d" * 64,
                    "task_queue": "demo-lifecycle",
                    "startup_seconds": 5,
                    "settle_seconds": 1,
                }
                f.request["plan"]["digest"] = digest(
                    {k: v for k, v in f.request["plan"].items() if k != "digest"}
                )
                request.write_text(canonical(f.request))
                connect.reset_mock()
                with self.assertRaises(ValueError):
                    await run(args)
                connect.assert_not_awaited()
                args.authorize_demo = True

                async def change_after_authorization():
                    request.write_text("{}")
                    return client

                connect.side_effect = change_after_authorization
                await run(args)
                self.assertEqual(client.start_workflow.call_args.args[1], f.request)

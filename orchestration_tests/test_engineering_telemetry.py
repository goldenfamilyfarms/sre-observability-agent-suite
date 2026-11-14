"""Real cross-queue Temporal orchestration; synthetic sources and authenticated evidence API."""

import asyncio
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from sre_suite.execution.journal import Journal
from sre_suite.execution.runtime import execution_key
from sre_suite.execution.temporal_activities import Activities
from sre_suite.execution.temporal_workflow import TicketExecution
from sre_suite.telemetry.collector import TelemetryCollector
from sre_suite.telemetry.schedule_contracts import TelemetrySchedule
from sre_suite.telemetry.scheduled_runtime import ScheduledRuntime
from sre_suite.telemetry.temporal_workflow import TelemetryCollection
from tests.execution_fixture import Fixture
from tests.test_engineering_telemetry import configure, source_payload
from tests.test_telemetry import Source


class EngineeringTelemetryEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        now = datetime.now(UTC).replace(microsecond=0)
        with patch("tests.execution_fixture.NOW", now), patch("tests.test_api.NOW", now):
            self.fixture = Fixture(self.root)
        self.addCleanup(self.fixture.close)
        self.runtime, self.policy, _, _, _ = configure(self.fixture, delay=2, window=1)
        self.runtime.clock = lambda: datetime.now(UTC)
        self.fixture.app._clock = self.runtime.clock
        self.activities = Activities(self.runtime)
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.addCleanup(self.executor.shutdown)
        self.queue = "engineering-" + uuid4().hex
        self.attempts = 0
        self.reads = 0
        self.lose_ack = False
        self.partial = False
        self.fail_collection = False
        self.environment = await WorkflowEnvironment.start_local(
            download_dest_dir=str(Path(tempfile.gettempdir()) / "sre-temporal-test-bin"),
            dev_server_log_level="error",
            ui=False,
        )
        self.addAsyncCleanup(self.environment.shutdown)

    def parent_worker(self):
        return Worker(
            self.environment.client,
            task_queue=self.queue,
            workflows=[TicketExecution],
            activities=[
                self.activities.prepare,
                self.activities.stage,
                self.activities.telemetry,
                self.activities.finish,
            ],
            activity_executor=self.executor,
            max_heartbeat_throttle_interval=timedelta(seconds=1),
            default_heartbeat_throttle_interval=timedelta(seconds=1),
        )

    @activity.defn(name="collect_telemetry")
    def collect(self, value):
        activity.heartbeat()
        self.attempts += 1
        if self.fail_collection:
            raise ApplicationError("synthetic collection failure", non_retryable=True)
        schedule = TelemetrySchedule.parse(value["schedule"])
        profile = self.policy.resolve(schedule.request)
        raw = source_payload(schedule.request, profile)
        if self.partial:
            raw["warnings"] = ["synthetic partial result"]

        def on_read():
            self.reads += 1

        source = Source(raw, on_read)
        collector = TelemetryCollector(
            self.policy, self.fixture.evidence, lambda p: source, clock=self.runtime.clock
        )
        scheduled = ScheduledRuntime(collector, Journal(self.root / "telemetry-journal"))
        receipt = scheduled.collect(value)
        if self.lose_ack and self.attempts == 1:
            raise ApplicationError("synthetic lost receipt")
        return receipt

    def telemetry_worker(self):
        return Worker(
            self.environment.client,
            task_queue="synthetic-telemetry",
            workflows=[TelemetryCollection],
            activities=[self.collect],
            activity_executor=self.executor,
            max_heartbeat_throttle_interval=timedelta(seconds=1),
            default_heartbeat_throttle_interval=timedelta(seconds=1),
        )

    async def start(self):
        return await self.environment.client.start_workflow(
            TicketExecution.run,
            self.fixture.spec,
            id=execution_key(self.fixture.spec),
            task_queue=self.queue,
            execution_timeout=timedelta(minutes=2),
        )

    async def waiting(self, handle):
        for _ in range(200):
            if await handle.query(TicketExecution.status) == "collecting_telemetry":
                return await handle.query(TicketExecution.telemetry_plan)
            await asyncio.sleep(0.025)
        self.fail("parent did not reach telemetry")

    async def test_real_child_lost_ack_reuses_capture_and_both_histories_replay(self):
        self.lose_ack = True
        async with self.parent_worker(), self.telemetry_worker():
            handle = await self.start()
            report = await handle.result()
            plan = await handle.query(TicketExecution.telemetry_plan)
        self.assertEqual(report["outcome"], "ready_for_review")
        self.assertEqual(self.attempts, 2)
        self.assertEqual(self.reads, 1)
        self.assertTrue(report["telemetry"]["ready"])
        await Replayer(workflows=[TicketExecution]).replay_workflow(await handle.fetch_history())
        child = self.environment.client.get_workflow_handle(plan["schedules"][0]["id"])
        await Replayer(workflows=[TelemetryCollection]).replay_workflow(await child.fetch_history())

    async def test_parent_worker_restart_keeps_candidate_and_window(self):
        async with self.telemetry_worker():
            async with self.parent_worker():
                handle = await self.start()
                plan = await self.waiting(handle)
            async with self.parent_worker():
                report = await handle.result()
                self.assertEqual(await handle.query(TicketExecution.telemetry_plan), plan)
        self.assertEqual(report["outcome"], "ready_for_review")
        self.assertEqual(self.reads, 1)

    async def test_cancel_parent_cancels_waiting_child_without_collection(self):
        async with self.parent_worker(), self.telemetry_worker():
            handle = await self.start()
            plan = await self.waiting(handle)
            await handle.cancel()
            with self.assertRaises(WorkflowFailureError):
                await handle.result()
            child = self.environment.client.get_workflow_handle(plan["schedules"][0]["id"])
            with self.assertRaises(WorkflowFailureError):
                await child.result()
        self.assertEqual(self.attempts, 0)
        self.assertEqual(self.fixture.jira_http.puts, 0)

    async def test_partial_child_evidence_blocks_parent_readiness(self):
        self.partial = True
        async with self.parent_worker(), self.telemetry_worker():
            handle = await self.start()
            report = await handle.result()
        self.assertEqual(report["outcome"], "blocked")
        self.assertEqual(len(report["telemetry"]["evidence"]), 1)
        self.assertFalse(report["telemetry"]["ready"])

    async def test_child_failure_produces_blocked_parent_report(self):
        self.fail_collection = True
        async with self.parent_worker(), self.telemetry_worker():
            handle = await self.start()
            report = await handle.result()
        self.assertEqual(report["outcome"], "blocked")
        self.assertFalse(report["telemetry"]["ready"])
        self.assertEqual(report["telemetry"]["evidence"], [])


class TelemetryReceiptBoundaryTests(unittest.TestCase):
    def test_process_receipt_accepts_real_api_namespaced_ids_and_rejects_tampering(self):
        import copy

        from sre_suite.execution.process import ProcessResult
        from sre_suite.execution.wire import canonical
        from sre_suite.telemetry.temporal_activities import TelemetryActivities
        from tests.test_engineering_telemetry import EngineeringTelemetryTests

        # Build a real authenticated API receipt, then exercise the process boundary parser.
        fixture = EngineeringTelemetryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        report = fixture.collect()[0]
        receipt = report["collections"][0]
        policy_file = fixture.fixture.root / "policy.json"
        policy_file.write_text(canonical(fixture.config["policy"]))
        policy_file.chmod(0o600)
        journal = fixture.fixture.root / "child-journal"
        journal.mkdir(mode=0o700)
        activities = TelemetryActivities(policy_file, journal, {})
        value = {"schedule": fixture.plan["schedules"][0]["schedule"], "slot": 0}
        with (
            patch("sre_suite.telemetry.temporal_activities.heartbeat"),
            patch(
                "sre_suite.telemetry.temporal_activities.run_process",
                return_value=ProcessResult(0, canonical(receipt).encode()),
            ),
        ):
            self.assertEqual(activities.collect(value), receipt)
        forged = copy.deepcopy(receipt)
        forged["evidence"]["producer"] = "different-producer"
        with (
            patch("sre_suite.telemetry.temporal_activities.heartbeat"),
            patch(
                "sre_suite.telemetry.temporal_activities.run_process",
                return_value=ProcessResult(0, canonical(forged).encode()),
            ),
            self.assertRaises(ApplicationError),
        ):
            activities.collect(value)


class TelemetryPlanExportTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_is_private_exclusive_and_does_not_start_workflow(self):
        import argparse
        import io
        import json
        from contextlib import redirect_stdout
        from unittest.mock import AsyncMock, MagicMock

        from sre_suite.execution.cli import run

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            output = root / "plan.json"
            plan = {"binding": {"artifact_digest": "sha256:" + "a" * 64}}
            client = MagicMock()
            client.get_workflow_handle.return_value.query = AsyncMock(return_value=plan)
            args = argparse.Namespace(
                command="export-telemetry-plan", workflow_id="synthetic-run", output=str(output)
            )
            with (
                patch("sre_suite.execution.cli.connect", AsyncMock(return_value=client)),
                redirect_stdout(io.StringIO()),
            ):
                await run(args)
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(output.read_text()), plan)
                with self.assertRaises(FileExistsError):
                    await run(args)
                client.start_workflow.assert_not_called()
                output.unlink()
                client.get_workflow_handle.return_value.query.return_value = None
                with self.assertRaises(ValueError):
                    await run(args)
                self.assertFalse(output.exists())

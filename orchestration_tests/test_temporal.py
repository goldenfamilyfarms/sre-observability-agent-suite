"""Real local Temporal service; synthetic Jira/API and an executable CLI fixture."""

import asyncio
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Event
from unittest.mock import patch
from uuid import uuid4

from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from orchestration_tests.test_container import TEST_IMAGE
from sre_suite.execution.runtime import execution_key
from sre_suite.execution.temporal_activities import Activities
from sre_suite.execution.temporal_workflow import TicketExecution
from sre_suite.execution.workspace import ContainerTests
from tests.execution_fixture import Fixture


class TemporalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = Fixture(Path(self.temp.name))
        self.addCleanup(self.fixture.close)
        (Path(tempfile.gettempdir()) / "sre-temporal-test-bin").mkdir(exist_ok=True)
        self.environment = await WorkflowEnvironment.start_local(
            download_dest_dir=str(Path(tempfile.gettempdir()) / "sre-temporal-test-bin"),
            dev_server_log_level="error",
            ui=False,
        )
        self.addAsyncCleanup(self.environment.shutdown)
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.addCleanup(self.executor.shutdown)
        self.activities = Activities(self.fixture.runtime)
        self.queue = "test-" + uuid4().hex

    def worker(self, activities=None):
        return Worker(
            self.environment.client,
            task_queue=self.queue,
            workflows=[TicketExecution],
            activities=activities
            or [self.activities.prepare, self.activities.stage, self.activities.finish],
            activity_executor=self.executor,
            max_heartbeat_throttle_interval=timedelta(seconds=1),
            default_heartbeat_throttle_interval=timedelta(seconds=1),
        )

    async def test_end_to_end_real_engine_retry_lost_write_and_history_replay(self):
        self.fixture.jira_http.lose_response = True
        self.fixture.runtime.tests = ContainerTests(
            replace(self.fixture.settings, test_image=TEST_IMAGE)
        )
        identity = execution_key(self.fixture.spec)
        async with self.worker():
            handle = await self.environment.client.start_workflow(
                TicketExecution.run,
                self.fixture.spec,
                id=identity,
                task_queue=self.queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
            report = await asyncio.wait_for(handle.result(), timeout=40)
            self.assertEqual(report["outcome"], "ready_for_review")
            self.assertTrue(report["jira_reported"])
            self.assertEqual(self.fixture.jira_http.puts, 1)
            history = await handle.fetch_history()
        await Replayer(workflows=[TicketExecution]).replay_workflow(history)
        with self.assertRaises(WorkflowAlreadyStartedError):
            await self.environment.client.start_workflow(
                TicketExecution.run,
                self.fixture.spec,
                id=identity,
                task_queue=self.queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )

    async def test_worker_restart_resumes_after_saved_stage_loses_acknowledgement(self):
        identity = execution_key(self.fixture.spec)
        saved = Event()
        interrupted = False

        @activity.defn(name="execute_stage")
        def lose_ack(value: dict) -> dict:
            nonlocal interrupted
            result = self.activities.stage(value)
            if value["role"] == "implement" and not interrupted:
                interrupted = True
                saved.set()
                raise ApplicationError("synthetic lost activity acknowledgement")
            return result

        with patch.object(
            self.fixture.runtime.worker, "execute", wraps=self.fixture.runtime.worker.execute
        ) as worker:
            async with self.worker([self.activities.prepare, lose_ack, self.activities.finish]):
                handle = await self.environment.client.start_workflow(
                    TicketExecution.run, self.fixture.spec, id=identity, task_queue=self.queue
                )
                self.assertTrue(await asyncio.to_thread(saved.wait, 15))
            async with self.worker():
                report = await asyncio.wait_for(handle.result(), timeout=30)
            self.assertEqual(report["outcome"], "ready_for_review")
            roles = [call.args[0] for call in worker.call_args_list]
            self.assertEqual(roles.count("implement"), 1)
        self.assertEqual(self.fixture.jira_http.puts, 1)

    async def test_blocked_review_stops_test_and_qa_stages(self):
        original = self.fixture.runtime.worker.execute

        def reject(role, payload, heartbeat=lambda: None):
            if role == "review":
                return {"verdict": "blocked", "summary": "synthetic review rejection", "edits": []}
            return original(role, payload, heartbeat)

        with (
            patch.object(self.fixture.runtime.worker, "execute", reject),
            patch.object(
                self.fixture.runtime.tests, "execute", side_effect=AssertionError("must not run")
            ),
        ):
            async with self.worker():
                report = await asyncio.wait_for(
                    self.environment.client.execute_workflow(
                        TicketExecution.run,
                        self.fixture.spec,
                        id=execution_key(self.fixture.spec),
                        task_queue=self.queue,
                    ),
                    timeout=30,
                )
        self.assertEqual(report["outcome"], "blocked")
        self.assertTrue(report["jira_reported"])

    async def test_activity_retries_are_bounded(self):
        attempts = []

        @activity.defn(name="prepare_ticket")
        async def fail(value: dict) -> dict:
            attempts.append(activity.info().attempt)
            raise ApplicationError("synthetic transient failure")

        async with self.worker([fail, self.activities.stage, self.activities.finish]):
            report = await asyncio.wait_for(
                self.environment.client.execute_workflow(
                    TicketExecution.run,
                    self.fixture.spec,
                    id=execution_key(self.fixture.spec),
                    task_queue=self.queue,
                ),
                timeout=20,
            )
        self.assertEqual(report["outcome"], "blocked")
        self.assertEqual(attempts, [1, 2, 3])
        self.assertEqual(self.fixture.jira_http.puts, 0)

    async def test_cancellation_reaches_heartbeat_activity_and_stops_workflow(self):
        entered = asyncio.Event()
        canceled = asyncio.Event()

        @activity.defn(name="prepare_ticket")
        async def wait(value: dict) -> dict:
            entered.set()
            try:
                while True:
                    activity.heartbeat()
                    await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                canceled.set()
                raise

        async with self.worker([wait, self.activities.stage, self.activities.finish]):
            handle = await self.environment.client.start_workflow(
                TicketExecution.run,
                self.fixture.spec,
                id=execution_key(self.fixture.spec),
                task_queue=self.queue,
            )
            await asyncio.wait_for(entered.wait(), timeout=10)
            await handle.cancel()
            await asyncio.wait_for(canceled.wait(), timeout=15)
            with self.assertRaises(WorkflowFailureError):
                await handle.result()
        self.assertEqual(self.fixture.jira_http.puts, 0)

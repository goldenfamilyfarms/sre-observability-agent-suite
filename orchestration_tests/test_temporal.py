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
from tests.publication_fixture import publication_runtime


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
        runtime, github, jira, _ = publication_runtime(self.fixture)
        github.lose_once = "/pulls"
        jira.lose_transition = True
        jira.lose_response = True
        self.activities = Activities(runtime)
        runtime.tests = ContainerTests(replace(self.fixture.settings, test_image=TEST_IMAGE))
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
            self.assertEqual(jira.puts, 1)
            self.assertEqual(jira.transitions, ["10", "20"])
            self.assertEqual(len(github.pulls), 1)
            self.assertTrue(report["pull_request"]["draft"])
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

    async def test_preflight_observes_worker_without_starting_any_workflow(self):
        from sre_suite.execution.readiness_temporal import inspect_temporal

        identity = execution_key(self.fixture.spec)
        async with self.worker():
            for _ in range(20):
                checks = await inspect_temporal(self.environment.client, self.queue, identity)
                if not any(c.status == "fail" for c in checks):
                    break
                await asyncio.sleep(0.1)
            self.assertFalse(any(c.status == "fail" for c in checks), checks)
            self.assertEqual({c.name: c.status for c in checks}["temporal_execution_id"], "pass")
            workflows = [w async for w in self.environment.client.list_workflows()]
        self.assertEqual(workflows, [])
        self.assertEqual(self.fixture.jira_http.puts, 0)
        self.assertEqual(list(self.fixture.settings.work_root.iterdir()), [])

    async def test_preflight_blocks_missing_pollers_and_existing_execution_id(self):
        from sre_suite.execution.readiness_temporal import inspect_temporal

        identity = execution_key(self.fixture.spec)
        checks = await inspect_temporal(self.environment.client, self.queue, identity)
        statuses = {c.name: c.status for c in checks}
        self.assertEqual(statuses["temporal_workflow_pollers"], "fail")
        self.assertEqual(statuses["temporal_activity_pollers"], "fail")
        async with self.worker():
            await self.environment.client.execute_workflow(
                TicketExecution.run, self.fixture.spec, id=identity, task_queue=self.queue
            )
            checks = await inspect_temporal(self.environment.client, self.queue, identity)
        self.assertEqual({c.name: c.status for c in checks}["temporal_execution_id"], "fail")
        self.assertEqual(len([w async for w in self.environment.client.list_workflows()]), 1)

    async def test_preflight_connects_read_only_and_rejects_missing_namespace(self):
        from sre_suite.execution.readiness_temporal import probe

        env = {
            "SRE_TEMPORAL_ADDRESS": self.environment.client.service_client.config.target_host,
            "SRE_TEMPORAL_TASK_QUEUE": self.queue,
            "SRE_TEMPORAL_NAMESPACE": "missing-fixture",
        }
        checks = await probe(env, execution_key(self.fixture.spec))
        self.assertTrue(any(c.status == "fail" for c in checks))
        env["SRE_TEMPORAL_NAMESPACE"] = "default"
        async with self.worker():
            checks = await probe(env, execution_key(self.fixture.spec))
        self.assertEqual({c.name: c.status for c in checks}["temporal_namespace"], "pass")
        self.assertEqual([w async for w in self.environment.client.list_workflows()], [])

"""Real Temporal timers/replay/restart plus killable telemetry subprocess boundaries."""

import asyncio
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
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

from sre_suite.execution.http import RemoteError
from sre_suite.execution.journal import Journal
from sre_suite.execution.process import run_process
from sre_suite.execution.temporal_activities import heartbeat
from sre_suite.execution.wire import canonical
from sre_suite.previews import PreviewRegistry
from sre_suite.telemetry.collector import TelemetryCollector
from sre_suite.telemetry.contracts import Backend, QueryPolicy
from sre_suite.telemetry.scheduled_runtime import ScheduledRuntime
from sre_suite.telemetry.temporal_activities import TelemetryActivities
from sre_suite.telemetry.temporal_workflow import TelemetryCollection
from tests.execution_fixture import Fixture
from tests.test_telemetry import Source, profile, response
from tests.test_telemetry_schedule import schedule


class TelemetryEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.now = datetime.now(UTC).replace(microsecond=0)
        for target in ("tests.execution_fixture.NOW", "tests.test_api.NOW"):
            patcher = patch(target, self.now)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fixture = Fixture(self.root)
        self.addCleanup(self.fixture.close)
        self.fixture.app._clock = lambda: datetime.now(UTC)
        self.policy = QueryPolicy(self.fixture.ticket.scope, (profile(Backend.PROMETHEUS),))
        raw = response(Backend.PROMETHEUS, self.policy.scope)
        raw["data"]["result"][0]["values"] = [[self.now.timestamp(), "1"]]
        self.source = Source(raw)
        self.collector = TelemetryCollector(
            self.policy, self.fixture.evidence, lambda p: self.source
        )
        self.runtime = ScheduledRuntime(self.collector, Journal(self.root / "scheduled"))
        self.schedule = schedule(self.policy, now=self.now)
        binary_dir = Path(tempfile.gettempdir()) / "sre-temporal-test-bin"
        binary_dir.mkdir(exist_ok=True)
        self.environment = await WorkflowEnvironment.start_local(
            download_dest_dir=str(binary_dir), dev_server_log_level="error", ui=False
        )
        self.addAsyncCleanup(self.environment.shutdown)
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.addCleanup(self.executor.shutdown)
        self.queue = "telemetry-" + uuid4().hex

    def worker(self, operation):
        return Worker(
            self.environment.client,
            task_queue=self.queue,
            workflows=[TelemetryCollection],
            activities=[operation],
            activity_executor=self.executor,
            max_heartbeat_throttle_interval=timedelta(seconds=1),
            default_heartbeat_throttle_interval=timedelta(seconds=1),
        )

    async def start(self, spec=None):
        spec = spec or self.schedule
        return await self.environment.client.start_workflow(
            TelemetryCollection.run,
            spec.wire(),
            id=spec.workflow_id,
            task_queue=self.queue,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )

    async def test_durable_timers_lost_upload_retry_and_history_replay(self):
        first = True
        upload = self.fixture.evidence.upload

        def lost_upload(*args):
            nonlocal first
            result = upload(*args)
            if first:
                first = False
                raise RemoteError(0)
            return result

        @activity.defn(name="collect_telemetry")
        def collect(value: dict) -> dict:
            heartbeat()
            try:
                return self.runtime.collect(value)
            except RemoteError:
                raise ApplicationError("synthetic lost upload response") from None

        with patch.object(self.fixture.evidence, "upload", side_effect=lost_upload):
            async with self.worker(collect):
                handle = await self.start()
                result = await asyncio.wait_for(handle.result(), 30)
                history = await handle.fetch_history()
        self.assertEqual(result["outcome"], "collected")
        self.assertEqual([r["slot"] for r in result["collections"]], [0, 1])
        self.assertEqual(len(self.source.calls), 2)
        self.assertNotIn("synthetic log", canonical(result))
        await Replayer(workflows=[TelemetryCollection]).replay_workflow(history)
        with self.assertRaises(WorkflowAlreadyStartedError):
            await self.start()

    async def test_saved_capture_survives_worker_restart_after_lost_ack(self):
        saved = Event()
        failed = False

        @activity.defn(name="collect_telemetry")
        def collect(value: dict) -> dict:
            nonlocal failed
            result = self.runtime.collect(value)
            if not failed:
                failed = True
                saved.set()
                raise ApplicationError("synthetic lost acknowledgement")
            return result

        async with self.worker(collect):
            handle = await self.start(replace(self.schedule, occurrences=1))
            self.assertTrue(await asyncio.to_thread(saved.wait, 15))
        self.runtime = ScheduledRuntime(self.collector, Journal(self.root / "scheduled"))
        async with self.worker(collect):
            result = await asyncio.wait_for(handle.result(), 20)
        self.assertEqual(result["outcome"], "collected")
        self.assertEqual(len(self.source.calls), 1)

    async def test_retry_budget_stops_after_three_attempts(self):
        attempts = []

        @activity.defn(name="collect_telemetry")
        async def fail(value: dict) -> dict:
            attempts.append(activity.info().attempt)
            raise ApplicationError("synthetic unavailable backend")

        async with self.worker(fail):
            handle = await self.start()
            result = await asyncio.wait_for(handle.result(), 20)
        self.assertEqual(attempts, [1, 2, 3])
        self.assertEqual(result["outcome"], "blocked")
        self.assertFalse(result["collections"])

    async def test_generation_change_stops_future_collections(self):
        @activity.defn(name="collect_telemetry")
        def collect(value: dict) -> dict:
            try:
                result = self.runtime.collect(value)
            except RemoteError:
                raise ApplicationError("synthetic stale generation", non_retryable=True) from None
            registry = PreviewRegistry(str(self.root / "evidence.db"))
            try:
                registry.start(
                    replace(self.fixture.preview, scope=replace(self.policy.scope, generation=2)),
                    1,
                    datetime.now(UTC),
                )
            finally:
                registry.close()
            return result

        async with self.worker(collect):
            handle = await self.start()
            result = await asyncio.wait_for(handle.result(), 20)
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(len(result["collections"]), 1)
        self.assertEqual(len(self.source.calls), 1)

    async def test_cancel_during_durable_timer_starts_no_collection(self):
        @activity.defn(name="collect_telemetry")
        async def forbidden(value: dict) -> dict:
            raise AssertionError("timer cancellation must not execute an activity")

        spec = replace(self.schedule, first_run_at=self.now + timedelta(seconds=30))
        async with self.worker(forbidden):
            handle = await self.start(spec)
            for _ in range(50):
                if await handle.query(TelemetryCollection.status) == "waiting":
                    break
                await asyncio.sleep(0.1)
            await handle.cancel()
            with self.assertRaises(WorkflowFailureError):
                await asyncio.wait_for(handle.result(), 10)
        self.assertFalse(self.source.calls)

    def process_activities(self, timeout=60):
        path = self.root / "policy.json"
        path.write_text(
            canonical(
                {
                    "schema_version": 2,
                    "scope": asdict(self.policy.scope),
                    "profiles": [
                        {**asdict(p), "scope_labels": dict(p.scope_labels)}
                        for p in self.policy.profiles
                    ],
                }
            )
        )
        path.chmod(0o600)
        return TelemetryActivities(path, self.root / "scheduled", self.fixture.env, timeout=timeout)

    def hanging_child(self, argv, cwd, env, **kwargs):
        script = (
            "import os,time; from pathlib import Path; "
            "Path('child.pid').write_text(str(os.getpid())); time.sleep(30)"
        )
        return run_process([sys.executable, "-c", script], cwd, env, **kwargs)

    async def test_total_deadline_kills_blocked_child_and_does_not_retry(self):
        activities = self.process_activities(timeout=1)
        with patch(
            "sre_suite.telemetry.temporal_activities.run_process", side_effect=self.hanging_child
        ) as runner:
            async with self.worker(activities.collect):
                result = await asyncio.wait_for((await self.start()).result(), 15)
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(runner.call_count, 1)
        pid = int((self.root / "scheduled" / "child.pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_cancel_active_collection_kills_child(self):
        activities = self.process_activities()
        with patch(
            "sre_suite.telemetry.temporal_activities.run_process", side_effect=self.hanging_child
        ):
            async with self.worker(activities.collect):
                handle = await self.start()
                path = self.root / "scheduled" / "child.pid"
                for _ in range(100):
                    if path.exists():
                        break
                    await asyncio.sleep(0.1)
                self.assertTrue(path.exists())
                await handle.cancel()
                with self.assertRaises(WorkflowFailureError):
                    await asyncio.wait_for(handle.result(), 15)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(path.read_text()), 0)


if __name__ == "__main__":
    unittest.main()

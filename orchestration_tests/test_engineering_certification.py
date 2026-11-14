"""Real engineering -> telemetry -> 120-check child workflow, replay and recovery."""

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

from sre_suite.execution.runtime import execution_key
from sre_suite.execution.temporal_activities import Activities, heartbeat
from sre_suite.execution.temporal_workflow import TicketExecution
from sre_suite.previews import PreviewRegistry
from sre_suite.telemetry.schedule_contracts import TelemetrySchedule
from sre_suite.telemetry.temporal_workflow import TelemetryCollection
from sre_suite.verification.temporal import ServiceCertification
from tests.engineering_certification_fixture import EngineeringCertificationFixture


class EngineeringCertificationEngineTests(unittest.IsolatedAsyncioTestCase):
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
            self.f = EngineeringCertificationFixture(
                self.root, wait=2, clock=lambda: datetime.now(UTC)
            )
        self.addCleanup(self.f.close)
        self.activities = Activities(self.f.runtime)
        self.executor = ThreadPoolExecutor(max_workers=6)
        self.addCleanup(self.executor.shutdown)
        cache = Path(tempfile.gettempdir()) / "sre-temporal-test-bin"
        cache.mkdir(exist_ok=True)
        self.env = await WorkflowEnvironment.start_local(
            download_dest_dir=str(cache), dev_server_log_level="error", ui=False
        )
        self.addAsyncCleanup(self.env.shutdown)
        self.queue = "engineering-" + uuid4().hex
        self.lock = threading.Lock()
        self.collected = False
        self.verifying = False
        self.block = False
        self.lose_ack = False
        self.fail_verifier = False
        self.invalidate = False
        self.attempts = 0
        self.bind_attempts = 0
        self.lose_bind = False

    def parent_worker(self):
        return Worker(
            self.env.client,
            task_queue=self.queue,
            workflows=[TicketExecution],
            activities=[
                self.activities.prepare,
                self.activities.stage,
                self.activities.telemetry,
                self.activities.certification,
                self.bind,
                self.activities.finish,
            ],
            activity_executor=self.executor,
            max_heartbeat_throttle_interval=timedelta(seconds=1),
            default_heartbeat_throttle_interval=timedelta(seconds=1),
        )

    @activity.defn(name="bind_engineering_certification")
    def bind(self, raw):
        self.bind_attempts += 1
        result = self.activities.certification_input(raw)
        if self.lose_bind and self.bind_attempts == 1:
            raise ApplicationError("synthetic lost binding acknowledgement")
        return result

    @activity.defn(name="collect_telemetry")
    def collect(self, raw):
        heartbeat()
        schedule = TelemetrySchedule.parse(raw["schedule"])
        with self.lock:
            if not self.collected:
                self.f.collect(self.plan)
                self.f.runtime.clock = lambda: datetime.now(UTC)
                self.f.v.engine.clock = self.f.runtime.clock
                self.f.f.app._clock = self.f.runtime.clock
                self.collected = True
            for entry, report in zip(self.plan["schedules"], self.f.reports, strict=True):
                if entry["id"] == schedule.workflow_id:
                    return report["collections"][0]
        raise ValueError("unknown schedule")

    @activity.defn(name="verify_certification")
    def verify(self, raw):
        self.attempts += 1
        self.verifying = True
        while self.block:
            heartbeat()
            time.sleep(0.02)
        if self.fail_verifier:
            raise ApplicationError("synthetic certification unavailable", non_retryable=True)
        result = self.f.v.engine.run(raw, publish=True, heartbeat=heartbeat)
        if self.invalidate:
            registry = PreviewRegistry(str(self.root / "evidence.db"))
            try:
                registry.start(
                    replace(self.f.f.preview, scope=replace(self.f.v.scope, generation=2)),
                    1,
                    self.f.runtime.clock(),
                )
            finally:
                registry.close()
        if self.lose_ack and self.attempts == 1:
            raise ApplicationError("synthetic lost certification acknowledgement")
        return result

    def telemetry_worker(self):
        return Worker(
            self.env.client,
            task_queue="synthetic-telemetry",
            workflows=[TelemetryCollection],
            activities=[self.collect],
            activity_executor=self.executor,
        )

    def verifier_worker(self):
        return Worker(
            self.env.client,
            task_queue="synthetic-certification",
            workflows=[ServiceCertification],
            activities=[self.verify],
            activity_executor=self.executor,
            max_heartbeat_throttle_interval=timedelta(seconds=1),
            default_heartbeat_throttle_interval=timedelta(seconds=1),
        )

    async def start(self):
        handle = await self.env.client.start_workflow(
            TicketExecution.run,
            self.f.f.spec,
            id=execution_key(self.f.f.spec),
            task_queue=self.queue,
            execution_timeout=timedelta(minutes=2),
        )
        for _ in range(400):
            self.plan = await handle.query(TicketExecution.telemetry_plan)
            if self.plan is not None:
                break
            await asyncio.sleep(0.01)
        self.assertIsNotNone(self.plan)
        return handle

    async def submit(self, handle):
        for _ in range(600):
            if self.collected:
                await handle.signal(
                    TicketExecution.submit_certification_fixture, self.f.fixture_ref
                )
                return
            await asyncio.sleep(0.01)
        self.fail("telemetry was not collected")

    async def test_all_120_child_certificate_lost_acks_and_history_replay(self):
        self.lose_ack = self.lose_bind = True
        async with self.parent_worker(), self.telemetry_worker(), self.verifier_worker():
            h = await self.start()
            await self.submit(h)
            result = await h.result()
        self.assertEqual(result["outcome"], "ready_for_review")
        self.assertEqual(result["certification"]["verdict"], "PASSED")
        self.assertEqual(self.attempts, 2)
        self.assertEqual(self.bind_attempts, 2)
        await Replayer(workflows=[TicketExecution]).replay_workflow(await h.fetch_history())
        child = self.env.client.get_workflow_handle(result["certification"]["workflow_id"])
        await Replayer(workflows=[ServiceCertification]).replay_workflow(
            await child.fetch_history()
        )

    async def test_fixture_timeout_blocks_without_starting_verifier(self):
        async with self.parent_worker(), self.telemetry_worker(), self.verifier_worker():
            h = await self.start()
            result = await h.result()
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(result["certification"]["verdict"], "INCOMPLETE")
        self.assertEqual(self.attempts, 0)

    async def test_child_failure_blocks_parent_readiness(self):
        self.fail_verifier = True
        async with self.parent_worker(), self.telemetry_worker(), self.verifier_worker():
            h = await self.start()
            await self.submit(h)
            result = await h.result()
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(result["certification"]["verdict"], "INCOMPLETE")

    async def test_parent_restart_preserves_plan_and_fixture(self):
        self.block = True
        async with self.telemetry_worker(), self.verifier_worker():
            async with self.parent_worker():
                h = await self.start()
                await self.submit(h)
                for _ in range(300):
                    if self.verifying:
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(self.verifying)
                plan = await h.query(TicketExecution.certification_plan)
            async with self.parent_worker():
                self.block = False
                result = await h.result()
                self.assertEqual(await h.query(TicketExecution.certification_plan), plan)
        self.assertEqual(result["outcome"], "ready_for_review")
        self.assertEqual(self.attempts, 1)

    async def test_parent_cancel_cancels_active_certification_child(self):
        self.block = True
        async with self.parent_worker(), self.telemetry_worker(), self.verifier_worker():
            h = await self.start()
            await self.submit(h)
            for _ in range(300):
                if self.verifying:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(self.verifying)
            await h.cancel()
            with self.assertRaises(WorkflowFailureError):
                await h.result()
            history = await h.fetch_history()
            ids = [
                e.start_child_workflow_execution_initiated_event_attributes.workflow_id
                for e in history.events
                if e.HasField("start_child_workflow_execution_initiated_event_attributes")
            ]
            child = self.env.client.get_workflow_handle(
                next(i for i in ids if i.startswith("sre-certification-"))
            )
            with self.assertRaises(WorkflowFailureError):
                await child.result()
        self.assertEqual(self.f.f.jira_http.puts, 0)

    async def test_generation_change_after_child_pass_blocks_parent_publication(self):
        self.invalidate = True
        async with self.parent_worker(), self.telemetry_worker(), self.verifier_worker():
            h = await self.start()
            await self.submit(h)
            result = await h.result()
        self.assertEqual(result["outcome"], "blocked")
        self.assertFalse(result["jira_reported"])
        self.assertEqual(self.f.f.jira_http.puts, 0)


class CertificationHandoffCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_plan_export_and_fixture_signal_do_not_start_workflows(self):
        import argparse
        import io
        import json
        from contextlib import redirect_stdout
        from unittest.mock import AsyncMock, MagicMock

        from sre_suite.execution.cli import run
        from sre_suite.execution.wire import canonical

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            f = EngineeringCertificationFixture(root)
            self.addCleanup(f.close)
            f.begin()
            f.collect()
            client = MagicMock()
            handle = client.get_workflow_handle.return_value
            handle.query = AsyncMock(return_value=f.cert_plan)
            handle.signal = AsyncMock()
            output = root / "exported.json"
            evidence = root / "fixture-ref.json"
            evidence.write_text(canonical(f.fixture_ref))
            evidence.chmod(0o600)
            with (
                patch("sre_suite.execution.cli.connect", AsyncMock(return_value=client)),
                redirect_stdout(io.StringIO()),
            ):
                args = argparse.Namespace(
                    command="export-certification-plan",
                    workflow_id=execution_key(f.f.spec),
                    output=str(output),
                )
                await run(args)
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(output.read_text()), f.cert_plan)
                with self.assertRaises(FileExistsError):
                    await run(args)
                await run(
                    argparse.Namespace(
                        command="submit-certification-fixture",
                        workflow_id=execution_key(f.f.spec),
                        evidence=str(evidence),
                    )
                )
                handle.signal.assert_awaited_once_with(
                    "submit_certification_fixture", f.fixture_ref
                )
                client.start_workflow.assert_not_called()
                evidence.chmod(0o644)
                with self.assertRaises(ValueError):
                    await run(
                        argparse.Namespace(
                            command="submit-certification-fixture",
                            workflow_id=execution_key(f.f.spec),
                            evidence=str(evidence),
                        )
                    )

    async def test_signal_first_reference_is_frozen_and_size_bounded(self):
        workflow = TicketExecution()
        workflow.submit_certification_fixture({"oversize": "x" * 9000})
        self.assertIsNone(workflow.fixture)
        workflow.submit_certification_fixture({"id": "first"})
        workflow.submit_certification_fixture({"id": "second"})
        self.assertEqual(workflow.fixture, {"id": "first"})

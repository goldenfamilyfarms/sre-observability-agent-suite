"""Real Temporal certification with authenticated evidence and independent synthetic identities."""

import asyncio
import tempfile
import time
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

from sre_suite.execution.http import RemoteError
from sre_suite.execution.temporal_activities import heartbeat
from sre_suite.previews import PreviewRegistry
from sre_suite.verification.runtime import identity
from sre_suite.verification.temporal import ServiceCertification
from tests.verification_fixture import VerificationFixture


class CertificationWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        now = datetime.now(UTC).replace(microsecond=0)
        with (
            patch("tests.verification_fixture.NOW", now),
            patch("tests.execution_fixture.NOW", now),
            patch("tests.test_api.NOW", now),
            patch("tests.verification_fixture.START", now - timedelta(seconds=120)),
            patch("tests.verification_fixture.END", now - timedelta(seconds=1)),
        ):
            self.f = VerificationFixture(self.root)
        self.addCleanup(self.f.close)
        self.f.engine.clock = lambda: datetime.now(UTC)
        self.f.fixture.app._clock = self.f.engine.clock
        cache = Path(tempfile.gettempdir()) / "sre-temporal-test-bin"
        cache.mkdir(exist_ok=True)
        self.environment = await WorkflowEnvironment.start_local(
            download_dest_dir=str(cache), dev_server_log_level="error", ui=False
        )
        self.addAsyncCleanup(self.environment.shutdown)
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.addCleanup(self.executor.shutdown)
        self.queue = "certification-" + uuid4().hex
        self.attempts = 0
        self.lose_ack = False
        self.invalidate = False
        self.block = False
        self.started = False

    @activity.defn(name="verify_certification")
    def verify(self, raw):
        self.attempts += 1
        self.started = True
        while self.block:
            heartbeat()
            time.sleep(0.02)
        try:
            receipt = self.f.engine.run(raw, publish=True, heartbeat=heartbeat)
        except RemoteError:
            raise ApplicationError("synthetic scope became stale", non_retryable=True) from None
        if self.attempts == 1 and self.lose_ack:
            if self.invalidate:
                from dataclasses import replace

                registry = PreviewRegistry(str(self.root / "evidence.db"))
                try:
                    registry.start(
                        replace(self.f.fixture.preview, scope=replace(self.f.scope, generation=2)),
                        1,
                        self.f.engine.clock(),
                    )
                finally:
                    registry.close()
            raise ApplicationError("synthetic lost acknowledgement")
        return receipt

    def worker(self):
        return Worker(
            self.environment.client,
            task_queue=self.queue,
            workflows=[ServiceCertification],
            activities=[self.verify],
            activity_executor=self.executor,
            max_heartbeat_throttle_interval=timedelta(seconds=1),
            default_heartbeat_throttle_interval=timedelta(seconds=1),
        )

    async def start(self):
        return await self.environment.client.start_workflow(
            ServiceCertification.run,
            self.f.input.wire(),
            id=identity(self.f.input),
            task_queue=self.queue,
            execution_timeout=timedelta(minutes=2),
        )

    async def test_all_120_real_activity_lost_ack_recovery_and_replay(self):
        self.lose_ack = True
        async with self.worker():
            handle = await self.start()
            result = await handle.result()
        self.assertEqual(result["verdict"], "PASSED")
        self.assertEqual(self.attempts, 2)
        self.assertEqual(result["evidence"]["producer"], "verifier")
        await Replayer(workflows=[ServiceCertification]).replay_workflow(
            await handle.fetch_history()
        )

    async def test_generation_change_on_retry_cannot_reuse_passed_certificate(self):
        self.lose_ack = True
        self.invalidate = True
        async with self.worker():
            handle = await self.start()
            result = await handle.result()
        self.assertEqual(result["verdict"], "INCOMPLETE")
        self.assertIsNone(result["evidence"])

    async def test_cancel_active_verification_propagates(self):
        self.block = True
        async with self.worker():
            handle = await self.start()
            for _ in range(300):
                if self.started:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(self.started)
            await handle.cancel()
            with self.assertRaises(WorkflowFailureError):
                await handle.result()
        self.assertEqual(len(list((self.root / "verification-journal").glob("*.json"))), 0)


class CertificationProcessTests(unittest.TestCase):
    def test_real_receipt_boundary_identity_and_total_process_budget(self):
        from sre_suite.execution.process import ExecutionBlocked, ProcessResult
        from sre_suite.execution.wire import canonical
        from sre_suite.verification.temporal import VerificationActivities

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            f = VerificationFixture(root)
            self.addCleanup(f.close)
            policy = root / "policy.json"
            policy.write_text(canonical(f.policy_raw))
            policy.chmod(0o600)
            journal = root / "child-journal"
            journal.mkdir(mode=0o700)
            dispatch = VerificationActivities(
                policy, journal, {"SRE_JIRA_API_TOKEN": "must-not-forward"}
            )
            receipt = f.engine.run(f.input.wire(), publish=True)
            with (
                patch("sre_suite.verification.temporal.heartbeat"),
                patch(
                    "sre_suite.verification.temporal.run_process",
                    return_value=ProcessResult(0, canonical(receipt).encode()),
                ) as run,
            ):
                self.assertEqual(dispatch.verify(f.input.wire()), receipt)
                self.assertEqual(run.call_args.kwargs["timeout"], 300)
                self.assertNotIn("SRE_JIRA_API_TOKEN", run.call_args.args[2])
            with (
                patch("sre_suite.verification.temporal.heartbeat"),
                patch("sre_suite.verification.temporal.run_process", side_effect=ExecutionBlocked),
                self.assertRaises(ApplicationError),
            ):
                dispatch.verify(f.input.wire())

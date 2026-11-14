"""Real Temporal graph/delivery replay, finite correction, cancellation and timeout."""

import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from sre_suite.coordination.temporal import SuiteExecution
from sre_suite.delivery.temporal import ReleaseDelivery
from tests.coordination_fixture import plan, rebind


@workflow.defn(name="TicketExecution")
class StubEngineering:
    @workflow.run
    async def run(self, raw: dict) -> dict:
        return {
            "outcome": "blocked" if raw["attempt"] == 0 else "ready_for_review",
            "jira_reported": True,
        }


class Steps:
    def __init__(self):
        self.calls = []
        self.block = False
        self.ready = True
        self.attempts = []
        self.executing = asyncio.Event()
        self.release = asyncio.Event()
        self.results = {}

    @activity.defn(name="delivery_step")
    async def delivery(self, value: dict) -> dict:
        self.calls.append(value["operation"])
        activity.heartbeat()
        if self.block:
            raise ApplicationError("synthetic policy rejection", non_retryable=True)
        if value["operation"] == "deploy":
            return {"outcome": "sync_requested", "accepted": False}
        if value["operation"] == "cleanup":
            return {"complete": True}
        return {"ready": self.ready, "outcome": "ready_for_verification", "accepted": False}

    @activity.defn(name="suite_prepare")
    async def prepare(self, value: dict) -> dict:
        self.attempts.append((value["node"], value["attempt"]))
        if self.block:
            raise ApplicationError("synthetic revoked approval", non_retryable=True)
        return {
            "input": {
                "policy": "a" * 64,
                "action": "promote",
                "timeout_seconds": 30,
                "attempt": value["attempt"],
            },
            "workflow_id": "test-child-" + uuid4().hex,
        }

    @activity.defn(name="suite_execute")
    async def execute(self, value: dict) -> dict:
        self.executing.set()
        while not self.ready and not self.release.is_set():
            activity.heartbeat()
            await asyncio.sleep(0.02)
        return self.results.get(value["node"], {"passed": True})

    @activity.defn(name="suite_record")
    async def record(self, value: dict) -> dict:
        return {"recorded": value["node"], "attempt": value["attempt"]}


class SuiteTemporalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        cache = Path(tempfile.gettempdir()) / "sre-temporal-test-bin"
        cache.mkdir(exist_ok=True)
        self.env = await WorkflowEnvironment.start_local(
            download_dest_dir=str(cache), dev_server_log_level="error", ui=False
        )
        self.addAsyncCleanup(self.env.shutdown)
        self.queue = "suite-test-" + uuid4().hex
        self.steps = Steps()

    async def run_case(self, cls, request):
        activities = [
            self.steps.delivery,
            self.steps.prepare,
            self.steps.execute,
            self.steps.record,
        ]
        async with Worker(
            self.env.client,
            task_queue=self.queue,
            workflows=[SuiteExecution, ReleaseDelivery, StubEngineering],
            activities=activities,
        ):
            handle = await self.env.client.start_workflow(
                cls.run, request, id="test-" + uuid4().hex, task_queue=self.queue
            )
            result = await asyncio.wait_for(handle.result(), 60)
            history = await handle.fetch_history()
        await Replayer(workflows=[SuiteExecution, ReleaseDelivery]).replay_workflow(history)
        return result

    async def test_delivery_preview_and_cleanup_do_not_imply_certification(self):
        request = {"policy": "a" * 64, "action": "promote", "timeout_seconds": 30}
        result = await self.run_case(ReleaseDelivery, request)
        self.assertTrue(result["ready"])
        self.assertFalse(result["accepted"])
        self.assertEqual(self.steps.calls, ["deploy", "readiness"])
        self.steps.calls.clear()
        result = await self.run_case(ReleaseDelivery, {**request, "action": "cleanup"})
        self.assertTrue(result["complete"])
        self.assertEqual(self.steps.calls, ["cleanup"])

    async def test_delivery_rejection_stops_before_readiness(self):
        self.steps.block = True
        result = await self.run_case(
            ReleaseDelivery, {"policy": "a" * 64, "action": "promote", "timeout_seconds": 30}
        )
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(self.steps.calls, ["deploy"])

    async def test_dependency_dispatch_and_review_checkpoint_replay(self):
        request = plan("analysis", checkpoint=True)
        request["nodes"][0]["task_queue"] = self.queue
        second = {**copy.deepcopy(request["nodes"][0]), "id": "second", "depends_on": ["first"]}
        request["nodes"].append(second)
        result = await self.run_case(SuiteExecution, rebind(request))
        self.assertEqual(result["outcome"], "awaiting_review")
        self.assertEqual(result["remaining"], 1)
        self.assertEqual(self.steps.attempts, [("first", 0)])

    async def test_revoked_dispatch_never_starts_agent(self):
        self.steps.block = True
        request = plan("analysis")
        request["nodes"][0]["task_queue"] = self.queue
        result = await self.run_case(SuiteExecution, rebind(request))
        self.assertEqual(result["outcome"], "blocked")
        self.assertFalse(self.steps.executing.is_set())

    async def test_image_build_and_publish_stop_for_new_digest_approval_and_replay(self):
        request = plan("image-build")
        first = request["nodes"][0]
        first["task_queue"] = self.queue
        request["nodes"].extend(
            [
                {
                    **copy.deepcopy(first),
                    "id": "publish",
                    "kind": "image-publish",
                    "depends_on": ["first"],
                },
                {
                    **copy.deepcopy(first),
                    "id": "after",
                    "kind": "analysis",
                    "depends_on": ["publish"],
                },
            ]
        )
        self.steps.results = {
            "first": {"outcome": "built", "image_digest": "sha256:" + "a" * 64},
            "publish": {"outcome": "attested", "build": {"synthetic": True}},
        }
        result = await self.run_case(SuiteExecution, rebind(request))
        self.assertEqual(result["outcome"], "awaiting_review")
        self.assertEqual(result["remaining"], 1)
        self.assertEqual(self.steps.attempts, [("first", 0), ("publish", 0)])

    async def test_cancellation_propagates_and_does_not_record_success(self):
        self.steps.ready = False
        request = plan("analysis")
        request["nodes"][0]["task_queue"] = self.queue
        async with Worker(
            self.env.client,
            task_queue=self.queue,
            workflows=[SuiteExecution],
            activities=[self.steps.prepare, self.steps.execute, self.steps.record],
        ):
            handle = await self.env.client.start_workflow(
                SuiteExecution.run, rebind(request), id="test-" + uuid4().hex, task_queue=self.queue
            )
            await asyncio.wait_for(self.steps.executing.wait(), 15)
            await handle.cancel()
            with self.assertRaises(WorkflowFailureError):
                await asyncio.wait_for(handle.result(), 15)

    async def test_reviewed_correction_budget_dispatches_distinct_attempts(self):
        request = plan(corrections=True)
        request["nodes"][0]["task_queue"] = self.queue
        result = await self.run_case(SuiteExecution, rebind(request))
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(self.steps.attempts, [("first", 0), ("first", 1)])

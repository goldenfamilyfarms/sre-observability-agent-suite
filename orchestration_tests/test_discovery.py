"""Real durable discovery with installed child workers, HTTPS identities and replay."""

import asyncio
import json
import os
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from sre_suite.discovery.analysis import POLICY_SHA256
from sre_suite.discovery.temporal import ArchitectureAssessmentPlanning, DiscoveryActivities
from sre_suite.execution.temporal_activities import heartbeat
from sre_suite.jira_planning.contracts import Plan
from sre_suite.local_pilot.config import write_json
from sre_suite.previews import PreviewRegistry
from tests.discovery_fixture import DiscoveryFixture
from tests.test_api import SECOND_TOKEN, TOKEN
from tests.test_api import request as api_request


class DiscoveryIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        now = datetime.now(UTC).replace(microsecond=0)
        with (
            patch("tests.discovery_fixture.NOW", now),
            patch("tests.execution_fixture.NOW", now),
            patch("tests.test_api.NOW", now),
        ):
            self.d = DiscoveryFixture(self.root)
        self.addCleanup(self.d.close)
        self.d.f.app._clock = lambda: datetime.now(UTC)
        self.policy = self.root / "request.json"
        write_json(self.policy, self.d.request.wire())
        cache = Path(tempfile.gettempdir()) / "sre-temporal-test-bin"
        cache.mkdir(exist_ok=True)
        self.temporal = await WorkflowEnvironment.start_local(
            download_dest_dir=str(cache), dev_server_log_level="error", ui=False
        )
        self.addAsyncCleanup(self.temporal.shutdown)
        cert, key = self.root / "server.crt", self.root / "server.key"
        conf = self.root / "openssl.cnf"
        conf.write_text(
            "[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
            "[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n"
        )
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-config",
                str(conf),
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )
        cert.chmod(0o600)
        key.chmod(0o600)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                status, value, _ = api_request(
                    owner.d.f.app,
                    self.path,
                    body,
                    token=self.headers.get("Authorization", "").removeprefix("Bearer "),
                )
                raw = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_):
                pass

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(str(cert), str(key))
        self.http.socket = tls.wrap_socket(self.http.socket, server_side=True)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_http)
        self.env = {
            "PATH": os.environ["PATH"],
            "SRE_EVIDENCE_API_URL": f"https://localhost:{self.http.server_port}",
            "SRE_EVIDENCE_API_TOKEN": TOKEN,
            "SRE_EVIDENCE_CA_FILE": str(cert),
            "ANTHROPIC_API_KEY": "MUST_NOT_REACH_SOURCE_CHILD",
            "SRE_JIRA_API_TOKEN": "MUST_NOT_REACH_SOURCE_CHILD",
        }
        self.ia = DiscoveryActivities(
            self.policy, self.root / "intake", "intake", self.env, self.d.f.repo
        )
        self.aa = DiscoveryActivities(
            self.policy,
            self.root / "assessor",
            "assessment",
            {**self.env, "SRE_EVIDENCE_API_TOKEN": SECOND_TOKEN},
        )
        self.assertNotIn("ANTHROPIC_API_KEY", self.ia.env)
        self.assertNotIn("SRE_JIRA_API_TOKEN", self.ia.env)
        self.calls = 0
        self.entered = threading.Event()
        self.stopped = threading.Event()
        self.mode = "retry"
        self.revoked = False

    def close_http(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=5)

    @activity.defn(name="architecture_intake")
    def intake(self, raw):
        self.calls += 1
        if self.mode == "cancel":
            self.entered.set()
            try:
                while True:
                    heartbeat()
                    time.sleep(0.1)
            finally:
                self.stopped.set()
        result = self.ia.intake(raw)
        if self.mode == "revoke":
            registry = PreviewRegistry(str(self.root / "evidence.db"))
            try:
                registry.start(
                    replace(self.d.f.preview, scope=replace(self.d.scope, generation=2)),
                    1,
                    datetime.now(UTC),
                )
                self.revoked = True
            finally:
                registry.close()
        if self.mode == "retry" and self.calls == 1:
            self.first = result
            raise ApplicationError("synthetic lost activity acknowledgement")
        if self.mode == "retry":
            self.assertEqual(result, self.first)
        return result

    async def execute(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            async with (
                Worker(
                    self.temporal.client,
                    task_queue=self.d.request.intake_queue,
                    workflows=[ArchitectureAssessmentPlanning],
                    activities=[self.intake],
                    activity_executor=executor,
                    max_heartbeat_throttle_interval=timedelta(seconds=1),
                ),
                Worker(
                    self.temporal.client,
                    task_queue=self.d.request.assessment_queue,
                    activities=[self.aa.assess, self.aa.plan],
                    activity_executor=executor,
                ),
            ):
                handle = await self.temporal.client.start_workflow(
                    ArchitectureAssessmentPlanning.run,
                    {"request": self.d.request.wire(), "policy_sha256": POLICY_SHA256},
                    id=self.d.request.workflow_id,
                    task_queue=self.d.request.intake_queue,
                    execution_timeout=timedelta(seconds=60),
                )
                if self.mode == "cancel":
                    self.assertTrue(await asyncio.to_thread(self.entered.wait, 15))
                    await handle.cancel()
                    with self.assertRaises(WorkflowFailureError):
                        await handle.result()
                    self.assertTrue(self.stopped.is_set())
                    return None
                result = await handle.result()
        await Replayer(workflows=[ArchitectureAssessmentPlanning]).replay_workflow(
            await handle.fetch_history()
        )
        return result

    async def test_real_agents_retry_replay_and_private_plan_export(self):
        result = await self.execute()
        self.assertEqual(result["outcome"], "plan_proposed")
        self.assertEqual(self.calls, 2)
        plan = Plan.parse(result["plan"])
        self.assertEqual(plan.discovery.request, self.d.request)
        self.assertNotEqual(plan.discovery.intake.producer, plan.discovery.assessment.producer)
        self.assertFalse(result["execution_started"])
        self.assertFalse(result["jira_published"])
        self.assertEqual(plan.fingerprint, result["plan_sha256"])
        output = self.root / "exported-plan"
        with (
            patch(
                "sys.argv",
                [
                    "discovery",
                    "result",
                    "--request",
                    str(self.policy),
                    "--journal",
                    str(self.root / "assessor"),
                    "--output",
                    str(output),
                ],
            ),
            patch.dict(os.environ, self.env, clear=True),
            patch("sre_suite.execution.cli.connect", return_value=self.temporal.client),
        ):
            # Exercise the async export path on the real client's owning loop.
            from argparse import Namespace

            from sre_suite.discovery.__main__ import durable

            exported = await durable(
                Namespace(command="result", journal=self.root / "assessor", output=output),
                self.d.request,
            )
        self.assertTrue(exported["prepared"])
        self.assertEqual(json.loads((output / "plan.json").read_bytes()), result["plan"])
        self.assertEqual((output / "plan.json").stat().st_mode & 0o777, 0o600)

    async def test_new_generation_blocks_assessment_and_plan(self):
        self.mode = "revoke"
        result = await self.execute()
        self.assertTrue(self.revoked)
        self.assertEqual(result["outcome"], "blocked")
        self.assertFalse(result["execution_started"])
        self.assertEqual(list((self.root / "assessor").iterdir()), [])

    async def test_cancellation_waits_for_activity_shutdown(self):
        self.mode = "cancel"
        await self.execute()

    async def test_worker_rejects_policy_changes_and_cross_role_dispatch_before_process(self):
        from sre_suite.execution.process import ExecutionBlocked, ProcessResult

        raw = {"request": self.d.request.wire(), "policy_sha256": POLICY_SHA256}
        with (
            patch("sre_suite.discovery.temporal.heartbeat"),
            patch("sre_suite.discovery.temporal.run_process") as process,
        ):
            for invalid in (
                {**raw, "policy_sha256": "a" * 64},
                {**raw, "request": {**raw["request"], "context_version": 1}},
                {**raw, "unexpected": True},
            ):
                with self.assertRaises(ApplicationError):
                    self.ia.intake(invalid)
            with self.assertRaises(ApplicationError):
                self.aa.intake(raw)
            write_json(self.root / "changed.json", {**raw["request"], "context_version": 1})
            self.ia.policy_file = self.root / "changed.json"
            with self.assertRaises(ApplicationError):
                self.ia.intake(raw)
            process.assert_not_called()
            self.ia.policy_file = self.policy
            for code, retryable in ((75, True), (1, False)):
                process.return_value = ProcessResult(code, b"", b"PRIVATE ERROR")
                with self.assertRaises(ApplicationError) as caught:
                    self.ia.intake(raw)
                self.assertEqual(caught.exception.non_retryable, not retryable)
                self.assertNotIn("PRIVATE", str(caught.exception))
            process.side_effect = ExecutionBlocked("PRIVATE ERROR")
            with self.assertRaises(ApplicationError) as caught:
                self.ia.intake(raw)
            self.assertTrue(caught.exception.non_retryable)
            self.assertNotIn("PRIVATE", str(caught.exception))

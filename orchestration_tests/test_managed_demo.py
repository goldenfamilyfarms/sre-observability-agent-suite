"""Real Temporal, supervised HTTPS target, evidence API and durable OTLP/Loki delivery."""

import asyncio
import json
import os
import socket
import ssl
import subprocess
import tempfile
import threading
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

from sre_suite.api_wire import evidence_from_wire
from sre_suite.demo_fixture.managed_activities import ManagedActivities
from sre_suite.execution.temporal_activities import heartbeat
from sre_suite.fixtures.config import target
from sre_suite.fixtures.contracts import FixtureRequest
from sre_suite.fixtures.temporal import FixtureAssessment
from sre_suite.previews import PreviewRegistry
from tests.fixtures_fixture import RUNNER_TOKEN
from tests.managed_fixture import ManagedFixture
from tests.test_api import request as api_request


class ManagedHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        cache = Path(tempfile.gettempdir()) / "sre-temporal-test-bin"
        cache.mkdir(exist_ok=True)
        self.temporal = await WorkflowEnvironment.start_local(
            download_dest_dir=str(cache), dev_server_log_level="error", ui=False
        )
        self.addAsyncCleanup(self.temporal.shutdown)
        self.cert, self.key = self.root / "server.crt", self.root / "server.key"
        config = self.root / "openssl.cnf"
        config.write_text(
            "[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n"
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
                str(self.key),
                "-out",
                str(self.cert),
                "-config",
                str(config),
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )
        self.cert.chmod(0o600)
        self.key.chmod(0o600)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.ingest = []
        self.drop_trace = False
        self.partial = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path in {"/v1/traces", "/loki/api/v1/push"}:
                    expected = "Bearer traces" if self.path == "/v1/traces" else "Bearer logs"
                    if self.headers.get("Authorization") != expected:
                        self.send_response(401)
                        self.end_headers()
                        return
                    owner.ingest.append((self.path, raw))
                    if self.path == "/v1/traces" and owner.drop_trace:
                        owner.drop_trace = False
                        self.close_connection = True
                        return
                    status = 200 if self.path == "/v1/traces" else 204
                    body = (
                        (b'{"partialSuccess":{"rejectedSpans":"1"}}' if owner.partial else b"{}")
                        if status == 200
                        else b""
                    )
                else:
                    status, value, _ = api_request(
                        owner.f.d.f.f.app,
                        self.path,
                        raw,
                        token=self.headers.get("Authorization", "").removeprefix("Bearer "),
                    )
                    body = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(str(self.cert), str(self.key))
        self.http.socket = tls.wrap_socket(self.http.socket, server_side=True)
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()
        self.addCleanup(self.close_http)
        base = f"https://localhost:{self.http.server_port}"
        export = {
            s: {
                "base_url": base,
                "authorization_env": f"SRE_DEMO_{s.upper()}_AUTH",
                "tenant_env": None,
                "ca_env": "SRE_DEMO_EXPORT_CA_FILE",
            }
            for s in ("logs", "traces")
        }
        now = datetime.now(UTC).replace(microsecond=0)
        with (
            patch("tests.verification_fixture.NOW", now),
            patch("tests.execution_fixture.NOW", now),
            patch("tests.test_api.NOW", now),
            patch("tests.verification_fixture.START", now - timedelta(seconds=120)),
            patch("tests.verification_fixture.END", now - timedelta(seconds=1)),
        ):
            self.f = ManagedFixture(
                self.root,
                certificate=self.cert,
                key=self.key,
                port=self.port,
                export=export,
                startup=10,
            )
        self.addCleanup(self.f.close)
        self.f.env.update(
            PATH=os.environ["PATH"],
            SRE_EVIDENCE_API_URL=base,
            SRE_EVIDENCE_API_TOKEN=RUNNER_TOKEN,
            SRE_EVIDENCE_CA_FILE=str(self.cert),
            SRE_DEMO_EXPORT_CA_FILE=str(self.cert),
        )
        await self.configure_backends()
        start = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=8)
        self.f.request["plan"].update(
            window_start=start.isoformat(), window_end=(start + timedelta(seconds=12)).isoformat()
        )

        def live():
            return datetime.now(UTC)

        self.f.d.f.f.app._clock = self.f.d.runner.clock = self.f.d.assessor.clock = live
        self.f.d.runner.target = target(
            self.f.d.policy,
            {
                "SRE_FIXTURE_TARGET_AUTH": self.f.env["SRE_DEMO_FIXTURE_AUTH"],
                "SRE_FIXTURE_TARGET_CA_FILE": str(self.cert),
            },
        )
        self.managed = ManagedActivities(self.f.path, self.f.state, self.f.env)
        self.executor = ThreadPoolExecutor(max_workers=8)
        self.addCleanup(self.executor.shutdown)
        self.ready_seen = threading.Event()
        self.stopped = threading.Event()
        self.exported = threading.Event()
        self.export_attempts = 0
        self.lose_export_ack = False
        self.invalidate = False

    async def configure_backends(self):
        pass

    def close_http(self):
        self.http.shutdown()
        self.http.server_close()
        self.http_thread.join(timeout=5)

    def worker(self, queue, workflows, activities):
        return Worker(
            self.temporal.client,
            task_queue=queue,
            workflows=workflows,
            activities=activities,
            activity_executor=self.executor,
            max_heartbeat_throttle_interval=timedelta(seconds=1),
            default_heartbeat_throttle_interval=timedelta(seconds=1),
        )

    def workers(self):
        return (
            self.worker(
                self.f.policy.dispatch["task_queue"], [], [self.serve, self.ready, self.export]
            ),
            self.worker(self.f.d.policy.runner_queue, [], [self.execute, self.publish]),
            self.worker(self.f.d.policy.assessor_queue, [FixtureAssessment], [self.assess]),
        )

    @activity.defn(name="serve_demo_target")
    def serve(self, raw):
        try:
            return self.managed.serve(raw)
        finally:
            self.stopped.set()

    @activity.defn(name="ready_demo_target")
    def ready(self, raw):
        result = self.managed.ready(raw)
        self.ready_seen.set()
        return result

    @activity.defn(name="export_demo_telemetry")
    def export(self, raw):
        self.export_attempts += 1
        result = self.managed.export(raw)
        self.exported.set()
        if self.lose_export_ack and self.export_attempts == 2:
            raise ApplicationError("synthetic lost activity acknowledgement")
        return result

    @activity.defn(name="execute_fixture")
    def execute(self, raw):
        result = self.f.d.runner.execute(raw["request"], heartbeat)
        if self.invalidate:
            f = self.f.d.f
            registry = PreviewRegistry(str(self.root / "evidence.db"))
            try:
                registry.start(
                    replace(f.f.preview, scope=replace(f.v.scope, generation=2)),
                    1,
                    datetime.now(UTC),
                )
            finally:
                registry.close()
        return result

    @activity.defn(name="publish_fixture_run")
    def publish(self, raw):
        return self.f.d.runner.publish(raw["request"], raw["reference"], heartbeat)

    @activity.defn(name="assess_fixture")
    def assess(self, raw):
        self.assertTrue(self.exported.is_set())
        return self.f.d.assessor.assess(raw["request"], raw["reference"], heartbeat)

    async def start(self):
        return await self.temporal.client.start_workflow(
            FixtureAssessment.run,
            self.f.request,
            id=FixtureRequest.parse(self.f.request).identity,
            task_queue=self.f.d.policy.assessor_queue,
            execution_timeout=timedelta(minutes=2),
        )

    def assert_stopped(self):
        self.assertTrue(self.stopped.is_set())
        with socket.socket() as sock:
            self.assertNotEqual(sock.connect_ex(("127.0.0.1", self.port)), 0)


class ManagedWorkflowTests(ManagedHarness):
    async def test_concrete_target_export_retries_workflow_restart_and_replay(self):
        self.drop_trace = self.lose_export_ack = True
        managed, runner, assessor = self.workers()
        async with managed, runner:
            async with assessor:
                handle = await self.start()
                for _ in range(1400):
                    if self.export_attempts >= 3:
                        break
                    await asyncio.sleep(0.01)
            resumed = self.worker(
                self.f.d.policy.assessor_queue, [FixtureAssessment], [self.assess]
            )
            async with resumed:
                result = await handle.result()
        self.assertEqual(result["outcome"], "assessed")
        self.assertEqual(self.export_attempts, 3)
        self.assertEqual([p for p, _ in self.ingest].count("/loki/api/v1/push"), 1)
        traces = [r for p, r in self.ingest if p == "/v1/traces"]
        self.assertEqual(len(traces), 2)
        self.assertEqual(traces[0], traces[1])
        doc = json.loads(self.f.d.f.v.assessor.read(evidence_from_wire(result["evidence"])))
        self.assertEqual(len(doc["checks"]), 120)
        self.assert_stopped()
        await Replayer(workflows=[FixtureAssessment]).replay_workflow(await handle.fetch_history())

    async def test_cancellation_acknowledges_owned_listener_cleanup(self):
        managed, runner, assessor = self.workers()
        async with managed, runner, assessor:
            handle = await self.start()
            for _ in range(1000):
                if self.ready_seen.is_set():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(self.ready_seen.is_set())
            await handle.cancel()
            with self.assertRaises(WorkflowFailureError):
                await handle.result()
        self.assert_stopped()
        self.assertEqual(self.ingest, [])

    async def test_generation_replacement_prevents_export_and_stops_target(self):
        self.invalidate = True
        managed, runner, assessor = self.workers()
        async with managed, runner, assessor:
            handle = await self.start()
            result = await handle.result()
        self.assertEqual(result["outcome"], "blocked")
        self.assertFalse(self.exported.is_set())
        self.assertEqual(self.ingest, [])
        self.assert_stopped()

    async def test_partial_export_never_reaches_assessor(self):
        self.partial = True
        managed, runner, assessor = self.workers()
        async with managed, runner, assessor:
            handle = await self.start()
            result = await handle.result()
        self.assertEqual(result["outcome"], "blocked")
        self.assertFalse(self.exported.is_set())
        self.assertEqual(self.export_attempts, 1)
        self.assertIn("/v1/traces", [p for p, _ in self.ingest])
        self.assert_stopped()

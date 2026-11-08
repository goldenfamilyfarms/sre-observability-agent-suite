"""Explicit real local-stack acceptance. Requires Docker, OpenSSL and a Temporal CLI.

No external Jira/GitHub/model calls. A pending test workflow uses an unused queue.
Only this test's labeled volume is removed during cleanup.
"""

import asyncio
import hashlib
import json
import os
import socket
import ssl
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from queue import Queue
from uuid import uuid4

import psycopg
from temporalio.client import Client

from sre_suite.api_wire import evidence_from_wire
from sre_suite.execution.evidence_client import EvidenceClient
from sre_suite.execution.http import JsonHttp, RemoteError
from sre_suite.execution.pilot import setup_pilot
from sre_suite.local_pilot.config import (
    WORKER_KEYS,
    LocalConfig,
    dsn,
    execution_environment,
    initialize,
    token,
    write_json,
)
from sre_suite.local_pilot.lifecycle import command, health, up
from tests.execution_fixture import Fixture

IMAGE = os.environ["SRE_TEST_POSTGRES_IMAGE"]
TEMPORAL = os.environ["SRE_TEST_TEMPORAL_BINARY"]


def ports():
    listeners = [socket.socket() for _ in range(3)]
    try:
        for listener in listeners:
            listener.bind(("127.0.0.1", 0))
        return [listener.getsockname()[1] for listener in listeners]
    finally:
        for listener in listeners:
            listener.close()


class StackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.fixture = Fixture(self.root)
        self.addCleanup(self.fixture.close)
        self.config = LocalConfig(uuid4().hex[:20], IMAGE, TEMPORAL, *ports())
        self.stack = self.root / "stack"
        now = datetime.now(UTC)
        self.manifest = {
            "schema_version": 2,
            "registration": {
                "schema_version": 2,
                "preview": {
                    "repository_id": 1,
                    "pr_number": 1,
                    "scope": asdict(self.fixture.ticket.scope),
                    "requested_at": now.isoformat(),
                    "expires_at": (now + timedelta(hours=1)).isoformat(),
                },
                "expected_generation": 0,
            },
            "ticket_key": "DEMO-1",
            "run_id": "local-startup-test",
            "baseline": {
                "sha256": hashlib.sha256(b"synthetic baseline").hexdigest(),
                "observed_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=50)).isoformat(),
            },
        }
        self.manifest_file = self.root / "manifest.json"
        write_json(self.manifest_file, self.manifest)
        initialize(self.stack, self.manifest_file, self.config)
        self.worker_file = self.root / "worker.json"
        env = {k: v for k, v in self.fixture.env.items() if k in WORKER_KEYS}
        env.update(SRE_CLAUDE_ENABLED="false", SRE_JIRA_WRITES_ENABLED="false")
        write_json(self.worker_file, env)
        self.baseline = self.root / "baseline.bin"
        self.baseline.write_bytes(b"synthetic baseline")
        self.stop = threading.Event()
        self.thread = None
        self.addCleanup(self.cleanup_stack)

    def cleanup_stack(self):
        self.stop_stack()
        name = self.config.name + "-postgres"
        # Ownership must match before deleting even a disposable test volume.
        found = command(["docker", "volume", "ls", "--format", "{{.Name}}"])
        if name in found.splitlines():
            info = json.loads(command(["docker", "volume", "inspect", name]))[0]
            self.assertEqual(info["Labels"]["sre.pilot.id"], self.config.identity)
            command(["docker", "volume", "rm", name])

    def start_stack(self):
        self.stop.clear()
        self.events = Queue()

        def run():
            try:
                up(
                    self.stack,
                    self.config,
                    self.worker_file,
                    notify=self.events.put,
                    stopping=self.stop.is_set,
                )
            except BaseException as error:
                self.events.put(error)

        self.thread = threading.Thread(target=run)
        self.thread.start()
        event = self.events.get(timeout=150)
        if isinstance(event, BaseException):
            raise event
        self.assertEqual(event["status"], "ready")
        self.assertTrue(event["worker_started"])
        self.assertFalse(event["execution_started"])
        self.assertEqual(
            health(self.stack, self.config, worker=True),
            {"postgres": "ready", "temporal": "ready", "https_api": "ready"},
        )

    def stop_stack(self):
        if self.thread is not None:
            self.stop.set()
            self.thread.join(timeout=45)
            self.assertFalse(self.thread.is_alive(), "supervisor must release its children")
            self.thread = None
            if not self.events.empty():
                event = self.events.get_nowait()
                if isinstance(event, BaseException):
                    raise event

    def test_real_start_setup_authenticated_tls_worker_restart_and_persistence(self):
        self.start_stack()
        env = execution_environment(self.stack, self.config, None, setup=True)
        output = self.root / "prepared"
        result = json.loads(
            command(
                [
                    sys.executable,
                    "-m",
                    "sre_suite.local_pilot",
                    "run",
                    "--directory",
                    str(self.stack),
                    "--",
                    "pilot-setup",
                    str(self.manifest_file),
                    "--baseline",
                    str(self.baseline),
                    "--output",
                    str(output),
                ]
            )
        )
        self.assertEqual(result["status"], "prepared")
        stored = evidence_from_wire(
            json.loads((output / "baseline-evidence.json").read_text())["evidence"]
        )
        evidence = EvidenceClient(
            self.config.url,
            token(self.stack, "worker.token"),
            local_ca_file=str(self.stack / "server.crt"),
        )
        evidence.verify(stored)
        with psycopg.connect(dsn(self.stack, self.config)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT version FROM sre_suite.schema_migrations ORDER BY version"
                ).fetchall(),
                [(1,), (2,)],
            )

        async def history(create):
            client = await Client.connect(
                f"127.0.0.1:{self.config.temporal_port}", namespace=self.config.name
            )
            if create:
                await client.start_workflow(
                    "LocalPersistenceProbe",
                    id="local-persistence-probe",
                    task_queue="unused-no-worker",
                    execution_timeout=timedelta(hours=1),
                )
            description = await client.get_workflow_handle("local-persistence-probe").describe()
            return description.run_id

        run_id = asyncio.run(history(True))
        self.stop_stack()
        self.assertTrue((self.stack / "temporal" / "state.db").exists())
        self.start_stack()
        self.assertEqual(asyncio.run(history(False)), run_id)
        self.assertEqual(setup_pilot(env, self.manifest_file, self.baseline, output), result)
        evidence.verify(stored)
        with psycopg.connect(dsn(self.stack, self.config)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM sre_suite.evidence_objects").fetchone()[0], 1
            )
            self.assertEqual(
                db.execute("SELECT count(*) FROM sre_suite.preview_events").fetchone()[0], 1
            )
        # Server reloads credentials on each request. Revocation takes effect without restart.
        credential_file = self.stack / "credentials.json"
        credentials = json.loads(credential_file.read_text())
        credentials["credentials"] = credentials["credentials"][:1]
        credential_file.write_text(json.dumps(credentials))
        with self.assertRaises(RemoteError) as failure:
            evidence.verify(stored)
        self.assertEqual(failure.exception.status, 401)
        # A newer generation remains protected against the old exact-scope grant.
        controller = JsonHttp(
            self.config.url,
            "Bearer " + token(self.stack, "controller.token"),
            local_ca_file=str(self.stack / "server.crt"),
        )
        registration = json.loads(json.dumps(self.manifest["registration"]))
        registration["preview"]["scope"]["generation"] = 2
        registration["expected_generation"] = 1
        with self.assertRaises(RemoteError) as failure:
            controller.call("POST", "/v1/previews/register", registration)
        self.assertEqual(failure.exception.status, 403)

    def test_tls_untrusted_clients_bad_framing_and_plaintext_are_rejected(self):
        self.start_stack()
        address = ("127.0.0.1", self.config.api_port)
        # Only explicitly configured clients trust this certificate.
        with socket.create_connection(address, timeout=5) as raw:
            with self.assertRaises(ssl.SSLCertVerificationError):
                ssl.create_default_context().wrap_socket(raw, server_hostname="localhost")
        context = ssl.create_default_context(cafile=str(self.stack / "server.crt"))
        for headers in (
            b"Content-Length: 2\r\nContent-Length: 2\r\n",
            b"Content-Length: 2\r\nTransfer-Encoding: chunked\r\n",
        ):
            with (
                socket.create_connection(address, timeout=5) as raw,
                context.wrap_socket(raw, server_hostname="localhost") as tls,
            ):
                tls.sendall(
                    b"POST /v1/previews/register HTTP/1.1\r\nHost: localhost\r\n"
                    + headers
                    + b"\r\n{}"
                )
                self.assertIn(b"400", tls.recv(8192).split(b"\r\n")[0])
        with socket.create_connection(address, timeout=5) as raw:
            raw.sendall(b"GET /healthz HTTP/1.0\r\n\r\n")
            try:
                response = raw.recv(8192)
                self.assertNotIn(b"200 OK", response)
            except ConnectionResetError:
                pass
        time.sleep(0.1)
        self.assertEqual(health(self.stack, self.config)["https_api"], "ready")

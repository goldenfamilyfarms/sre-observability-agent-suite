"""Exercise the WSGI boundary with real PostgreSQL and an isolated HTTPS listener."""

import base64
import http.client
import io
import json
import os
import ssl
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path
from queue import Queue
from threading import Event, Thread
from unittest.mock import patch
from wsgiref.simple_server import WSGIRequestHandler, make_server

from test_postgres import DSN, DatabaseFixture

from sre_suite.api import EvidenceContextApi
from sre_suite.api_storage import _Evidence, postgres_backend
from sre_suite.auth import TokenAuthenticator
from sre_suite.demo import example_ticket
from sre_suite.memory import Finding, MemoryKind
from sre_suite.postgres import (
    PostgresDatabase,
    PostgresEvidenceStore,
    PostgresKnowledgeStore,
    PostgresPreviewRegistry,
    migrate,
)
from sre_suite.previews import PreviewRequest
from tests.test_api import AUDIENCE, TOKEN, credential, request
from tests.test_foundation import NOW


class PostgresApiTests(DatabaseFixture):
    def setUp(self):
        super().setUp()
        migrate(DSN, self.schema)
        self.auth = TokenAuthenticator(AUDIENCE, (credential(self.scope),))
        self.app = EvidenceContextApi(
            self.auth, postgres_backend(DSN, self.schema), clock=lambda: NOW
        )
        self.registry = self.keep(PostgresPreviewRegistry(DSN, self.schema))
        self.preview = PreviewRequest(1, 1, self.scope, NOW, NOW + timedelta(hours=1))
        self.registry.start(self.preview, 0, NOW)
        evidence = asdict(example_ticket(NOW).evidence[0])
        evidence.pop("producer")
        evidence["id"] = "sample"
        self.upload = {
            "schema_version": 2,
            "evidence": evidence,
            "content_base64": base64.b64encode(b"synthetic baseline").decode(),
        }
        self.context = {"schema_version": 2, "scope": asdict(self.scope), "expected_version": None}

    def test_upload_read_context_and_reopen_through_authenticated_api(self):
        status, payload, _ = request(self.app, "/v1/evidence", self.upload)
        self.assertEqual(status, 200, payload)
        from sre_suite.api_wire import evidence_from_wire

        evidence = evidence_from_wire(payload["evidence"])
        store = self.keep(PostgresEvidenceStore(DSN, self.schema))
        knowledge = self.keep(PostgresKnowledgeStore(DSN, store, schema=self.schema))
        knowledge.propose_finding(
            Finding("fact", self.scope, MemoryKind.GRAPH, "service", "CALLS", "db", evidence)
        )
        knowledge.reconcile_finding("fact", 0, NOW)
        reopened = EvidenceContextApi(
            self.auth, postgres_backend(DSN, self.schema), clock=lambda: NOW
        )
        status, context, _ = request(reopened, "/v1/context", self.context)
        self.assertEqual(status, 200, context)
        self.assertEqual(context["version"], 1)
        self.assertEqual(context["findings"][0]["evidence"]["producer"], "worker-one")
        self.assertEqual(request(reopened, "/v1/evidence/read", payload)[0], 200)
        self.registry.start(replace(self.preview, scope=replace(self.scope, generation=2)), 1, NOW)
        self.assertEqual(request(reopened, "/v1/context", self.context)[0], 409)
        self.assertEqual(request(reopened, "/v1/evidence/read", payload)[0], 409)

    def test_concurrent_same_upload_is_idempotent(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(lambda _: request(self.app, "/v1/evidence", self.upload), range(2))
            )
        self.assertEqual([item[0] for item in results], [200, 200])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(
            self.connection().execute("SELECT count(*) FROM evidence_objects").fetchone()[0], 1
        )

    def test_generation_change_waits_for_upload_transaction_then_blocks_stale_access(self):
        entered, release = Event(), Event()
        pid_queue = Queue()
        original = _Evidence.put

        def pause(store, *args):
            original(store, *args)
            entered.set()
            if not release.wait(timeout=10):
                raise RuntimeError("test barrier timed out")

        def advance():
            registry = PostgresPreviewRegistry(DSN, self.schema)
            try:
                pid_queue.put(registry._db.connection.info.backend_pid)
                registry.start(
                    replace(self.preview, scope=replace(self.scope, generation=2)), 1, NOW
                )
            finally:
                registry.close()

        db = self.connection()
        with patch.object(_Evidence, "put", pause), ThreadPoolExecutor(max_workers=2) as pool:
            upload = pool.submit(request, self.app, "/v1/evidence", self.upload)
            try:
                self.assertTrue(entered.wait(timeout=5))
                update = pool.submit(advance)
                pid = pid_queue.get(timeout=5)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    waiting = db.execute(
                        "SELECT count(*) FROM pg_locks WHERE pid=%s "
                        "AND locktype='advisory' AND NOT granted",
                        (pid,),
                    ).fetchone()[0]
                    if waiting:
                        break
                    time.sleep(0.01)
                self.assertTrue(waiting, "generation writer must wait on the API transaction lock")
                self.assertFalse(update.done())
            finally:
                release.set()
            self.assertEqual(upload.result(timeout=5)[0], 200)
            update.result(timeout=5)
        self.assertEqual(request(self.app, "/v1/evidence", self.upload)[0], 409)
        self.assertEqual(request(self.app, "/v1/context", self.context)[0], 409)

    def test_failure_and_revocation_after_insert_leave_no_evidence(self):
        original = _Evidence.put

        def failure(store, *args):
            original(store, *args)
            raise RuntimeError("private backend diagnostic")

        with patch.object(_Evidence, "put", failure):
            self.assertEqual(
                request(self.app, "/v1/evidence", self.upload)[:2],
                (503, {"error": "service_unavailable"}),
            )
        self.assertEqual(
            self.connection().execute("SELECT count(*) FROM evidence_objects").fetchone()[0], 0
        )

        def revoke(store, *args):
            original(store, *args)
            self.auth.replace_credentials(())

        with patch.object(_Evidence, "put", revoke):
            self.assertEqual(request(self.app, "/v1/evidence", self.upload)[0], 401)
        self.assertEqual(
            self.connection().execute("SELECT count(*) FROM evidence_objects").fetchone()[0], 0
        )

    def test_request_process_exit_rolls_back_uploaded_evidence(self):
        script = """
import os
from sre_suite.api import EvidenceContextApi
from sre_suite.api_storage import _Evidence, postgres_backend
from sre_suite.auth import TokenAuthenticator
from tests.test_api import AUDIENCE, credential, request
from tests.test_foundation import NOW
from sre_suite.demo import example_ticket
import json
original = _Evidence.put
def crash(store, *args):
    original(store, *args)
    os._exit(24)
_Evidence.put = crash
auth = TokenAuthenticator(AUDIENCE, (credential(example_ticket(NOW).scope),))
app = EvidenceContextApi(auth, postgres_backend(
    os.environ['SRE_TEST_POSTGRES_DSN'], os.environ['SRE_TEST_SCHEMA']), clock=lambda: NOW)
request(app, '/v1/evidence', json.loads(os.environ['SRE_TEST_UPLOAD']))
"""
        result = subprocess.run(
            [os.sys.executable, "-c", script],
            env={
                **os.environ,
                "SRE_TEST_SCHEMA": self.schema,
                "SRE_TEST_UPLOAD": json.dumps(self.upload, default=str),
            },
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 24)
        self.assertEqual(
            self.connection().execute("SELECT count(*) FROM evidence_objects").fetchone()[0], 0
        )

    def test_nested_postgres_savepoint_failure_and_outer_rollback(self):
        database = self.keep(PostgresDatabase(DSN, self.schema))
        with self.assertRaises(RuntimeError), database.transaction(write=True):
            database.execute("UPDATE metadata SET version=1")
            with self.assertRaises(ValueError), database.transaction(write=True):
                database.execute("UPDATE metadata SET version=2")
                raise ValueError("inner failure")
            self.assertEqual(database.execute("SELECT version FROM metadata").fetchone()[0], 1)
            raise RuntimeError("outer failure")
        self.assertEqual(database.execute("SELECT version FROM metadata").fetchone()[0], 0)
        with (
            database.transaction(write=False),
            self.assertRaises(RuntimeError),
            database.transaction(write=True),
        ):
            pass

    def test_real_https_request_authentication_and_upload(self):
        # Certificate/key are generated only in a disposable test directory.
        # wsgiref is a test host, not a proposed production server.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "openssl.cnf"
            config.write_text(
                "[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost\n"
            )
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-keyout",
                    str(root / "key.pem"),
                    "-out",
                    str(root / "cert.pem"),
                    "-days",
                    "1",
                    "-config",
                    str(config),
                ],
                check=True,
                capture_output=True,
                timeout=20,
            )
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(root / "cert.pem", root / "key.pem")

            class Handler(WSGIRequestHandler):
                def get_environ(self):
                    environ = super().get_environ()
                    environ["HTTPS"] = "on"  # Set by this TLS-wrapped server, never by a header.
                    return environ

                def log_message(self, *_):
                    pass

            with make_server("127.0.0.1", 0, self.app, handler_class=Handler) as server:
                server.socket = context.wrap_socket(server.socket, server_side=True)
                server.base_environ["wsgi.errors"] = io.StringIO()
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    client_context = ssl.create_default_context(cafile=str(root / "cert.pem"))
                    client = http.client.HTTPSConnection(
                        "localhost", server.server_port, context=client_context, timeout=5
                    )
                    try:
                        client.request(
                            "POST",
                            "/v1/context",
                            json.dumps(self.context),
                            {"Content-Type": "application/json"},
                        )
                        response = client.getresponse()
                        self.assertEqual(response.status, 401)
                        response.read()
                        client.request(
                            "POST",
                            "/v1/evidence",
                            json.dumps(self.upload, default=str),
                            {
                                "Content-Type": "application/json",
                                "Authorization": "Bearer " + TOKEN,
                            },
                        )
                        response = client.getresponse()
                        self.assertEqual(response.status, 200)
                        self.assertEqual(
                            json.loads(response.read())["evidence"]["producer"], "worker-one"
                        )
                    finally:
                        client.close()
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
                    self.assertFalse(thread.is_alive())

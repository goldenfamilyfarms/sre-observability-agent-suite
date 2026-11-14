import base64
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from wsgiref.util import setup_testing_defaults
from wsgiref.validate import validator

from sre_suite.api import EvidenceContextApi, evidence_id
from sre_suite.api_storage import _Evidence, sqlite_backend
from sre_suite.auth import (
    WORKER_ACTIONS,
    Action,
    Credential,
    Forbidden,
    Grant,
    TokenAuthenticator,
    Unauthenticated,
    token_digest,
)
from sre_suite.database import SQLiteDatabase
from sre_suite.demo import example_ticket
from sre_suite.evidence import EvidenceStore
from sre_suite.memory import Finding, KnowledgeStore, MemoryKind
from sre_suite.previews import PreviewRegistry, PreviewRequest
from tests.test_foundation import NOW

# Deliberately synthetic, never accepted by a deployed service.
TOKEN = "synthetic-test-token-" + "x" * 32
SECOND_TOKEN = "synthetic-test-token-" + "y" * 32
AUDIENCE = "sre-evidence-test"


def credential(scope, actions=WORKER_ACTIONS, token=TOKEN, subject="worker-one"):
    return Credential(
        subject,
        token_digest(token),
        AUDIENCE,
        NOW - timedelta(minutes=1),
        NOW + timedelta(hours=1),
        frozenset(Grant(scope, action) for action in actions),
    )


def request(app, path, payload, *, token=TOKEN, body=None, validate=True, **overrides):
    raw = json.dumps(payload, default=str).encode() if body is None else body
    environ = {}
    setup_testing_defaults(environ)
    environ.update(
        {
            "wsgi.url_scheme": "https",
            "REQUEST_METHOD": "POST",
            "PATH_INFO": path,
            "QUERY_STRING": "",
            "CONTENT_TYPE": "application/json",
            "CONTENT_LENGTH": str(len(raw)),
            "HTTP_AUTHORIZATION": "Bearer " + token,
            "wsgi.input": io.BytesIO(raw),
        }
    )
    environ.update(overrides)
    output = {}

    def respond(status, headers, exc_info=None):
        output.update(status=int(status.split()[0]), headers=dict(headers))
        return lambda _: None

    response = (validator(app) if validate else app)(environ, respond)
    try:
        result = b"".join(response)
    finally:
        getattr(response, "close", lambda: None)()
    return output["status"], json.loads(result), output["headers"]


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.scope = example_ticket(NOW).scope
        self.credential = credential(self.scope)
        self.auth = TokenAuthenticator(AUDIENCE, (self.credential,))

    def test_authentication_expiry_not_before_audience_and_revocation(self):
        self.assertEqual(self.auth.authenticate("Bearer " + TOKEN, NOW).subject, "worker-one")
        for header, time in (
            ("Basic " + TOKEN, NOW),
            ("Bearer " + SECOND_TOKEN, NOW),
            ("Bearer " + TOKEN, NOW - timedelta(hours=1)),
            ("Bearer " + TOKEN, NOW + timedelta(hours=1)),
            ("Bearer " + TOKEN + "," + TOKEN, NOW),
        ):
            with self.assertRaises(Unauthenticated):
                self.auth.authenticate(header, time)
        self.auth.replace_credentials((replace(self.credential, audience="other-service"),))
        with self.assertRaises(Unauthenticated):
            self.auth.authenticate("Bearer " + TOKEN, NOW)
        self.auth.replace_credentials(())
        with self.assertRaises(Unauthenticated):
            self.auth.authenticate("Bearer " + TOKEN, NOW)

    def test_grants_do_not_wildcard_any_scope_field_or_action(self):
        read_only = credential(self.scope, (Action.CONTEXT_READ,))
        for scope in (
            replace(self.scope, service="other"),
            replace(self.scope, revision="b" * 40),
            replace(self.scope, environment_id="preview-r1-pr2"),
            replace(self.scope, generation=2),
        ):
            with self.assertRaises(Forbidden):
                self.auth.authorize(read_only, scope, Action.CONTEXT_READ)
        with self.assertRaises(Forbidden):
            self.auth.authorize(read_only, self.scope, Action.EVIDENCE_WRITE)

    def test_duplicate_tokens_and_invalid_config_are_rejected(self):
        with self.assertRaises(ValueError):
            self.auth.replace_credentials((self.credential, self.credential))
        for changes in (
            {"subject": "*"},
            {"token_sha256": TOKEN},
            {"grants": set()},
            {"expires_at": NOW - timedelta(minutes=1)},
        ):
            with self.assertRaises(ValueError):
                replace(self.credential, **changes)
        for token in ("short", "x" * 129, TOKEN + "\n"):
            with self.assertRaises(ValueError):
                token_digest(token)

    def test_rotation_accepts_new_token_and_removes_old_token(self):
        replacement = credential(self.scope, token=SECOND_TOKEN)
        self.auth.replace_credentials((self.credential, replacement))
        self.assertEqual(
            self.auth.authenticate("Bearer " + SECOND_TOKEN, NOW).subject, "worker-one"
        )
        self.auth.replace_credentials((replacement,))
        with self.assertRaises(Unauthenticated):
            self.auth.authenticate("Bearer " + TOKEN, NOW)


class ApiTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = str(Path(temp.name) / "api.db")
        self.scope = example_ticket(NOW).scope
        self.credential = credential(self.scope)
        self.auth = TokenAuthenticator(AUDIENCE, (self.credential,))
        self.now = NOW
        self.backend = sqlite_backend(self.path)
        self.app = EvidenceContextApi(self.auth, self.backend, clock=lambda: self.now)
        self.registry = PreviewRegistry(self.path)
        self.addCleanup(self.registry.close)
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

    def count(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            return db.execute("SELECT count(*) FROM evidence_objects").fetchone()[0]

    def put(self):
        status, payload, _ = request(self.app, "/v1/evidence", self.upload)
        self.assertEqual(status, 200, payload)
        return payload["evidence"]

    def test_authenticated_upload_read_and_idempotent_retry(self):
        evidence = self.put()
        self.assertEqual(evidence["producer"], "worker-one")
        self.assertEqual(evidence["id"], evidence_id("worker-one", self.scope, "sample"))
        self.assertEqual(self.put(), evidence)
        self.assertEqual(self.count(), 1)
        status, payload, headers = request(
            self.app, "/v1/evidence/read", {"schema_version": 2, "evidence": evidence}
        )
        self.assertEqual(status, 200)
        self.assertEqual(base64.b64decode(payload["content_base64"]), b"synthetic baseline")
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_missing_invalid_and_expired_tokens_never_open_storage_or_read_body(self):
        class NoRead(io.BytesIO):
            def read(self, *_):
                raise AssertionError("unauthenticated body read")

        with patch.object(self.app, "_database", side_effect=AssertionError("database opened")):
            for header in ("", "Bearer bad", "Bearer " + SECOND_TOKEN, "Basic " + TOKEN):
                status, payload, headers = request(
                    self.app,
                    "/v1/context",
                    self.context,
                    HTTP_AUTHORIZATION=header,
                    **{"wsgi.input": NoRead()},
                )
                self.assertEqual(status, 401)
                self.assertIn("WWW-Authenticate", headers)
                self.assertEqual(payload, {"error": "invalid_token"})
            self.now += timedelta(hours=1)
            self.assertEqual(request(self.app, "/v1/context", self.context)[0], 401)

    def test_permission_checks_before_storage_and_no_client_role_override(self):
        self.auth.replace_credentials((credential(self.scope, (Action.CONTEXT_READ,)),))
        with patch.object(self.app, "_database", side_effect=AssertionError("database opened")):
            self.assertEqual(request(self.app, "/v1/evidence", self.upload)[0], 403)
            altered = {**self.context, "role": "admin"}
            self.assertEqual(request(self.app, "/v1/context", altered)[0], 400)
        self.assertEqual(self.count(), 0)

    def test_every_scope_field_is_authorized_before_storage(self):
        for changes in (
            {"generation": 2},
            {"environment_id": "preview-r1-pr2"},
            {"revision": "b" * 40},
            {"service": "other"},
            {"environment": "staging"},
        ):
            payload = {**self.context, "scope": {**self.context["scope"], **changes}}
            with patch.object(self.app, "_database", side_effect=AssertionError("database opened")):
                self.assertEqual(request(self.app, "/v1/context", payload)[0], 403)

    def test_scope_identity_generation_and_strict_wire_types(self):
        for change in (
            {"generation": True},
            {"generation": "1"},
            {"generation": 1.0},
            {"generation": 0},
            {"generation": 2**63},
            {"environment_id": "BAD"},
            {"extra": "ignored?"},
        ):
            payload = {**self.context, "scope": {**self.context["scope"], **change}}
            self.assertEqual(request(self.app, "/v1/context", payload)[0], 400)
        for field in ("environment_id", "generation"):
            scope = dict(self.context["scope"])
            del scope[field]
            self.assertEqual(
                request(self.app, "/v1/context", {**self.context, "scope": scope})[0], 400
            )
        for version in (True, 1, 2.0, "2"):
            self.assertEqual(
                request(self.app, "/v1/context", {**self.context, "schema_version": version})[0],
                400,
            )

    def test_producer_is_server_owned_and_ids_are_principal_scoped(self):
        forged = {**self.upload, "evidence": {**self.upload["evidence"], "producer": "steward"}}
        self.assertEqual(request(self.app, "/v1/evidence", forged)[0], 400)
        first = self.put()
        self.auth.replace_credentials(
            (self.credential, credential(self.scope, token=SECOND_TOKEN, subject="worker-two"))
        )
        status, payload, _ = request(self.app, "/v1/evidence", self.upload, token=SECOND_TOKEN)
        self.assertEqual(status, 200)
        self.assertNotEqual(first["id"], payload["evidence"]["id"])
        self.assertEqual(payload["evidence"]["producer"], "worker-two")
        self.assertEqual(self.count(), 2)

    def test_new_generation_requires_new_grant_and_cannot_read_old_bytes(self):
        evidence = self.put()
        scope = replace(self.scope, generation=2)
        self.registry.start(replace(self.preview, scope=scope), 1, NOW)
        changed_upload = {
            **self.upload,
            "evidence": {**self.upload["evidence"], "scope": asdict(scope)},
        }
        self.assertEqual(request(self.app, "/v1/evidence", changed_upload)[0], 403)
        self.auth.replace_credentials((credential(scope),))
        altered = {**evidence, "scope": asdict(scope)}
        self.assertEqual(
            request(self.app, "/v1/evidence/read", {"schema_version": 2, "evidence": altered})[0],
            404,
        )
        status, payload, _ = request(self.app, "/v1/evidence", changed_upload)
        self.assertEqual(status, 200)
        self.assertNotEqual(payload["evidence"]["id"], evidence["id"])

    def test_open_preview_expiry_is_enforced_independently_of_token_expiry(self):
        self.auth.replace_credentials(
            (replace(self.credential, expires_at=NOW + timedelta(hours=2)),)
        )
        self.now = NOW + timedelta(hours=1)
        self.assertEqual(
            request(self.app, "/v1/context", self.context)[:2],
            (409, {"error": "preview_not_current"}),
        )
        self.assertEqual(self.count(), 0)

    def test_context_expiring_during_read_and_excessive_context_are_rejected(self):
        from sre_suite.api_storage import _Knowledge
        from sre_suite.memory import ContextSnapshot

        evidence = replace(example_ticket(NOW).evidence[0], expires_at=NOW + timedelta(seconds=1))
        fact = Finding("fact", self.scope, MemoryKind.SEMANTIC, "service", "uses", "http", evidence)

        def expired(*_):
            self.now = NOW + timedelta(seconds=2)
            return ContextSnapshot(1, self.scope, (fact,))

        with patch.object(_Knowledge, "get_context_snapshot", expired):
            self.assertEqual(
                request(self.app, "/v1/context", self.context)[:2],
                (409, {"error": "context_changed"}),
            )
        self.now = NOW
        with patch.object(
            _Knowledge,
            "get_context_snapshot",
            return_value=ContextSnapshot(1, self.scope, (fact,) * 201),
        ):
            self.assertEqual(request(self.app, "/v1/context", self.context)[0], 413)

    def test_upload_rejects_digest_mismatch_and_id_reuse_with_changed_metadata(self):
        self.put()
        altered = {**self.upload, "evidence": {**self.upload["evidence"], "uri": "urn:changed"}}
        self.assertEqual(request(self.app, "/v1/evidence", altered)[0], 409)
        altered = {**self.upload, "content_base64": base64.b64encode(b"wrong").decode()}
        self.assertEqual(request(self.app, "/v1/evidence", altered)[0], 409)
        self.assertEqual(self.count(), 1)

    def test_old_generation_upload_read_and_context_denied_even_with_old_grant(self):
        evidence = self.put()
        self.registry.start(replace(self.preview, scope=replace(self.scope, generation=2)), 1, NOW)
        for route, payload in (
            ("/v1/evidence", self.upload),
            ("/v1/evidence/read", {"schema_version": 2, "evidence": evidence}),
            ("/v1/context", self.context),
        ):
            status, error, _ = request(self.app, route, payload)
            self.assertEqual(status, 409)
            self.assertEqual(error, {"error": "preview_not_current"})
        self.assertEqual(self.count(), 1)

    def test_closed_expired_and_missing_previews_are_denied(self):
        self.registry.terminate(self.scope, "closed", NOW)
        self.assertEqual(request(self.app, "/v1/evidence", self.upload)[0], 409)
        other = replace(self.scope, environment_id="preview-r1-pr2")
        self.auth.replace_credentials((credential(other),))
        self.assertEqual(
            request(self.app, "/v1/context", {**self.context, "scope": asdict(other)})[0], 409
        )
        self.auth.replace_credentials(
            (replace(self.credential, expires_at=NOW + timedelta(hours=2)),)
        )
        self.now = NOW + timedelta(hours=1)
        self.assertEqual(request(self.app, "/v1/context", self.context)[0], 409)

    def test_production_is_not_enabled_by_a_service_grant(self):
        from sre_suite.contracts import Environment

        scope = replace(self.scope, environment=Environment.PRODUCTION)
        self.auth.replace_credentials((credential(scope),))
        self.assertEqual(
            request(self.app, "/v1/context", {**self.context, "scope": asdict(scope)})[0], 403
        )

    def test_context_contains_only_verified_exact_scope_findings_and_checks_version(self):
        store = EvidenceStore(self.path)
        knowledge = KnowledgeStore(self.path, store)
        self.addCleanup(store.close)
        self.addCleanup(knowledge.close)
        evidence = example_ticket(NOW).evidence[0]
        store.put(evidence, b"synthetic baseline", NOW)
        fact = Finding(
            "verified", self.scope, MemoryKind.SEMANTIC, "service", "uses", "http", evidence
        )
        knowledge.propose_finding(fact)
        knowledge.reconcile_finding("verified", 0, NOW)
        knowledge.propose_finding(replace(fact, id="proposed"))
        other = replace(self.scope, environment_id="preview-r1-pr2")
        other_evidence = replace(evidence, id="other-evidence", scope=other)
        store.put(other_evidence, b"synthetic baseline", NOW)
        knowledge.propose_finding(replace(fact, id="other", scope=other, evidence=other_evidence))
        knowledge.reconcile_finding("other", 1, NOW)
        status, payload, _ = request(self.app, "/v1/context", self.context)
        self.assertEqual(status, 200)
        self.assertEqual(payload["version"], 2)
        self.assertEqual([item["id"] for item in payload["findings"]], ["verified"])
        self.assertEqual(
            request(self.app, "/v1/context", {**self.context, "expected_version": 1})[0], 409
        )
        self.assertEqual(
            request(self.app, "/v1/context", {**self.context, "expected_version": 2})[0], 200
        )
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE evidence_objects SET content=?", (b"tampered",))
        self.assertEqual(request(self.app, "/v1/context", self.context)[1]["findings"], [])

    def test_read_does_not_disclose_unknown_tampered_or_other_scope_evidence(self):
        evidence = self.put()
        for altered in ({**evidence, "id": "unknown"}, {**evidence, "producer": "forged"}):
            self.assertEqual(
                request(self.app, "/v1/evidence/read", {"schema_version": 2, "evidence": altered})[
                    :2
                ],
                (404, {"error": "evidence_unavailable"}),
            )
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE evidence_objects SET content=?", (b"tampered",))
        self.assertEqual(
            request(self.app, "/v1/evidence/read", {"schema_version": 2, "evidence": evidence})[0],
            404,
        )

    def test_revocation_during_upload_rolls_back_content(self):
        original = _Evidence.put

        def revoke(store, *args):
            original(store, *args)
            self.auth.replace_credentials(())

        with patch.object(_Evidence, "put", revoke):
            self.assertEqual(request(self.app, "/v1/evidence", self.upload)[0], 401)
        self.assertEqual(self.count(), 0)

    def test_expiry_while_waiting_for_database_lock_is_rechecked(self):
        original = self.app._database

        def delayed():
            self.now = NOW + timedelta(hours=1)
            return original()

        with patch.object(self.app, "_database", delayed):
            self.assertEqual(request(self.app, "/v1/evidence", self.upload)[0], 401)
        self.assertEqual(self.count(), 0)

    def test_evidence_expiry_during_upload_rolls_back_content(self):
        self.upload["evidence"]["expires_at"] = NOW + timedelta(seconds=1)
        original = _Evidence.put

        def delay(store, *args):
            original(store, *args)
            self.now = NOW + timedelta(seconds=2)

        with patch.object(_Evidence, "put", delay):
            self.assertEqual(request(self.app, "/v1/evidence", self.upload)[0], 409)
        self.assertEqual(self.count(), 0)

    def test_malformed_duplicate_deep_nonfinite_and_non_utf8_json(self):
        for body in (
            b'{"schema_version":2,"schema_version":2}',
            b"NaN",
            b"\xff",
            b"{",
            b"[" * 1000 + b"]" * 1000,
        ):
            self.assertEqual(request(self.app, "/v1/context", {}, body=body)[0], 400)

    def test_invalid_base64_timestamps_uri_and_content_limit(self):
        for value in ("!", "Zg===", "eA==\n", "A" * 1_398_105):
            self.assertEqual(
                request(self.app, "/v1/evidence", {**self.upload, "content_base64": value})[0], 400
            )
        for changes in (
            {"observed_at": "not-a-date"},
            {"observed_at": "2026-11-07T18:00:00"},
            {"uri": "https://host/path?token=secret"},
            {"uri": "file:///tmp/private"},
        ):
            self.assertEqual(
                request(
                    self.app,
                    "/v1/evidence",
                    {**self.upload, "evidence": {**self.upload["evidence"], **changes}},
                )[0],
                400,
            )

    def test_https_header_method_query_and_body_limits(self):
        for overrides, expected in (
            ({"wsgi.url_scheme": "http", "HTTP_X_FORWARDED_PROTO": "https"}, 400),
            ({"QUERY_STRING": "access_token=" + TOKEN}, 400),
            ({"REQUEST_METHOD": "GET"}, 405),
            ({"CONTENT_TYPE": "text/plain"}, 415),
            ({"HTTP_CONTENT_ENCODING": "gzip"}, 415),
            ({"HTTP_TRANSFER_ENCODING": "chunked"}, 400),
            ({"CONTENT_LENGTH": ""}, 411),
            ({"CONTENT_LENGTH": "5,5"}, 411),
            ({"CONTENT_LENGTH": "1500001"}, 413),
        ):
            self.assertEqual(
                request(self.app, "/v1/context", self.context, validate=False, **overrides)[0],
                expected,
            )
        self.assertEqual(
            request(self.app, "/v1/context", {}, body=b"{}", CONTENT_LENGTH="3")[0], 400
        )
        self.assertEqual(request(self.app, "/v1/admin", {})[0], 404)

    def test_errors_never_return_tokens_dsn_or_backend_exception(self):
        with patch.object(self.app, "_database", side_effect=RuntimeError("private DSN " + TOKEN)):
            status, body, headers = request(self.app, "/v1/context", self.context)
        self.assertEqual(status, 503)
        self.assertEqual(body, {"error": "service_unavailable"})
        self.assertNotIn(TOKEN, json.dumps(body))
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_bounded_context_response_and_expected_version_types(self):
        for value in (True, -1, 1.0, "1", 2**63):
            self.assertEqual(
                request(self.app, "/v1/context", {**self.context, "expected_version": value})[0],
                400,
            )
        with patch("sre_suite.api_wire.MAX_RESPONSE_BYTES", 1):
            self.assertEqual(request(self.app, "/v1/evidence", self.upload)[0], 413)
        self.assertEqual(self.count(), 0)


class NestedTransactionTests(unittest.TestCase):
    def test_sqlite_savepoint_rollback_does_not_commit_outer_work(self):
        db = SQLiteDatabase(sqlite3.connect(":memory:"))
        self.addCleanup(db.close)
        db.execute("CREATE TABLE probe (value INTEGER)")
        with self.assertRaises(RuntimeError), db.transaction(write=True):
            db.execute("INSERT INTO probe VALUES (1)")
            with self.assertRaises(ValueError), db.transaction(write=True):
                db.execute("INSERT INTO probe VALUES (2)")
                raise ValueError("inner failure")
            self.assertEqual(db.execute("SELECT * FROM probe").fetchall(), [(1,)])
            raise RuntimeError("outer failure")
        self.assertEqual(db.execute("SELECT * FROM probe").fetchall(), [])
        with (
            db.transaction(write=False),
            self.assertRaises(RuntimeError),
            db.transaction(write=True),
        ):
            pass

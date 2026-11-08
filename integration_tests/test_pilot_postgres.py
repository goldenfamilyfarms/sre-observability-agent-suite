"""Controller registration uses the same real PostgreSQL generation fence as workers."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import timedelta
from unittest.mock import patch

from test_postgres import DSN, DatabaseFixture

from sre_suite.api import EvidenceContextApi
from sre_suite.api_storage import _Previews, postgres_backend
from sre_suite.auth import Action, TokenAuthenticator
from sre_suite.postgres import PostgresPreviewRegistry, migrate
from sre_suite.previews import PreviewRequest
from tests.test_api import AUDIENCE, SECOND_TOKEN, credential, request
from tests.test_foundation import NOW


class PostgresRegistrationTests(DatabaseFixture):
    def setUp(self):
        super().setUp()
        migrate(DSN, self.schema)
        self.controller = credential(
            self.scope, (Action.PREVIEW_REGISTER,), SECOND_TOKEN, "controller"
        )
        self.auth = TokenAuthenticator(AUDIENCE, (credential(self.scope), self.controller))
        self.app = EvidenceContextApi(
            self.auth,
            postgres_backend(DSN, self.schema),
            clock=lambda: NOW,
            allow_preview_registration=True,
        )
        self.preview = PreviewRequest(1, 1, self.scope, NOW, NOW + timedelta(hours=1))
        self.payload = {
            "schema_version": 2,
            "preview": asdict(self.preview),
            "expected_generation": 0,
        }

    def register(self, payload=None):
        return request(
            self.app, "/v1/previews/register", payload or self.payload, token=SECOND_TOKEN
        )

    def test_concurrent_retry_reopen_and_worker_current_scope(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.register(), range(2)))
        self.assertEqual([r[0] for r in results], [200, 200])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(
            self.connection().execute("SELECT count(*) FROM preview_events").fetchone()[0], 1
        )
        reopened = self.keep(PostgresPreviewRegistry(DSN, self.schema))
        self.assertEqual(reopened.require_current(self.scope, NOW), self.preview)
        self.assertEqual(
            request(
                self.app,
                "/v1/context",
                {
                    "schema_version": 2,
                    "scope": asdict(self.scope),
                    "expected_version": None,
                },
            )[0],
            200,
        )

    def test_competing_revisions_have_one_winner_and_stale_retry_cannot_revert(self):
        competing = replace(self.preview, scope=replace(self.scope, revision="b" * 40))
        alternate = replace(
            self.controller,
            grants=frozenset(
                {
                    *self.controller.grants,
                    *credential(competing.scope, (Action.PREVIEW_REGISTER,)).grants,
                }
            ),
        )
        self.auth.replace_credentials((alternate,))
        payload = {**self.payload, "preview": asdict(competing)}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.register, (self.payload, payload)))
        self.assertEqual(sorted(r[0] for r in results), [200, 409])
        registry = self.keep(PostgresPreviewRegistry(DSN, self.schema))
        winner = self.preview if results[0][0] == 200 else competing
        registry.start(replace(winner, scope=replace(winner.scope, generation=2)), 1, NOW)
        self.assertEqual(self.register()[0], 409)
        self.assertEqual(self.register(payload)[0], 409)

    def test_revocation_and_failure_roll_back_preview_and_audit_event(self):
        original = _Previews.start
        for revoke in (False, True):

            def fail(store, *args, revoke=revoke):
                original(store, *args)
                if revoke:
                    self.auth.replace_credentials(())
                else:
                    raise RuntimeError("private database diagnostic")

            with patch.object(_Previews, "start", fail):
                self.assertEqual(self.register()[0], 401 if revoke else 503)
            for table in ("previews", "preview_events"):
                self.assertEqual(
                    self.connection().execute("SELECT count(*) FROM " + table).fetchone()[0], 0
                )

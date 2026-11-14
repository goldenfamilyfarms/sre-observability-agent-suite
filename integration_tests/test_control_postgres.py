"""Control semantics on real PostgreSQL, including upgrade of the knowledge-state constraints."""

from datetime import timedelta
from unittest.mock import patch

import psycopg
from psycopg import sql
from test_postgres import DSN

from sre_suite.api_storage import postgres_backend
from sre_suite.control.api import ControlApi
from sre_suite.execution.evidence_client import EvidenceClient
from sre_suite.postgres import PostgresPreviewRegistry, migrate, migrations
from tests import test_control
from tests.test_api import TOKEN
from tests.test_foundation import NOW
from tests.verification_fixture import TokenTransport


class PostgresControlTests(test_control.ControlTests):
    def setUp(self):
        super().setUp()
        from uuid import uuid4

        self.schema = "sre_test_" + uuid4().hex
        self.addCleanup(self.drop_schema)
        with patch("sre_suite.postgres.migrations", return_value=migrations()[:2]):
            self.assertEqual(migrate(DSN, self.schema), (1, 2))
        self.assertEqual(migrate(DSN, self.schema), (3,))
        self.app = ControlApi(
            self.auth,
            postgres_backend(DSN, self.schema),
            clock=lambda: self.now,
            policy=self.app.policy,
        )
        registry = PostgresPreviewRegistry(DSN, self.schema)
        self.addCleanup(registry.close)
        registry.start(self.f.preview, 0, NOW)
        self.f.registry.close()
        self.f.registry = registry
        self.api = EvidenceClient(
            "https://evidence.invalid", TOKEN, TokenTransport(self.app, TOKEN)
        )
        self.evidence = self.api.upload(
            "finding-source",
            self.scope,
            b'{"assertions":[{"subject":"service","predicate":"calls","value":"database"}]}',
            NOW,
            NOW + timedelta(minutes=30),
        )

    def drop_schema(self):
        with psycopg.connect(DSN, autocommit=True) as db:
            db.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(self.schema))
            )

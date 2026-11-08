"""Real PostgreSQL tests. Run explicitly against a disposable database; never skip.

Each test owns a random sre_test_ schema. Cleanup drops only that schema.
"""

import json
import os
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg import sql

from sre_suite.context import PreviewCoordinator
from sre_suite.demo import FakeWorker, example_ticket
from sre_suite.memory import Finding, FindingState, MemoryKind
from sre_suite.postgres import (
    Migration,
    PostgresCheckpoints,
    PostgresDatabase,
    PostgresEvidenceStore,
    PostgresKnowledgeStore,
    PostgresPreviewRegistry,
    migrate,
    migrations,
)
from sre_suite.previews import PreviewRequest
from sre_suite.serialization import encode_scope
from tests.test_foundation import NOW

DSN = os.environ.get("SRE_TEST_POSTGRES_DSN")
if not DSN:
    raise RuntimeError("Set SRE_TEST_POSTGRES_DSN to an explicitly disposable PostgreSQL database")


class DatabaseFixture(unittest.TestCase):
    def setUp(self):
        self.schema = "sre_test_" + uuid4().hex
        self.ticket = example_ticket(NOW)
        self.scope = self.ticket.scope
        self.addCleanup(self.drop_schema)

    def drop_schema(self):
        with psycopg.connect(DSN, autocommit=True) as db:
            db.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(self.schema))
            )

    def connection(self):
        db = psycopg.connect(DSN, autocommit=True)
        db.execute("SELECT set_config('search_path', %s, false)", (self.schema + ",pg_catalog",))
        self.addCleanup(db.close)
        return db

    def keep(self, adapter):
        self.addCleanup(adapter.close)
        return adapter

    def race(self, function):
        barrier = Barrier(2)

        def run(index):
            barrier.wait(timeout=5)
            try:
                function(index)
                return "committed"
            except ValueError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            return sorted(pool.map(run, range(2)))


class MigrationTests(DatabaseFixture):
    def test_clean_install_and_idempotent_retry(self):
        self.assertEqual(migrate(DSN, self.schema), (1, 2))
        self.assertEqual(migrate(DSN, self.schema), ())
        db = self.connection()
        rows = db.execute(
            "SELECT version, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        self.assertEqual(rows, [(m.version, m.checksum) for m in migrations()])

    def test_upgrade_preserves_scoped_rows_and_adapter_requires_latest_schema(self):
        with patch("sre_suite.postgres.migrations", return_value=migrations()[:1]):
            self.assertEqual(migrate(DSN, self.schema), (1,))
        db = self.connection()
        db.execute(
            "INSERT INTO checkpoints VALUES ('run', 1, 'ready', %s)", (encode_scope(self.scope),)
        )
        with self.assertRaisesRegex(ValueError, "pending"):
            PostgresCheckpoints(DSN, self.scope, self.schema)
        self.assertEqual(migrate(DSN, self.schema), (2,))
        checkpoint = self.keep(PostgresCheckpoints(DSN, self.scope, self.schema))
        self.assertEqual(checkpoint.load("run"), (1, "ready"))

    def test_failing_migration_rolls_back_ddl_history_and_schema_creation(self):
        broken = Migration(3, "003_broken.sql", "CREATE TABLE rollback_probe(id INT); SELECT 1/0;")
        with patch("sre_suite.postgres.migrations", return_value=migrations() + (broken,)):
            with self.assertRaises(psycopg.errors.DivisionByZero):
                migrate(DSN, self.schema)
        db = self.connection()
        self.assertIsNone(db.execute("SELECT to_regnamespace(%s)", (self.schema,)).fetchone()[0])
        self.assertEqual(migrate(DSN, self.schema), (1, 2))
        with patch("sre_suite.postgres.migrations", return_value=migrations() + (broken,)):
            with self.assertRaises(psycopg.errors.DivisionByZero):
                migrate(DSN, self.schema)
        self.assertIsNone(db.execute("SELECT to_regclass('rollback_probe')").fetchone()[0])
        self.assertEqual(db.execute("SELECT count(*) FROM schema_migrations").fetchone()[0], 2)

    def test_checksum_changes_unknown_versions_and_gaps_are_rejected(self):
        migrate(DSN, self.schema)
        db = self.connection()
        for alteration in (
            "UPDATE schema_migrations SET checksum='changed' WHERE version=1",
            "UPDATE schema_migrations SET version=99 WHERE version=2",
            "DELETE FROM schema_migrations WHERE version=1",
        ):
            with db.transaction(force_rollback=True):
                db.execute(sql.SQL(alteration))
                # Use the same connection so these deliberately uncommitted edits are visible.
                from sre_suite.postgres import _history

                with self.assertRaisesRegex(ValueError, "migration history"):
                    _history(db, migrations())
        db.execute("UPDATE schema_migrations SET checksum='changed' WHERE version=1")
        with self.assertRaises(ValueError):
            migrate(DSN, self.schema)
        with self.assertRaises(ValueError):
            PostgresPreviewRegistry(DSN, self.schema)

    def test_unmanaged_schema_and_unsafe_names_are_rejected(self):
        db = self.connection()
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        with self.assertRaisesRegex(ValueError, "unmanaged"):
            migrate(DSN, self.schema)
        for name in ("public", "pg_catalog", "sre_x;DROP SCHEMA public", "sre_" + "x" * 60):
            with self.assertRaises(ValueError):
                migrate(DSN, name)

    def test_concurrent_migrators_apply_each_version_once(self):
        self.assertEqual(self.race(lambda _: migrate(DSN, self.schema)), ["committed", "committed"])
        self.assertEqual(
            self.connection().execute("SELECT count(*) FROM schema_migrations").fetchone()[0], 2
        )

    def test_invalid_existing_scope_aborts_upgrade_without_rewriting_data(self):
        with patch("sre_suite.postgres.migrations", return_value=migrations()[:1]):
            migrate(DSN, self.schema)
        db = self.connection()
        db.execute("INSERT INTO checkpoints VALUES ('unscoped', 1, 'old', '{}')")
        with self.assertRaises(psycopg.errors.CheckViolation):
            migrate(DSN, self.schema)
        self.assertEqual(db.execute("SELECT scope FROM checkpoints").fetchone()[0], "{}")
        self.assertEqual(db.execute("SELECT count(*) FROM schema_migrations").fetchone()[0], 1)


class PersistenceTests(DatabaseFixture):
    def setUp(self):
        super().setUp()
        migrate(DSN, self.schema)
        self.evidence = self.keep(PostgresEvidenceStore(DSN, self.schema))
        self.evidence.put(self.ticket.evidence[0], b"synthetic baseline", NOW)
        self.knowledge = self.keep(PostgresKnowledgeStore(DSN, self.evidence, schema=self.schema))
        self.registry = self.keep(PostgresPreviewRegistry(DSN, self.schema))
        self.preview = PreviewRequest(1, 1, self.scope, NOW, NOW + timedelta(hours=1))
        self.registry.start(self.preview, 0, NOW)
        self.coordinator = PreviewCoordinator(self.registry, self.knowledge, self.evidence)

    def fact(self, name="fact", value="db"):
        return Finding(
            name, self.scope, MemoryKind.GRAPH, "service", "CALLS", value, self.ticket.evidence[0]
        )

    def test_reopen_preserves_evidence_knowledge_preview_and_checkpoint(self):
        fact = self.fact()
        self.knowledge.propose_finding(fact)
        self.knowledge.reconcile_finding(fact.id, 0, NOW)
        checkpoint = self.keep(PostgresCheckpoints(DSN, self.scope, self.schema))
        checkpoint.save("run", "ready", 0, "ready")
        self.evidence.close()
        self.knowledge.close()
        self.registry.close()
        checkpoint.close()
        evidence = self.keep(PostgresEvidenceStore(DSN, self.schema))
        knowledge = self.keep(PostgresKnowledgeStore(DSN, evidence, schema=self.schema))
        registry = self.keep(PostgresPreviewRegistry(DSN, self.schema))
        checkpoint = self.keep(PostgresCheckpoints(DSN, self.scope, self.schema))
        self.assertTrue(evidence.verify(self.ticket.evidence[0], self.scope, NOW))
        self.assertEqual(knowledge.get_dependency_graph(self.scope, NOW), (fact,))
        self.assertEqual(knowledge.events(), ((1, "fact", "verified"),))
        self.assertEqual(registry.require_current(self.scope, NOW), self.preview)
        self.assertEqual(checkpoint.load("run"), (1, "ready"))

    def test_preview_retries_tombstones_expiry_and_stale_generations(self):
        self.registry.start(self.preview, 0, NOW)
        self.assertEqual(
            self.connection().execute("SELECT count(*) FROM preview_events").fetchone()[0], 1
        )
        self.registry.terminate(self.scope, "closed", NOW)
        self.registry.terminate(self.scope, "retry", NOW)
        with self.assertRaises(ValueError):
            self.registry.start(self.preview, 0, NOW)
        next_request = replace(self.preview, scope=replace(self.scope, generation=2))
        self.registry.start(next_request, 1, NOW)
        for action in (
            lambda: self.registry.require_current(self.scope, NOW),
            lambda: self.registry.terminate(self.scope, "late", NOW),
            lambda: self.registry.start(self.preview, 0, NOW),
            lambda: self.registry.require_current(next_request.scope, NOW + timedelta(hours=1)),
            lambda: self.registry.start(
                replace(next_request, scope=replace(self.scope, generation=4)), 3, NOW
            ),
        ):
            with self.assertRaises(ValueError):
                action()
        self.assertEqual(self.registry.require_current(next_request.scope, NOW), next_request)

    def test_competing_generations_have_exactly_one_winner(self):
        def update(index):
            registry = PostgresPreviewRegistry(DSN, self.schema)
            try:
                scope = replace(self.scope, generation=2, revision=str(index) * 40)
                registry.start(replace(self.preview, scope=scope), 1, NOW)
            finally:
                registry.close()

        self.assertEqual(self.race(update), ["committed", "rejected"])
        self.assertEqual(
            self.connection().execute("SELECT count(*) FROM preview_events").fetchone()[0], 2
        )

    def test_concurrent_first_insert_retries_emit_one_event(self):
        def update(_):
            registry = PostgresPreviewRegistry(DSN, self.schema)
            try:
                scope = replace(self.scope, environment_id="preview-r1-pr2")
                registry.start(replace(self.preview, pr_number=2, scope=scope), 0, NOW)
            finally:
                registry.close()

        self.assertEqual(self.race(update), ["committed", "committed"])
        self.assertEqual(
            self.connection().execute("SELECT count(*) FROM preview_events").fetchone()[0], 2
        )

    def test_process_exit_rolls_back_generation_and_audit_event(self):
        script = """
import os
from dataclasses import replace
from datetime import timedelta
from sre_suite.demo import example_ticket
from sre_suite.postgres import PostgresPreviewRegistry
from sre_suite.previews import PreviewRequest
from tests.test_foundation import NOW
registry = PostgresPreviewRegistry(
    os.environ['SRE_TEST_POSTGRES_DSN'], os.environ['SRE_TEST_SCHEMA']
)
original = registry._event
def crash(*args):
    original(*args)
    os._exit(23)
registry._event = crash
scope = replace(example_ticket(NOW).scope, generation=2)
registry.start(PreviewRequest(1, 1, scope, NOW, NOW+timedelta(hours=1)), 1, NOW)
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            env={**os.environ, "SRE_TEST_SCHEMA": self.schema},
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 23)
        self.assertEqual(self.registry.require_current(self.scope, NOW), self.preview)
        self.assertEqual(
            self.connection().execute("SELECT count(*) FROM preview_events").fetchone()[0], 1
        )

    def test_checkpoint_cas_idempotency_and_scope_isolation(self):
        first = self.keep(PostgresCheckpoints(DSN, self.scope, self.schema))
        self.assertEqual(first.save("run", "start", 0, "ready"), 1)
        self.assertEqual(first.save("run", "next", 1, "running"), 2)
        self.assertEqual(first.save("run", "start", 0, "ready"), 1)
        self.assertEqual(first.load("run"), (2, "running"))
        for scope in (
            replace(self.scope, generation=2),
            replace(self.scope, environment_id="preview-r1-pr2"),
        ):
            other = self.keep(PostgresCheckpoints(DSN, scope, self.schema))
            for action in (
                lambda other=other: other.load("run"),
                lambda other=other: other.save("run", "start", 0, "ready"),
            ):
                with self.assertRaises(ValueError):
                    action()
        with self.assertRaises(ValueError):
            first.save("run", "start", 0, "changed")
        with self.assertRaises(ValueError):
            first.save("run", "stale", 0, "ready")

    def test_concurrent_checkpoint_first_writers_and_retry(self):
        def save(index):
            checkpoint = PostgresCheckpoints(DSN, self.scope, self.schema)
            try:
                checkpoint.save("race", f"op-{index}", 0, "ready")
            finally:
                checkpoint.close()

        self.assertEqual(self.race(save), ["committed", "rejected"])
        db = self.connection()
        self.assertEqual(db.execute("SELECT count(*) FROM operations").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT version FROM checkpoints").fetchone()[0], 1)

    def test_evidence_integrity_immutability_expiry_and_scopes(self):
        evidence = self.ticket.evidence[0]
        self.evidence.put(evidence, b"synthetic baseline", NOW)
        for altered, content in (
            (evidence, b"wrong"),
            (replace(evidence, producer="other"), b"synthetic baseline"),
            (evidence, b"x" * 1048577),
        ):
            with self.assertRaises(ValueError):
                self.evidence.put(altered, content, NOW)
        for scope in (
            replace(self.scope, generation=2),
            replace(self.scope, environment_id="preview-r1-pr2"),
        ):
            self.assertFalse(self.evidence.verify(evidence, scope, NOW))
        self.assertFalse(self.evidence.verify(evidence, self.scope, NOW + timedelta(hours=1)))
        self.knowledge.propose_finding(self.fact())
        self.knowledge.reconcile_finding("fact", 0, NOW)
        self.connection().execute("UPDATE evidence_objects SET content=%s", (b"tampered",))
        self.assertFalse(self.evidence.verify(evidence, self.scope, NOW))
        self.assertFalse(self.knowledge.get_context_snapshot(self.scope, NOW).findings)
        with self.assertRaises(ValueError):
            self.knowledge.reconcile_finding("fact", 1, NOW)

    def test_graph_edges_and_single_valued_conflicts_preserve_existing_semantics(self):
        for index, fact in enumerate((self.fact("one", "db"), self.fact("two", "cache"))):
            self.knowledge.propose_finding(fact)
            self.assertEqual(
                self.knowledge.reconcile_finding(fact.id, index, NOW), FindingState.VERIFIED
            )
        self.assertEqual(len(self.knowledge.get_dependency_graph(self.scope, NOW)), 2)
        single = self.keep(
            PostgresKnowledgeStore(DSN, self.evidence, frozenset({"CALLS"}), schema=self.schema)
        )
        single.propose_finding(self.fact("three", "queue"))
        self.assertEqual(single.reconcile_finding("three", 2, NOW), FindingState.DISPUTED)
        for scope in (
            replace(self.scope, generation=2),
            replace(self.scope, environment_id="preview-r1-pr2"),
        ):
            self.assertFalse(single.get_context_snapshot(scope, NOW).findings)

    def test_concurrent_knowledge_reconciliation_uses_one_global_version(self):
        for index in range(2):
            self.knowledge.propose_finding(self.fact(str(index)))

        def reconcile(index):
            evidence = PostgresEvidenceStore(DSN, self.schema)
            knowledge = PostgresKnowledgeStore(DSN, evidence, schema=self.schema)
            try:
                knowledge.reconcile_finding(str(index), 0, NOW)
            finally:
                knowledge.close()
                evidence.close()

        self.assertEqual(self.race(reconcile), ["committed", "rejected"])
        self.assertEqual(self.knowledge.version, 1)
        self.assertEqual(len(self.knowledge.events()), 1)

    def test_repeatable_read_keeps_version_and_facts_in_one_snapshot(self):
        self.knowledge.propose_finding(self.fact())
        database = self.keep(PostgresDatabase(DSN, self.schema))
        with database.transaction(write=False):
            self.assertEqual(database.execute("SELECT version FROM metadata").fetchone()[0], 0)
            self.knowledge.reconcile_finding("fact", 0, NOW)
            self.assertEqual(
                database.execute("SELECT count(*) FROM findings WHERE state='verified'").fetchone()[
                    0
                ],
                0,
            )
        self.assertEqual(
            database.execute("SELECT count(*) FROM findings WHERE state='verified'").fetchone()[0],
            1,
        )

    def test_coordinator_rejects_changed_generation_and_changed_context(self):
        request = self.coordinator.prepare("run", self.ticket, NOW)
        result = FakeWorker().execute(request)
        self.coordinator.validate_completion(request, result, NOW)
        self.knowledge.propose_finding(self.fact())
        self.knowledge.reconcile_finding("fact", 0, NOW)
        with self.assertRaises(ValueError):
            self.coordinator.validate_completion(request, result, NOW)
        request = self.coordinator.prepare("run-2", self.ticket, NOW)
        self.registry.start(replace(self.preview, scope=replace(self.scope, generation=2)), 1, NOW)
        with self.assertRaises(ValueError):
            self.coordinator.validate_completion(request, FakeWorker().execute(request), NOW)

    def test_database_guards_reject_missing_boolean_and_mismatched_identity(self):
        db = self.connection()
        for generation in (None, True, 0, -1, "1", 1.5, 2**63):
            scope = json.loads(encode_scope(self.scope))
            scope["scope"]["generation"] = generation
            with self.subTest(generation=generation), self.assertRaises(psycopg.Error):
                db.execute(
                    "INSERT INTO checkpoints VALUES ('invalid', 1, 'ready', %s)",
                    (json.dumps(scope),),
                )
        payload = db.execute("SELECT payload FROM previews").fetchone()[0]
        raw = json.loads(payload)
        raw["scope"]["environment_id"] = "preview-r1-pr2"
        with self.assertRaises(psycopg.errors.CheckViolation):
            db.execute("UPDATE previews SET payload=%s", (json.dumps(raw),))
        with self.assertRaises(psycopg.errors.CheckViolation):
            db.execute("INSERT INTO checkpoints VALUES ('missing', 1, 'ready', '{}')")

    def test_audit_failure_rolls_back_checkpoint_and_connection_recovers(self):
        checkpoint = self.keep(PostgresCheckpoints(DSN, self.scope, self.schema))
        original = checkpoint._db.execute

        def interrupted(query, parameters=()):
            result = original(query, parameters)
            if query.startswith("INSERT INTO operations"):
                raise RuntimeError("injected after operation insert")
            return result

        with patch.object(checkpoint._db, "execute", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                checkpoint.save("run", "start", 0, "ready")
        self.assertIsNone(checkpoint.load("run"))
        self.assertEqual(
            self.connection().execute("SELECT count(*) FROM operations").fetchone()[0], 0
        )
        self.assertEqual(checkpoint.save("run", "start", 0, "ready"), 1)

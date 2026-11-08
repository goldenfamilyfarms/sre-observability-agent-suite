import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path

from sre_suite.agents import AgentRequest
from sre_suite.certification import CheckResult, Outcome, certify, load_catalog
from sre_suite.context import PreviewCoordinator
from sre_suite.contracts import Approval, Environment, GateResult, Release, Scope
from sre_suite.demo import FakeWorker, example_ticket
from sre_suite.evidence import EvidenceStore
from sre_suite.memory import Finding, KnowledgeStore, MemoryKind
from sre_suite.policy import PromotionPolicy, promotion_denials
from sre_suite.previews import PreviewRegistry, PreviewRequest, preview_environment_id
from sre_suite.serialization import decode_scope, encode_scope, scope_from_dict
from sre_suite.storage import quarantine_legacy
from sre_suite.workflow import Checkpoints
from tests.test_foundation import NOW


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.scope = example_ticket(NOW).scope

    def test_identity_uses_repository_id_and_pr_number_without_slug_collision(self):
        self.assertEqual(preview_environment_id(1, 1), "preview-r1-pr1")
        self.assertNotEqual(preview_environment_id(1, 2), preview_environment_id(2, 1))
        self.assertLessEqual(len(preview_environment_id(2**63 - 1, 2**63 - 1)), 63)
        for value in (True, 0, -1, 1.2, "1", 2**63):
            with self.subTest(value=value), self.assertRaises(ValueError):
                preview_environment_id(value, 1)

    def test_scope_requires_valid_id_and_strict_positive_generation(self):
        for value in ("", "Upper", "x/y", "-x", "x-", "x" * 64, 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(self.scope, environment_id=value)
        for value in (True, False, 0, -1, 1.0, "1", None, 2**63):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(self.scope, generation=value)
        with self.assertRaises(TypeError):
            Scope("service", Environment.DEVELOPMENT, "a" * 40)

    def test_wire_round_trip_and_unknown_fields_versions_rejected(self):
        self.assertEqual(decode_scope(encode_scope(self.scope)), self.scope)
        self.assertEqual(scope_from_dict(asdict(self.scope)), self.scope)
        raw = json.loads(encode_scope(self.scope))
        for version in (1, True, "2", 3):
            with self.subTest(version=version), self.assertRaises(ValueError):
                decode_scope(json.dumps({**raw, "schema_version": version}))
        for field in ("environment_id", "generation"):
            legacy = dict(raw["scope"])
            del legacy[field]
            with self.assertRaises(ValueError):
                scope_from_dict(legacy)
        with self.assertRaises(ValueError):
            scope_from_dict({**raw["scope"], "extra": "ignored?"})
        with self.assertRaises(ValueError):
            decode_scope(
                encode_scope(self.scope).replace(
                    '"generation": 1', '"generation": 1, "generation": 2'
                )
            )

    def test_ticket_evidence_and_context_cannot_cross_preview_or_generation(self):
        ticket = example_ticket(NOW)
        for other in (
            replace(self.scope, environment_id="preview-r1-pr2"),
            replace(self.scope, generation=2),
        ):
            with self.subTest(scope=other):
                self.assertFalse(ticket.evidence[0].valid_for(other, NOW))
                with self.assertRaises(ValueError):
                    replace(ticket, scope=other)

    def test_preview_request_rejects_production_and_mismatched_identity(self):
        request = PreviewRequest(1, 1, self.scope, NOW, NOW + timedelta(hours=1))
        for scope in (
            replace(self.scope, environment=Environment.PRODUCTION),
            replace(self.scope, environment_id="other-preview"),
        ):
            with self.assertRaises(ValueError):
                replace(request, scope=scope)
        with self.assertRaises(ValueError):
            replace(request, expires_at=NOW)


class LocalFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = str(Path(temp.name) / "foundation.db")
        self.ticket = example_ticket(NOW)
        self.scope = self.ticket.scope
        self.evidence = EvidenceStore(self.path)
        self.addCleanup(self.evidence.close)
        self.evidence.put(self.ticket.evidence[0], b"synthetic baseline", NOW)
        self.knowledge = KnowledgeStore(self.path, self.evidence)
        self.addCleanup(self.knowledge.close)
        self.registry = PreviewRegistry(self.path)
        self.addCleanup(self.registry.close)
        self.preview = PreviewRequest(1, 1, self.scope, NOW, NOW + timedelta(hours=1))
        self.registry.start(self.preview, 0, NOW)
        self.coordinator = PreviewCoordinator(self.registry, self.knowledge, self.evidence)

    def next_generation(self):
        next_request = replace(self.preview, scope=replace(self.scope, generation=2))
        self.registry.start(next_request, 1, NOW)
        return next_request


class PreviewRegistryTests(LocalFixture):
    def test_process_exit_during_generation_update_rolls_back_record_and_event(self):
        script = """
import os, sys
from dataclasses import replace
from sre_suite.previews import PreviewRegistry
from sre_suite.demo import example_ticket
from datetime import datetime, UTC, timedelta
from sre_suite.previews import PreviewRequest
now = datetime(2026, 11, 7, 18, tzinfo=UTC)
registry = PreviewRegistry(sys.argv[1])
request = PreviewRequest(
    1, 1, replace(example_ticket(now).scope, generation=2), now, now+timedelta(hours=1)
)
original = registry._event
def interrupted(*args):
    original(*args)
    os._exit(23)
registry._event = interrupted
registry.start(request, 1, now)
"""
        result = subprocess.run([sys.executable, "-c", script, self.path], check=False, timeout=10)
        self.assertEqual(result.returncode, 23)
        reopened = PreviewRegistry(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.require_current(self.scope, NOW), self.preview)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM preview_events").fetchone()[0], 1)

    def test_current_generation_survives_reopen_and_duplicate_start(self):
        self.registry.start(self.preview, 0, NOW)
        second = PreviewRegistry(self.path)
        self.addCleanup(second.close)
        self.assertEqual(second.require_current(self.scope, NOW), self.preview)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM preview_events").fetchone()[0], 1)

    def test_generation_advance_rejects_late_build_and_stale_close(self):
        next_request = self.next_generation()
        for action in (
            lambda: self.registry.start(self.preview, 0, NOW),
            lambda: self.registry.require_current(self.scope, NOW),
            lambda: self.registry.terminate(self.scope, "late close", NOW),
        ):
            with self.assertRaises(ValueError):
                action()
        self.assertEqual(self.registry.require_current(next_request.scope, NOW), next_request)

    def test_generation_skip_and_same_generation_payload_change_rejected(self):
        for request, expected in (
            (replace(self.preview, scope=replace(self.scope, generation=3)), 1),
            (replace(self.preview, scope=replace(self.scope, generation=3)), 2),
            (replace(self.preview, expires_at=NOW + timedelta(hours=2)), 0),
        ):
            with self.assertRaises(ValueError):
                self.registry.start(request, expected, NOW)

    def test_tombstone_prevents_recreation_until_explicit_next_generation(self):
        self.registry.terminate(self.scope, "PR closed", NOW)
        self.registry.terminate(self.scope, "duplicate event", NOW)
        reopened = PreviewRegistry(self.path)
        self.addCleanup(reopened.close)
        with self.assertRaises(ValueError):
            reopened.start(self.preview, 0, NOW)
        with self.assertRaises(ValueError):
            reopened.require_current(self.scope, NOW)
        next_request = self.next_generation()
        self.assertEqual(reopened.require_current(next_request.scope, NOW), next_request)

    def test_expiry_blocks_execution_but_allows_tombstoning(self):
        expired = NOW + timedelta(hours=1)
        with self.assertRaises(ValueError):
            self.registry.require_current(self.scope, expired)
        with self.assertRaises(ValueError):
            self.registry.start(self.preview, 0, expired)
        self.registry.terminate(self.scope, "TTL expired", expired)

    def test_two_connections_cannot_publish_different_generation_two_requests(self):
        second = PreviewRegistry(self.path)
        self.addCleanup(second.close)
        next_request = self.next_generation()
        competing = replace(next_request, scope=replace(next_request.scope, revision="c" * 40))
        with self.assertRaises(ValueError):
            second.start(competing, 1, NOW)


class EvidenceAndContextTests(LocalFixture):
    def test_two_workers_receive_the_same_verified_context(self):
        finding = Finding(
            "fact",
            self.scope,
            MemoryKind.SEMANTIC,
            "service",
            "uses",
            "http",
            self.ticket.evidence[0],
        )
        self.knowledge.propose_finding(finding)
        self.knowledge.reconcile_finding(finding.id, 0, NOW)
        other_memory = KnowledgeStore(self.path, self.evidence)
        self.addCleanup(other_memory.close)
        other = PreviewCoordinator(self.registry, other_memory, self.evidence)
        first = self.coordinator.prepare("worker-1", self.ticket, NOW)
        second = other.prepare("worker-2", self.ticket, NOW)
        self.assertEqual(first.context, second.context)
        self.assertEqual(first.context.findings, (finding,))

    def test_expired_evidence_blocks_worker_completion(self):
        request = self.coordinator.prepare("run", self.ticket, NOW)
        with self.assertRaises(ValueError):
            self.coordinator.validate_completion(
                request, FakeWorker().execute(request), NOW + timedelta(hours=1)
            )

    def test_evidence_digest_size_metadata_and_scope_are_verified(self):
        evidence = self.ticket.evidence[0]
        self.assertTrue(self.evidence.verify(evidence, self.scope, NOW))
        with self.assertRaises(ValueError):
            self.evidence.put(evidence, b"wrong bytes", NOW)
        with self.assertRaises(ValueError):
            self.evidence.put(evidence, b"x" * (EvidenceStore.MAX_BYTES + 1), NOW)
        with self.assertRaises(ValueError):
            self.evidence.put(replace(evidence, producer="substitute"), b"synthetic baseline", NOW)
        self.assertFalse(self.evidence.verify(evidence, replace(self.scope, generation=2), NOW))
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE evidence_objects SET content=?", (b"tampered",))
        self.assertFalse(self.evidence.verify(evidence, self.scope, NOW))

    def test_missing_or_tampered_evidence_cannot_be_reconciled(self):
        unknown = replace(self.ticket.evidence[0], id="not-stored")
        finding = Finding(
            "missing", self.scope, MemoryKind.SEMANTIC, "service", "uses", "http", unknown
        )
        self.knowledge.propose_finding(finding)
        with self.assertRaises(ValueError):
            self.knowledge.reconcile_finding(finding.id, 0, NOW)
        self.assertEqual(self.knowledge.version, 0)

    def test_verified_facts_are_isolated_and_tampering_invalidates_context(self):
        finding = Finding(
            "fact", self.scope, MemoryKind.GRAPH, "service", "CALLS", "db", self.ticket.evidence[0]
        )
        self.knowledge.propose_finding(finding)
        self.knowledge.reconcile_finding(finding.id, 0, NOW)
        for other in (
            replace(self.scope, generation=2),
            replace(self.scope, environment_id="preview-r1-pr2"),
        ):
            self.assertFalse(self.knowledge.get_context_snapshot(other, NOW).findings)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE evidence_objects SET content=?", (b"tampered",))
        self.assertFalse(self.knowledge.get_context_snapshot(self.scope, NOW).findings)

    def test_worker_completion_requires_current_generation_and_context(self):
        request = self.coordinator.prepare("run", self.ticket, NOW)
        result = FakeWorker().execute(request)
        self.coordinator.validate_completion(request, result, NOW)
        self.next_generation()
        with self.assertRaises(ValueError):
            self.coordinator.validate_completion(request, result, NOW)
        with self.assertRaises(ValueError):
            self.coordinator.prepare("late-run", self.ticket, NOW)

    def test_changed_knowledge_requires_rebuild_before_accepting_result(self):
        request = self.coordinator.prepare("run", self.ticket, NOW)
        finding = Finding(
            "fact",
            self.scope,
            MemoryKind.SEMANTIC,
            "service",
            "uses",
            "http",
            self.ticket.evidence[0],
        )
        self.knowledge.propose_finding(finding)
        self.knowledge.reconcile_finding(finding.id, 0, NOW)
        with self.assertRaises(ValueError):
            self.coordinator.validate_completion(request, FakeWorker().execute(request), NOW)

    def test_unknown_worker_evidence_is_rejected_even_when_digest_is_well_formed(self):
        request = self.coordinator.prepare("run", self.ticket, NOW)
        result = FakeWorker().execute(request)
        fabricated = replace(
            result.evidence[0], id="fabricated", sha256=hashlib.sha256(b"fake").hexdigest()
        )
        with self.assertRaises(ValueError):
            self.coordinator.validate_completion(
                request, replace(result, evidence=(fabricated,)), NOW
            )

    def test_context_for_another_preview_cannot_be_substituted(self):
        request = self.coordinator.prepare("run", self.ticket, NOW)
        with self.assertRaises(ValueError):
            AgentRequest(
                "run",
                self.ticket,
                replace(request.context, scope=replace(self.scope, generation=2)),
            )


class AuthorizationTests(LocalFixture):
    def setUp(self):
        super().setUp()
        self.release = Release(self.scope, "sha256:" + "c" * 64, "sha256:" + "d" * 64, ("ci", "qa"))
        self.gates = tuple(
            GateResult(name, self.release, True, self.ticket.evidence[0])
            for name in self.release.required_gates
        )

    def test_nonproduction_authorization_and_gates_cannot_cross_instances_or_generations(self):
        policy = PromotionPolicy((self.release,))
        self.assertFalse(self.coordinator.promotion_denials(self.release, self.gates, policy, NOW))
        for scope in (
            replace(self.scope, environment_id="preview-r1-pr2"),
            replace(self.scope, generation=2),
        ):
            release = replace(self.release, scope=scope)
            self.assertTrue(promotion_denials(release, self.gates, policy, NOW))
            # Even new authorization cannot make old gate evidence apply to a new scope.
            self.assertTrue(
                promotion_denials(release, self.gates, PromotionPolicy((release,)), NOW)
            )

    def test_expired_closed_and_superseded_previews_cannot_pass_local_promotion_boundary(self):
        policy = PromotionPolicy((self.release,))
        self.registry.terminate(self.scope, "closed", NOW)
        self.assertTrue(self.coordinator.promotion_denials(self.release, self.gates, policy, NOW))
        self.next_generation()
        self.assertTrue(self.coordinator.promotion_denials(self.release, self.gates, policy, NOW))

    def test_production_approval_is_bound_to_instance_and_generation(self):
        production = replace(
            self.release,
            scope=replace(
                self.scope, environment=Environment.PRODUCTION, environment_id="production-primary"
            ),
        )
        approval = Approval("approval", production, "human", NOW, NOW + timedelta(hours=1))
        policy = PromotionPolicy(production_approvers=frozenset({"human"}))
        for scope in (
            replace(production.scope, environment_id="production-secondary"),
            replace(production.scope, generation=2),
        ):
            changed = replace(production, scope=scope)
            gates = tuple(
                GateResult(name, changed, True, replace(self.ticket.evidence[0], scope=scope))
                for name in changed.required_gates
            )
            self.assertTrue(promotion_denials(changed, gates, policy, NOW, approval))

    def test_certification_cannot_reuse_results_from_another_preview_generation(self):
        catalog = load_catalog()
        ready = replace(
            catalog,
            checks=tuple(
                replace(c, query_template="fixture", assertion="true") for c in catalog.checks
            ),
        )
        start = NOW - timedelta(minutes=2)
        results = tuple(
            CheckResult(
                c.id,
                ready.version,
                self.release,
                start,
                NOW,
                Outcome.PASS,
                self.ticket.evidence,
                True,
            )
            for c in ready.checks
        )
        self.assertEqual(certify(ready, self.release, start, NOW, results, NOW).verdict, "PASSED")
        for scope in (
            replace(self.scope, environment_id="preview-r1-pr2"),
            replace(self.scope, generation=2),
        ):
            report = certify(ready, replace(self.release, scope=scope), start, NOW, results, NOW)
            self.assertEqual(report.verdict, "INCOMPLETE")
            self.assertEqual(report.schema_version, "2")


class CheckpointTests(LocalFixture):
    def test_read_write_and_idempotent_replay_are_bound_to_scope(self):
        first = Checkpoints(self.path, self.scope)
        self.addCleanup(first.close)
        first.save("run", "ready", 0, "ready")
        for scope in (
            replace(self.scope, generation=2),
            replace(self.scope, environment_id="preview-r1-pr2"),
        ):
            second = Checkpoints(self.path, scope)
            try:
                with self.assertRaises(ValueError):
                    second.load("run")
                with self.assertRaises(ValueError):
                    second.save("run", "ready", 0, "ready")
                with self.assertRaises(ValueError):
                    second.save("run", "next", 1, "implementing")
            finally:
                second.close()


class MigrationTests(unittest.TestCase):
    def test_unknown_schema_version_is_not_downgraded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "future.db")
            with sqlite3.connect(path) as db:
                db.execute(
                    "CREATE TABLE schema_versions (component TEXT PRIMARY KEY, version INTEGER)"
                )
                db.execute("INSERT INTO schema_versions VALUES ('knowledge', 99)")
            evidence = EvidenceStore(path)
            try:
                with self.assertRaises(ValueError):
                    KnowledgeStore(path, evidence)
                with self.assertRaises(ValueError):
                    quarantine_legacy(path)
            finally:
                evidence.close()

    def test_archive_failure_rolls_back_preceding_table_renames(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "partial.db")
            with sqlite3.connect(path) as db:
                for name in ("metadata", "findings", "events", "checkpoints"):
                    db.execute(f"CREATE TABLE {name} (payload TEXT)")
            with self.assertRaises(ValueError):
                quarantine_legacy(path)
            with sqlite3.connect(path) as db:
                names = {
                    row[0]
                    for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                self.assertEqual(names, {"metadata", "findings", "events", "checkpoints"})

    def test_legacy_data_is_rejected_then_preserved_without_trusting_guessed_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "legacy.db")
            with sqlite3.connect(path) as db:
                db.executescript("""
                    CREATE TABLE metadata (id INTEGER PRIMARY KEY, version INTEGER);
                    INSERT INTO metadata VALUES (1, 1);
                    CREATE TABLE findings (id TEXT PRIMARY KEY, payload TEXT, state TEXT);
                    INSERT INTO findings VALUES ('old', '{"scope":"unscoped"}', 'verified');
                    CREATE TABLE events (version INTEGER PRIMARY KEY, finding_id TEXT, state TEXT);
                    INSERT INTO events VALUES (1, 'old', 'verified');
                    CREATE TABLE checkpoints (
                        run_id TEXT PRIMARY KEY, version INTEGER, payload TEXT);
                    INSERT INTO checkpoints VALUES ('run', 1, 'ready');
                    CREATE TABLE operations (run_id TEXT, operation_id TEXT, payload TEXT,
                        expected_version INTEGER, result_version INTEGER,
                        PRIMARY KEY(run_id,operation_id));
                    INSERT INTO operations VALUES ('run', 'ready', 'ready', 0, 1);
                """)
            evidence = EvidenceStore(path)
            try:
                with self.assertRaises(ValueError):
                    KnowledgeStore(path, evidence)
                with self.assertRaises(ValueError):
                    Checkpoints(path, example_ticket(NOW).scope)
                self.assertEqual(len(quarantine_legacy(path)), 5)
                self.assertEqual(quarantine_legacy(path), ())
                knowledge = KnowledgeStore(path, evidence)
                checkpoints = Checkpoints(path, example_ticket(NOW).scope)
                try:
                    self.assertEqual(knowledge.version, 0)
                    self.assertFalse(
                        knowledge.get_context_snapshot(example_ticket(NOW).scope, NOW).findings
                    )
                    self.assertIsNone(checkpoints.load("run"))
                finally:
                    knowledge.close()
                    checkpoints.close()
                with sqlite3.connect(path) as db:
                    self.assertEqual(
                        db.execute("SELECT payload FROM legacy_v1_findings").fetchone()[0],
                        '{"scope":"unscoped"}',
                    )
                    self.assertEqual(
                        db.execute("SELECT payload FROM legacy_v1_checkpoints").fetchone()[0],
                        "ready",
                    )
            finally:
                evidence.close()

    def test_partial_legacy_schema_rolls_back_without_archiving(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "partial.db")
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE findings (payload TEXT)")
            with self.assertRaises(ValueError):
                quarantine_legacy(path)
            with sqlite3.connect(path) as db:
                names = {
                    row[0]
                    for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                self.assertEqual(names, {"findings"})

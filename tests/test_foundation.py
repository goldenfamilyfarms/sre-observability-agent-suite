import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sre_suite.agents import AgentRequest, validate_result
from sre_suite.contracts import Approval, Environment, GateResult, Release
from sre_suite.demo import FakeWorker, example_ticket, run_demo
from sre_suite.evidence import EvidenceStore
from sre_suite.memory import Finding, FindingState, KnowledgeStore, MemoryKind
from sre_suite.planning import execution_order
from sre_suite.policy import PromotionPolicy, promotion_denials
from sre_suite.workflow import Checkpoints, TicketState, Transition, advance

NOW = datetime(2026, 11, 7, 18, tzinfo=UTC)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.ticket = example_ticket(NOW)

    def test_ticket_requires_complete_execution_contract(self):
        for field in (
            "workstream",
            "objective",
            "current_state",
            "desired_state",
            "rollback_strategy",
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                replace(self.ticket, **{field: " "})
        for field in (
            "implementation_scope",
            "acceptance_criteria",
            "qa_requirements",
            "telemetry_requirements",
            "delivery_requirements",
            "evidence",
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                replace(self.ticket, **{field: ()})

    def test_revision_and_environment_are_explicit(self):
        with self.assertRaises(ValueError):
            replace(self.ticket.scope, revision="main")
        with self.assertRaises(ValueError):
            replace(self.ticket.scope, environment="prod")

    def test_evidence_rejects_credentials_and_naive_time(self):
        evidence = self.ticket.evidence[0]
        for uri in ("https://user:secret@example.test/e", "https://example.test/e?token=x"):
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                replace(evidence, uri=uri)
        with self.assertRaises(ValueError):
            replace(evidence, observed_at=NOW.replace(tzinfo=None))

    def test_evidence_scope_must_match_ticket(self):
        with self.assertRaises(ValueError):
            replace(self.ticket, scope=replace(self.ticket.scope, service="other"))

    def test_deterministic_dependency_order(self):
        dependent = replace(self.ticket, key="DEMO-2", dependencies=("DEMO-1",))
        self.assertEqual(execution_order([dependent, self.ticket]), ("DEMO-1", "DEMO-2"))

    def test_plan_rejects_cycles_missing_and_duplicate_tickets(self):
        dependent = replace(self.ticket, key="DEMO-2", dependencies=("DEMO-1",))
        cycle = replace(self.ticket, dependencies=("DEMO-2",))
        for tickets in ([dependent], [cycle, dependent], [self.ticket, self.ticket]):
            with self.subTest(tickets=tickets), self.assertRaises(ValueError):
                execution_order(tickets)

    def test_self_dependency_rejected(self):
        with self.assertRaises(ValueError):
            replace(self.ticket, dependencies=(self.ticket.key,))


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.ticket = example_ticket(NOW)
        self.release = Release(
            self.ticket.scope, "sha256:" + "c" * 64, "sha256:" + "d" * 64, ("ci", "qa")
        )
        self.evidence = self.ticket.evidence[0]

    def gates(self, release=None):
        release = release or self.release
        return tuple(
            GateResult(name, release, True, replace(self.evidence, scope=release.scope))
            for name in release.required_gates
        )

    def test_default_deny_nonproduction(self):
        self.assertTrue(promotion_denials(self.release, self.gates(), PromotionPolicy(), NOW))

    def test_exact_nonproduction_authorization(self):
        policy = PromotionPolicy((self.release,))
        self.assertEqual(promotion_denials(self.release, self.gates(), policy, NOW), ())
        other = replace(self.release, artifact_digest="sha256:" + "e" * 64)
        self.assertTrue(promotion_denials(other, self.gates(other), policy, NOW))

    def test_missing_failed_duplicate_and_stale_gates_deny(self):
        policy = PromotionPolicy((self.release,))
        gates = self.gates()
        bad_sets = (
            gates[:1],
            gates + gates[:1],
            (replace(gates[0], passed=False), gates[1]),
            (replace(gates[0], evidence=replace(self.evidence, expires_at=NOW)), gates[1]),
        )
        for bad in bad_sets:
            with self.subTest(gates=bad):
                self.assertTrue(promotion_denials(self.release, bad, policy, NOW))

    def test_production_requires_human_even_if_auto_authorized(self):
        release = replace(
            self.release, scope=replace(self.release.scope, environment=Environment.PRODUCTION)
        )
        policy = PromotionPolicy((release,), frozenset({"human-1"}))
        self.assertTrue(promotion_denials(release, self.gates(release), policy, NOW))
        approval = Approval("approval-1", release, "human-1", NOW, NOW + timedelta(hours=1))
        self.assertEqual(promotion_denials(release, self.gates(release), policy, NOW, approval), ())

    def test_approval_is_bound_to_all_release_fields_and_authority(self):
        release = replace(
            self.release, scope=replace(self.release.scope, environment=Environment.PRODUCTION)
        )
        policy = PromotionPolicy(production_approvers=frozenset({"human-1"}))
        approval = Approval("approval-1", release, "human-1", NOW, NOW + timedelta(hours=1))
        invalid = [
            replace(approval, revoked=True),
            replace(approval, approver="worker"),
            replace(approval, issued_at=NOW - timedelta(hours=2), expires_at=NOW),
            replace(approval, issued_at=NOW + timedelta(minutes=1)),
        ]
        for field, value in (
            ("artifact_digest", "sha256:" + "f" * 64),
            ("plan_digest", "sha256:" + "f" * 64),
            ("required_gates", ("ci",)),
            ("scope", replace(release.scope, revision="f" * 40)),
        ):
            invalid.append(replace(approval, release=replace(release, **{field: value})))
        for bad in invalid:
            with self.subTest(approval=bad):
                self.assertTrue(promotion_denials(release, self.gates(release), policy, NOW, bad))


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "memory.db")
        self.evidence_store = EvidenceStore(self.path)
        self.addCleanup(self.evidence_store.close)
        self.store = KnowledgeStore(self.path, self.evidence_store, frozenset({"VERSION"}))
        self.addCleanup(self.store.close)
        self.ticket = example_ticket(NOW)
        self.evidence_store.put(self.ticket.evidence[0], b"synthetic baseline", NOW)
        self.finding = Finding(
            "finding-1",
            self.ticket.scope,
            MemoryKind.GRAPH,
            "demo-service",
            "CALLS",
            "demo-dependency",
            self.ticket.evidence[0],
        )

    def verify(self):
        self.store.propose_finding(self.finding)
        return self.store.reconcile_finding(self.finding.id, 0, NOW)

    def test_proposals_are_not_canonical(self):
        self.store.propose_finding(self.finding)
        self.assertEqual(self.store.get_context_snapshot(self.ticket.scope, NOW).findings, ())

    def test_verified_graph_persists_across_reopen(self):
        self.assertEqual(self.verify(), FindingState.VERIFIED)
        second = KnowledgeStore(self.path, self.evidence_store)
        self.addCleanup(second.close)
        self.assertEqual(second.get_dependency_graph(self.ticket.scope, NOW), (self.finding,))
        self.assertEqual(second.version, 1)
        self.assertEqual(second.events(), ((1, "finding-1", "verified"),))

    def test_conflict_is_disputed_without_overwriting_fact(self):
        finding = replace(
            self.finding,
            kind=MemoryKind.SEMANTIC,
            predicate="VERSION",
            value="1.0",
        )
        self.store.propose_finding(finding)
        self.assertEqual(self.store.reconcile_finding(finding.id, 0, NOW), FindingState.VERIFIED)
        conflict = replace(finding, id="finding-2", value="2.0")
        self.store.propose_finding(conflict)
        self.assertEqual(self.store.reconcile_finding(conflict.id, 1, NOW), FindingState.DISPUTED)
        self.assertEqual(
            self.store.get_context_snapshot(self.ticket.scope, NOW).findings, (finding,)
        )

    def test_graph_allows_multiple_calls_targets(self):
        self.verify()
        second_dependency = replace(self.finding, id="finding-2", value="cache")
        self.store.propose_finding(second_dependency)
        self.assertEqual(
            self.store.reconcile_finding(second_dependency.id, 1, NOW), FindingState.VERIFIED
        )
        self.assertEqual(
            self.store.get_dependency_graph(self.ticket.scope, NOW),
            (self.finding, second_dependency),
        )

    def test_proposal_retry_is_idempotent_but_key_reuse_rejected(self):
        self.verify()
        self.store.propose_finding(self.finding)
        self.assertEqual(
            self.store.reconcile_finding(self.finding.id, 1, NOW), FindingState.VERIFIED
        )
        self.assertEqual(self.store.version, 1)
        with self.assertRaises(ValueError):
            self.store.propose_finding(replace(self.finding, value="other"))

    def test_stale_writer_rejected_across_connections(self):
        second = KnowledgeStore(self.path, self.evidence_store)
        self.addCleanup(second.close)
        self.verify()
        with self.assertRaises(ValueError):
            second.reconcile_finding(self.finding.id, 0, NOW)

    def test_expired_future_and_other_scope_evidence_not_used(self):
        self.verify()
        for scope, now in (
            (self.ticket.scope, NOW + timedelta(hours=2)),
            (self.ticket.scope, NOW - timedelta(hours=2)),
            (replace(self.ticket.scope, revision="e" * 40), NOW),
        ):
            with self.subTest(scope=scope, now=now):
                self.assertFalse(self.store.get_context_snapshot(scope, now).findings)

    def test_expired_proposal_cannot_be_verified(self):
        self.store.propose_finding(self.finding)
        with self.assertRaises(ValueError):
            self.store.reconcile_finding(self.finding.id, 0, NOW + timedelta(hours=2))
        self.assertEqual(self.store.version, 0)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.ticket = example_ticket(NOW)

    def transition(
        self, current, target, role, actor="independent", ticket=None, completed=frozenset()
    ):
        ticket = ticket or self.ticket
        return advance(
            ticket,
            current,
            Transition(target, actor, role, self.ticket.evidence[0], True),
            completed,
            "worker-1",
            NOW,
        )

    def test_full_ticket_path(self):
        state = TicketState.PLANNED
        for target, role in (
            (TicketState.READY, "planner"),
            (TicketState.IMPLEMENTING, "implementer"),
            (TicketState.IN_REVIEW, "implementer"),
            (TicketState.QA_READY, "reviewer"),
            (TicketState.QA_PASSED, "qa"),
            (TicketState.SHIPPING, "shipper"),
            (TicketState.VERIFYING, "shipper"),
            (TicketState.ACCEPTED, "verifier"),
        ):
            state = self.transition(
                state, target, role, "worker-1" if role == "implementer" else "independent"
            )
        self.assertEqual(state, TicketState.ACCEPTED)

    def test_no_skipping_gates(self):
        with self.assertRaises(ValueError):
            self.transition(TicketState.PLANNED, TicketState.ACCEPTED, "verifier")

    def test_failed_or_unspecified_verdict_cannot_advance(self):
        with self.assertRaises(ValueError):
            advance(
                self.ticket,
                TicketState.IN_REVIEW,
                Transition(TicketState.QA_READY, "reviewer", "reviewer", self.ticket.evidence[0]),
                frozenset(),
                "worker-1",
                NOW,
            )

    def test_dependencies_checked_again_at_execution(self):
        ticket = replace(self.ticket, dependencies=("DEMO-2",))
        for current, target, role in (
            (TicketState.PLANNED, TicketState.READY, "planner"),
            (TicketState.READY, TicketState.IMPLEMENTING, "implementer"),
        ):
            with self.assertRaises(ValueError):
                self.transition(current, target, role, "worker-1", ticket)

    def test_implementation_cannot_approve_review_qa_or_certification(self):
        for current, target, role in (
            (TicketState.IN_REVIEW, TicketState.QA_READY, "reviewer"),
            (TicketState.QA_READY, TicketState.QA_PASSED, "qa"),
            (TicketState.VERIFYING, TicketState.ACCEPTED, "verifier"),
        ):
            with self.subTest(role=role), self.assertRaises(ValueError):
                self.transition(current, target, role, "worker-1")

    def test_terminal_and_blocked_states_do_not_resume_implicitly(self):
        for state in (TicketState.ACCEPTED, TicketState.CANCELED, TicketState.BLOCKED):
            with self.subTest(state=state), self.assertRaises(ValueError):
                self.transition(state, TicketState.READY, "planner")

    def test_stale_transition_evidence_rejected(self):
        with self.assertRaises(ValueError):
            advance(
                self.ticket,
                TicketState.PLANNED,
                Transition(TicketState.READY, "planner", "planner", self.ticket.evidence[0]),
                frozenset(),
                "worker-1",
                NOW + timedelta(hours=2),
            )

    def test_checkpoint_restart_replay_and_compare_and_swap(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "checkpoints.db")
            first = Checkpoints(path, self.ticket.scope)
            self.assertEqual(first.save("run", "ready", 0, "ready"), 1)
            first.close()
            second = Checkpoints(path, self.ticket.scope)
            try:
                self.assertEqual(second.load("run"), (1, "ready"))
                self.assertEqual(second.save("run", "ready", 0, "ready"), 1)
                self.assertEqual(second.save("run", "implement", 1, "implementing"), 2)
                self.assertEqual(second.save("run", "ready", 0, "ready"), 1)
                self.assertEqual(second.load("run"), (2, "implementing"))
                with self.assertRaises(ValueError):
                    second.save("run", "ready", 0, "changed")
                with self.assertRaises(ValueError):
                    second.save("run", "review", 1, "in_review")
            finally:
                second.close()

    def test_worker_result_binding_and_retry_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = EvidenceStore(str(Path(directory) / "memory.db"))
            memory = KnowledgeStore(str(Path(directory) / "memory.db"), evidence)
            try:
                request = AgentRequest(
                    "run", self.ticket, memory.get_context_snapshot(self.ticket.scope, NOW)
                )
                result = FakeWorker().execute(request)
                validate_result(request, result)
                for bad in (
                    replace(result, execution_id="other"),
                    replace(result, context_version=100),
                    replace(result, evidence=()),
                ):
                    with self.assertRaises(ValueError):
                        validate_result(request, bad)
                with self.assertRaises(ValueError):
                    replace(request, max_attempts=1000)
            finally:
                memory.close()
                evidence.close()

    def test_local_vertical_slice(self):
        result = run_demo()
        self.assertEqual(result["checkpoint"], (1, "ready"))
        self.assertEqual(result["restored_findings"], 1)
        self.assertFalse(result["deployed"])

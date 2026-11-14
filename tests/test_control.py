"""Real transactional control API: independent identities, immutable approvals and knowledge."""

import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path

from sre_suite.auth import Action, TokenAuthenticator
from sre_suite.control.api import ControlApi, ControlPolicy
from sre_suite.control.contracts import Binding
from sre_suite.control.store import prepare_sqlite
from sre_suite.execution.evidence_client import EvidenceClient
from tests.execution_fixture import Fixture
from tests.test_api import AUDIENCE, TOKEN, credential, request
from tests.test_foundation import NOW
from tests.verification_fixture import TokenTransport


class ControlTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.f = Fixture(self.root)
        self.addCleanup(self.f.close)
        self.scope = self.f.ticket.scope
        self.tokens = {s: "synthetic-" + s + "x" * 43 for s in ("human", "steward", "agent")}
        self.tokens["worker-one"] = TOKEN
        self.auth = TokenAuthenticator(
            AUDIENCE,
            tuple(
                credential(self.scope, tuple(Action), token=t, subject=s)
                for s, t in self.tokens.items()
            ),
        )
        self.now = NOW
        self.app = ControlApi(
            self.auth,
            prepare_sqlite(str(self.root / "evidence.db")),
            clock=lambda: self.now,
            policy=ControlPolicy(
                frozenset({"human"}), frozenset({"steward"}), frozenset({"calls"})
            ),
        )
        self.binding = Binding.parse(
            {
                "scope": asdict(self.scope),
                "context_version": 0,
                "artifact_digest": "sha256:" + "a" * 64,
                "plan_digest": "sha256:" + "b" * 64,
                "action": "promote",
                "target": {"environment": "production", "cluster": "synthetic"},
            }
        )
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

    def call(self, op, payload, subject="human", operation_id="operation-one", expected=200):
        status, body, _ = request(
            self.app,
            "/v1/control/" + op,
            {
                "schema_version": 2,
                "scope": asdict(self.scope),
                "operation_id": operation_id,
                "payload": payload,
            },
            token=self.tokens[subject],
        )
        self.assertEqual(status, expected, body)
        return body.get("result", body)

    def approve(self, subject="human", binding=None, expected=200):
        return self.call(
            "approve",
            {
                "binding": (binding or self.binding).wire(),
                "expires_at": (NOW + timedelta(minutes=20)).isoformat(),
            },
            subject,
            expected=expected,
        )

    def proposal(self, value="database", identity="one"):
        return self.call(
            "propose",
            {
                "id": identity,
                "kind": "graph",
                "subject": "service",
                "predicate": "calls",
                "value": value,
                "evidence": asdict(self.evidence),
            },
            "worker-one",
        )

    def reconcile(
        self, proposal, version=0, decision="verify", supersedes=None, op="reconcile-one"
    ):
        return self.call(
            "reconcile",
            {
                "finding_id": proposal["finding_id"],
                "decision": decision,
                "expected_version": version,
                "supersedes": supersedes,
            },
            "steward",
            operation_id=op,
        )

    def test_human_approval_exact_binding_expiry_and_irreversible_revocation(self):
        approved = self.approve()
        self.assertTrue(approved["active"])
        self.assertTrue(approved["human"])
        self.assertEqual(self.approve(), approved)
        changed = replace(self.binding, artifact_digest="sha256:" + "c" * 64)
        self.assertFalse(self.call("approval", {"binding": changed.wire()})["active"])
        self.call("revoke", {"binding": self.binding.wire()})
        self.assertFalse(self.call("approval", {"binding": self.binding.wire()})["active"])
        self.approve(expected=409)

    def test_agents_cannot_approve_production_even_with_write_grant(self):
        self.approve(subject="agent", expected=403)
        self.app.policy = replace(
            self.app.policy, automatic_bindings=(("agent", self.binding.identity),)
        )
        self.approve(subject="agent", expected=403)
        dev = replace(self.binding, target={"environment": "development"})
        self.app.policy = replace(self.app.policy, automatic_bindings=(("agent", dev.identity),))
        self.assertFalse(self.approve(subject="agent", binding=dev)["human"])
        self.app.policy = replace(self.app.policy, automatic_bindings=())
        self.assertFalse(self.call("approval", {"binding": dev.wire()})["active"])

    def test_expiry_revocation_and_new_generation_block_approval_reuse(self):
        self.approve()
        self.now += timedelta(minutes=21)
        self.assertFalse(self.call("approval", {"binding": self.binding.wire()})["active"])
        self.now = NOW
        self.f.registry.start(
            replace(self.f.preview, scope=replace(self.scope, generation=2)), 1, NOW
        )
        self.call("approval", {"binding": self.binding.wire()}, expected=409)

    def test_proposal_requires_independent_steward_and_changes_context(self):
        proposed = self.proposal()
        self.assertEqual(self.proposal(), proposed)
        self.call(
            "reconcile",
            {
                "finding_id": proposed["finding_id"],
                "decision": "verify",
                "expected_version": 0,
                "supersedes": None,
            },
            "worker-one",
            expected=403,
        )
        result = self.reconcile(proposed)
        self.assertEqual(result["state"], "verified")
        self.assertEqual(self.reconcile(proposed), result)
        context = self.api.context(self.scope)
        self.assertEqual(context["version"], 1)
        self.assertEqual(context["findings"][0]["value"], "database")
        self.approve(expected=409)

    def test_conflicts_supersession_and_terminal_facts(self):
        first = self.proposal()
        self.reconcile(first)
        second = self.proposal("queue", "two")
        conflict = self.reconcile(second, 1, op="conflict")
        self.assertEqual(conflict["state"], "disputed")
        result = self.reconcile(second, 2, supersedes=first["finding_id"], op="replace")
        self.assertEqual(result["state"], "verified")
        self.assertEqual(self.api.context(self.scope)["findings"][0]["value"], "queue")
        self.call(
            "reconcile",
            {
                "finding_id": first["finding_id"],
                "decision": "verify",
                "expected_version": 4,
                "supersedes": None,
            },
            "steward",
            expected=409,
        )

    def test_outbox_pins_batch_across_new_events_and_ack_is_retry_safe(self):
        self.approve()
        batch = self.call("events", {"consumer": "events"})
        self.proposal()
        self.assertEqual(batch, self.call("events", {"consumer": "events"}))
        payload = {
            "consumer": "events",
            "expected_cursor": batch["cursor"],
            "batch_sha256": batch["batch_sha256"],
        }
        ack = self.call("ack", payload, operation_id="ack-one")
        self.assertEqual(ack, self.call("ack", payload, operation_id="ack-one"))
        next_batch = self.call("events", {"consumer": "events"})
        self.assertGreater(next_batch["next_cursor"], batch["next_cursor"])
        self.call("ack", {**payload, "batch_sha256": "0" * 64}, operation_id="bad", expected=409)

    def test_artifacts_are_content_verified_immutable_and_scoped(self):
        value = {"key": "artifact-one", "evidence": asdict(self.evidence)}
        first = self.call("artifact-put", value, "worker-one")
        self.assertEqual(first, self.call("artifact-put", value, "worker-one"))
        self.assertEqual(first, self.call("artifact-get", {"key": "artifact-one"}, "agent"))
        self.call("artifact-put", value, "agent", expected=403)
        changed = self.api.upload("other", self.scope, b"{}", NOW, NOW + timedelta(minutes=30))
        self.call(
            "artifact-put", {**value, "evidence": asdict(changed)}, "worker-one", expected=409
        )
        self.now += timedelta(minutes=31)
        self.call("artifact-get", {"key": "artifact-one"}, expected=409)

    def test_pending_is_scoped_and_corroboration_requires_final_verification(self):
        proposed = self.proposal()
        pending = self.call("pending", {}, "steward")
        self.assertEqual(len(pending["proposals"]), 1)
        result = self.reconcile(proposed, decision="corroborate")
        self.assertEqual(result["state"], "corroborated")
        self.assertEqual(self.api.context(self.scope)["findings"], [])
        self.reconcile(proposed, 1, op="verify-corroborated")
        self.assertEqual(len(self.api.context(self.scope)["findings"]), 1)

    def test_production_registration_requires_human_enable_and_monotonic_generation(self):
        from sre_suite.auth import Credential, Grant
        from sre_suite.contracts import Environment

        production = replace(
            self.scope, environment=Environment.PRODUCTION, environment_id="production-one"
        )
        credentials = tuple(
            Credential(
                s,
                __import__("hashlib").sha256(t.encode()).hexdigest(),
                AUDIENCE,
                NOW - timedelta(minutes=1),
                NOW + timedelta(hours=2),
                frozenset(Grant(production, a) for a in Action),
            )
            for s, t in self.tokens.items()
        )
        self.auth.replace_credentials(credentials)
        self.scope = production
        payload = {
            "scope": asdict(production),
            "requested_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
            "expected_generation": 0,
        }
        self.call("environment-register", payload, expected=403)
        self.app.policy = replace(self.app.policy, production_enabled=True)
        self.call("environment-register", payload, "agent", expected=403)
        self.call("environment-register", payload)
        self.assertEqual(self.api.context(production)["version"], 0)
        self.binding = replace(self.binding, scope=production)
        self.approve()
        self.assertTrue(self.call("approval", {"binding": self.binding.wire()})["human"])
        # Requesting a generation the credential does not grant fails before any database write.
        old = self.scope
        self.scope = replace(production, generation=2)
        self.call(
            "environment-register",
            {**payload, "scope": asdict(self.scope), "expected_generation": 1},
            expected=403,
        )
        self.scope = old
        self.app.policy = replace(self.app.policy, production_enabled=False)
        self.call("approval", {"binding": self.binding.wire()}, expected=403)

    def test_steward_corroborates_explicit_assertions_not_untrusted_claims(self):
        from sre_suite.control.client import ControlClient
        from sre_suite.control.steward import KnowledgeSteward, StewardRules

        good = self.proposal()
        self.proposal("unsubstantiated", "bad")
        agent = KnowledgeSteward(
            ControlClient(TokenTransport(self.app, self.tokens["steward"])),
            self.api,
            StewardRules(frozenset({"worker-one"}), frozenset({"calls"})),
        )
        result = agent.run(self.scope)
        self.assertEqual(result["scope"], asdict(self.scope))
        self.assertIn(good["finding_id"], {r["finding_id"] for r in result["results"]})
        self.assertEqual(
            {r.get("state", r.get("outcome")) for r in result["results"]},
            {"verified", "unsubstantiated"},
        )

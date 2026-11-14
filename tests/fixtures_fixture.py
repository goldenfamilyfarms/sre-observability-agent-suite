"""Synthetic runner transport plus independent real authenticated evidence subjects."""

import copy
from datetime import datetime
from pathlib import Path

from sre_suite.auth import TokenAuthenticator
from sre_suite.execution.evidence_client import EvidenceClient
from sre_suite.execution.http import RemoteError
from sre_suite.execution.journal import Journal
from sre_suite.execution.runtime import ExecutionRuntime
from sre_suite.execution.wire import canonical
from sre_suite.fixtures.contracts import FixturePolicy
from sre_suite.fixtures.runtime import FixtureRuntime
from tests.engineering_certification_fixture import EngineeringCertificationFixture
from tests.test_api import AUDIENCE, SECOND_TOKEN, credential
from tests.verification_fixture import THIRD, TokenTransport

RUNNER_TOKEN = "synthetic-fixture-runner-" + "r" * 32


def policy_raw(v):
    return {
        "schema_version": 2,
        "verification_policy": copy.deepcopy(v.policy_raw),
        "runner": "runner",
        "target": {
            "base_url": "https://fixture.example.invalid",
            "authorization_env": "SRE_FIXTURE_TARGET_AUTH",
            "ca_env": None,
        },
        "runner_queue": "synthetic-fixture-runner",
        "assessor_queue": "synthetic-assessor",
        "scenarios": [
            {
                "id": name,
                "path": "/fixtures/" + name,
                "payload": {"case": name},
                "assertions": {"/ok": True},
            }
            for name in ("traces", "logs", "promql")
        ],
        "checks": {
            key: {
                "scenario": key.split("-")[0].lower(),
                "expected": value["expected"],
                "justification": value["justification"],
            }
            for key, value in v.assessments.items()
        },
    }


class Target:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.responses = {}
        self.lose_once = False
        self.mutate = lambda r: None

    def call(self, method, path, payload):
        assert method == "POST"
        self.calls.append((path, copy.deepcopy(payload)))
        key = payload["idempotency_key"]
        if key not in self.responses:
            result = {
                k: copy.deepcopy(payload[k])
                for k in (
                    "schema_version",
                    "binding",
                    "policy_sha256",
                    "scenario",
                    "idempotency_key",
                )
            }
            result.update(executed_at=self.clock().isoformat(), result={"ok": True})
            self.mutate(result)
            self.responses[key] = result
        if self.lose_once:
            self.lose_once = False
            raise RemoteError(0)
        return copy.deepcopy(self.responses[key])


class FixtureAgentFixture:
    def __init__(self, root: Path, *, clock=None):
        self.engineering = EngineeringCertificationFixture(root, clock=clock)
        self.f, self.v = self.engineering.f, self.engineering.v
        self.raw = policy_raw(self.v)
        self.policy = FixturePolicy.parse(self.raw)
        self.path = root / "fixture-policy.json"
        self.path.write_text(canonical(self.raw))
        self.path.chmod(0o600)
        self.f.app._auth = TokenAuthenticator(
            AUDIENCE,
            (
                credential(self.v.scope),
                credential(self.v.scope, token=SECOND_TOKEN, subject="assessor"),
                credential(self.v.scope, token=THIRD, subject="verifier"),
                credential(self.v.scope, token=RUNNER_TOKEN, subject="runner"),
            ),
        )
        self.api = EvidenceClient(
            "https://evidence.example.invalid",
            RUNNER_TOKEN,
            TokenTransport(self.f.app, RUNNER_TOKEN),
        )
        self.engineering.config["fixture"] = self.policy.dispatch
        self.engineering.path.write_text(canonical(self.engineering.config))
        old = self.engineering.runtime
        self.engineering.runtime = ExecutionRuntime(
            old.settings,
            jira=old.jira,
            github=old.github,
            evidence=old.evidence,
            tests=old.tests,
            clock=old.clock,
        )
        self.now = self.engineering.clock()
        self.target = Target(lambda: self.now)
        self.runner = FixtureRuntime(
            self.policy,
            self.api,
            Journal(root / "runner-journal"),
            target=self.target,
            clock=lambda: self.now,
        )
        self.assessor = FixtureRuntime(
            self.policy, self.v.assessor, Journal(root / "assessor-journal"), clock=lambda: self.now
        )

    def begin(self):
        self.engineering.begin()
        self.request = {"plan": self.engineering.cert_plan, "dispatch": self.policy.dispatch}
        self.start = datetime.fromisoformat(self.engineering.cert_plan["window_start"])
        self.end = datetime.fromisoformat(self.engineering.cert_plan["window_end"])
        self.now = self.start
        self.f.app._clock = lambda: self.now

    def complete(self):
        artifact = self.runner.execute(self.request)
        self.now = self.end
        reference = self.runner.publish(self.request, artifact)
        result = self.assessor.assess(self.request, reference)
        self.engineering.collect()
        self.engineering.fixture_ref = result["evidence"]
        return reference, result

    def close(self):
        self.engineering.close()

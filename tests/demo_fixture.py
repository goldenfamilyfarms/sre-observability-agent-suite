"""Private synthetic target, real evidence API and the packaged demo controller."""

import copy
from pathlib import Path

from sre_suite.demo_fixture.controller import Controller, recipe
from sre_suite.demo_fixture.scenarios import matrix, prepare_policy
from sre_suite.execution.journal import Journal
from sre_suite.execution.wire import digest
from sre_suite.fixtures.contracts import FixturePolicy
from sre_suite.fixtures.runtime import FixtureRuntime
from tests.fixtures_fixture import FixtureAgentFixture


class DemoFixture:
    def __init__(self, root, *, target=None):
        self.f = FixtureAgentFixture(root)
        self.f.begin()
        self.verification = copy.deepcopy(self.f.v.policy_raw)
        for key, item in matrix().items():
            if not item["applicable"]:
                self.verification["checks"][key].update(
                    applicable=False, source=None, selector={}, parameters={}
                )
        self.expectations = {
            key: self.f.v.assessments[key]["expected"]
            for key, item in matrix().items()
            if item["expectation_required"]
        }
        self.raw = prepare_policy(
            self.verification, self.expectations, target or self.f.raw["target"], "runner"
        )
        self.policy = FixturePolicy.parse(self.raw)
        self.recipe = recipe("synthetic/demo", self.policy.verification.scope.revision)
        self.request = copy.deepcopy(self.f.request)
        self.request["plan"].pop("digest")
        self.request["plan"]["release"]["artifact_digest"] = "sha256:" + digest(self.recipe)
        self.request["plan"].update(
            policy_sha256=self.policy.verification.fingerprint, fixture=self.policy.dispatch
        )
        self.request["dispatch"] = self.policy.dispatch
        self.root = Path(root) / "controller"
        self.root.mkdir(mode=0o700)
        self.controller = self.reopen()
        self.lose_once = False
        self.runner = FixtureRuntime(
            self.policy,
            self.f.api,
            Journal(Path(root) / "demo-runner"),
            target=self,
            clock=lambda: self.f.now,
        )
        self.assessor = FixtureRuntime(
            self.policy,
            self.f.v.assessor,
            Journal(Path(root) / "demo-assessor"),
            clock=lambda: self.f.now,
        )

    def reopen(self):
        return Controller(
            self.policy, self.request, self.recipe, self.root, self.f.api, clock=lambda: self.f.now
        )

    def call(self, method, path, payload):
        assert method == "POST"
        response = self.controller.execute(path, payload)
        if self.lose_once:
            self.lose_once = False
            from sre_suite.execution.http import RemoteError

            raise RemoteError(0)
        return response

    def close(self):
        self.f.close()

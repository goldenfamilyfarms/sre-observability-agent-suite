"""Real scoped API and all 120 synthetic observations for an engineering candidate."""

import json
from dataclasses import asdict, replace
from unittest.mock import patch

from sre_suite.contracts import Release
from sre_suite.execution.runtime import ExecutionRuntime
from sre_suite.execution.wire import canonical
from sre_suite.telemetry.schedule_contracts import TelemetrySchedule
from tests.test_engineering_telemetry import run_stages
from tests.verification_fixture import VerificationFixture


class EngineeringCertificationFixture:
    def __init__(self, root, *, wait=5, publication=False, clock=None):
        self.v = VerificationFixture(root)
        self.f = self.v.fixture
        self.clock = clock or self.v.engine.clock
        raw = self.v.policy_raw
        self.telemetry = {
            "schema_version": 2,
            "policy": {
                "schema_version": 2,
                "scope": raw["scope"],
                "profiles": [s["profile"] for s in raw["sources"]],
            },
            "profiles": [
                {"profile": s["profile"]["id"], "trace_id": s["trace_id"]} for s in raw["sources"]
            ],
            "task_queue": "synthetic-telemetry",
            "producer": raw["collector"],
            "delay_seconds": 0,
            "window_seconds": 3,
        }
        self.config = {
            "schema_version": 2,
            "policy": raw,
            "task_queue": "synthetic-certification",
            "fixture_wait_seconds": wait,
        }
        self.path = root / "engineering-certification.json"
        self.telemetry_path = root / "engineering-telemetry.json"
        for p, value in ((self.path, self.config), (self.telemetry_path, self.telemetry)):
            p.write_text(canonical(value))
            p.chmod(0o600)
        old = self.f.runtime
        self.publication = None
        if publication:
            from tests.publication_fixture import publication_runtime

            old, *self.publication = publication_runtime(self.f)
        self.runtime = ExecutionRuntime(
            replace(
                old.settings,
                telemetry_config=str(self.telemetry_path),
                certification_config=str(self.path),
            ),
            jira=old.jira,
            github=old.github,
            evidence=old.evidence,
            tests=old.tests,
            clock=self.clock,
        )
        self.f.app._clock = self.clock

    def begin(self):
        self.args = run_stages(self.runtime, self.f.spec)
        self.plan = self.runtime.prepare_telemetry(self.args)
        self.cert_plan = self.runtime.prepare_certification(self.args)
        self.args["certification_plan"] = self.cert_plan

    def collect(self, plan=None):
        if plan is not None:
            self.plan = plan
        schedule = TelemetrySchedule.parse(self.plan["schedules"][0]["schedule"])
        start, end = schedule.request.start, schedule.request.end
        self.v.release = Release(
            self.v.scope,
            self.plan["binding"]["artifact_digest"],
            self.plan["binding"]["plan_digest"],
            ("certification",),
        )
        now = max(self.clock(), end)
        self.runtime.clock = lambda: now
        self.f.app._clock = lambda: now
        with (
            patch("tests.verification_fixture.START", start),
            patch("tests.verification_fixture.END", end),
            patch("tests.verification_fixture.NOW", now),
        ):
            self.v.populate()
            self.v.collect()
        self.v.engine.clock = self.runtime.clock
        by_id = dict(self.v.observations)
        self.reports = []
        for entry in self.plan["schedules"]:
            s = TelemetrySchedule.parse(entry["schedule"])
            e = by_id[s.request.profile]
            receipt = {
                "slot": 0,
                "status": "observed",
                "evidence_id": e.id,
                "evidence_sha256": e.sha256,
                "evidence": json.loads(canonical(asdict(e))),
            }
            self.reports.append({"outcome": "collected", "collections": [receipt]})
        self.fixture_ref = json.loads(canonical(asdict(self.v.input.fixture)))
        return self.reports

    def bind(self):
        self.args["telemetry"] = {"plan": self.plan, "reports": self.reports}
        self.args["certification_fixture"] = self.fixture_ref
        return self.runtime.bind_certification(self.args)

    def certify(self):
        request = self.bind()
        receipt = self.v.engine.run(request["input"], publish=True)
        self.args["certification"] = {"request": request, "receipt": receipt}
        return receipt

    def close(self):
        self.v.close()

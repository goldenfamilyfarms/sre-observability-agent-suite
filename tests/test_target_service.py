import copy
import tempfile
import unittest
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

from sre_suite.api_wire import evidence_from_wire
from sre_suite.fixtures.target_service import validate, verify
from sre_suite.target_service.prepare import export_reference, prepare
from sre_suite.target_service.reference import State, acknowledge_trace, resource
from tests.delivery_fixture import DeliveryFixture
from tests.fixtures_fixture import FixtureAgentFixture
from tests.test_foundation import NOW


def identity():
    return {
        "service.name": "reference",
        "deployment.environment.name": "development",
        "service.version": "c" * 40,
        "sre.source.revision": "b" * 40,
        "sre.environment.id": "preview-one",
        "sre.environment.generation": "1",
        "sre.artifact.digest": "sha256:" + "d" * 64,
        "sre.plan.digest": "sha256:" + "e" * 64,
    }


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.state = State(self.root / "state.db", identity())

    def test_measured_http_unicode_error_spans_logs_metrics_and_retry(self):
        for name, status in [("health", 200), ("unicode", 200), ("error", 500)]:
            parent = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
            result = self.state.request("/" + name, parent, name, self.state.binding)
            self.assertEqual(result[0], status)
            self.assertEqual(
                result, self.state.request("/" + name, parent, name, self.state.binding)
            )
        self.assertEqual(len(self.state.pending()), 6)
        traces = [p for _, s, p in self.state.pending() if s == "traces"]
        spans = [v["resourceSpans"][0]["scopeSpans"][0]["spans"][0] for v in traces]
        self.assertTrue(all(int(s["endTimeUnixNano"]) > int(s["startTimeUnixNano"]) for s in spans))
        self.assertTrue(
            all(s["traceId"] == "a" * 32 and s["parentSpanId"] == "b" * 16 for s in spans)
        )
        self.assertEqual(sum(bool(s["events"]) for s in spans), 1)
        self.assertIn("sre_target_requests_total", self.state.metrics())
        self.assertIn('sre_environment_generation="1"', self.state.metrics())
        self.assertEqual(len(State(self.state.path, identity()).pending()), 6)

    def test_stale_scope_reused_id_and_state_generation_are_rejected(self):
        parent = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
        with self.assertRaises(ValueError):
            self.state.request("/health", parent, "one", "bad")
        self.state.request("/health", parent, "one", self.state.binding)
        with self.assertRaises(ValueError):
            self.state.request("/error", parent, "one", self.state.binding)
        with self.assertRaises(ValueError):
            State(self.state.path, identity() | {"sre.environment.generation": "2"})
        for changes in (
            {"deployment.environment.name": "production"},
            {"service.version": "main"},
            {"sre.artifact.digest": "latest"},
        ):
            env = {
                "OTEL_RESOURCE_ATTRIBUTES": ",".join(
                    k + "=" + v for k, v in (identity() | changes).items()
                )
            }
            with self.assertRaises(ValueError):
                resource(env)

    def test_trace_acknowledgements_accept_omitted_zero_and_reject_loss_or_warning(self):
        for value in (
            {},
            {"partialSuccess": {}},
            {"partialSuccess": {"rejectedSpans": "0"}},
            {"partialSuccess": {"rejectedSpans": 0, "errorMessage": ""}},
        ):
            acknowledge_trace(value)
        for value in (
            {"partialSuccess": {"rejectedSpans": "1"}},
            {"partialSuccess": {"errorMessage": "warning"}},
            {"partialSuccess": {"rejectedSpans": False}},
            {"partialSuccess": []},
            {"unknown": 0},
        ):
            with self.assertRaises(ValueError):
                acknowledge_trace(value)

    def test_journey_array_assertions_require_exact_status_and_body(self):
        from sre_suite.verification.predicates import MISSING, get, matches

        result = {"steps": [{"status": 200, "body": {"status": "ok"}}]}
        expected = {"/steps/0/status": 200, "/steps/0/body/status": "ok"}
        self.assertTrue(matches(result, expected))
        for path in ("/steps/1/status", "/steps/-1/status", "/steps/01/status", "/steps/-/status"):
            self.assertIs(get(result, path), MISSING)
        result["steps"][0]["status"] = 500
        self.assertFalse(matches(result, expected))
        result["steps"][0]["status"] = 200
        result["steps"][0]["body"]["status"] = "failed"
        self.assertFalse(matches(result, expected))
        self.assertEqual(get({"a/b": [{"~": 1}]}, "/a~1b/0/~0"), 1)

    def test_export_is_standalone_and_refuses_overwrite(self):
        export_reference(self.root / "reference")
        content = (self.root / "reference/reference_service.py").read_text()
        compile(content, "reference_service.py", "exec")
        self.assertNotIn("from sre_suite", content)
        with self.assertRaises(FileExistsError):
            export_reference(self.root / "reference")


class TargetProofTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.d = DeliveryFixture(self.root)
        self.addCleanup(self.d.close)
        self.d.approve()
        self.d.runtime.deploy()
        self.d.cluster.healthy(self.d.request)
        receipt = self.d.runtime.readiness()
        self.config = validate(
            {
                "build": self.d.raw["build"],
                "readiness": receipt["evidence"],
                "builder": "builder",
                "delivery": "delivery",
                "maximum_age_seconds": 300,
            },
            "runner",
            "assessor",
            "collector",
            "verifier",
        )
        self.binding = {
            "release": {
                "scope": asdict(self.d.scope),
                "artifact_digest": self.d.request.binding.artifact_digest,
                "plan_digest": self.d.request.binding.plan_digest,
                "required_gates": ["certification"],
            },
            "artifact_kind": "container-image-v1",
            "context_version": 0,
        }

    def test_independent_proofs_bind_actual_image_plan_generation_and_freshness(self):
        result = verify(self.config, self.binding, self.d.evidence, NOW)
        self.assertEqual(result["service.version"], "d" * 40)
        for mutate in ("artifact", "plan", "generation", "kind"):
            raw = copy.deepcopy(self.binding)
            if mutate == "generation":
                raw["release"]["scope"]["generation"] += 1
            elif mutate == "kind":
                raw["artifact_kind"] = "source-candidate-v1"
            else:
                raw["release"][mutate + "_digest"] = "sha256:" + "e" * 64
            with self.assertRaises(ValueError):
                verify(self.config, raw, self.d.evidence, NOW)
        with self.assertRaises(ValueError):
            verify(self.config, self.binding, self.d.evidence, NOW + timedelta(seconds=301))

    def test_readiness_refreshes_even_with_unchanged_pod_resource_versions(self):
        self.d.runtime.clock = lambda: NOW + timedelta(seconds=20)
        self.d.app._clock = self.d.runtime.clock
        receipt = self.d.runtime.readiness()
        ref = evidence_from_wire(receipt["evidence"])
        self.assertEqual(ref.observed_at, NOW + timedelta(seconds=20))
        self.assertNotEqual(receipt["evidence"], self.config["readiness"])

    def test_prepare_keeps_full_reviewed_oracle_and_requires_image_plan(self):
        # Separate synthetic fixture policy; preparation itself never reads a live target.
        folder = self.root / "fixture"
        folder.mkdir()
        f = FixtureAgentFixture(folder)
        self.addCleanup(f.close)
        f.begin()
        plan = copy.deepcopy(f.engineering.cert_plan)
        plan.pop("digest", None)
        plan.pop("fixture", None)
        plan["artifact_kind"] = "container-image-v1"
        checks = copy.deepcopy(f.raw["checks"])
        names = {"traces": "health", "logs": "unicode", "promql": "error"}
        for check in checks.values():
            check["scenario"] = names[check["scenario"]]
        raw = {
            "plan": plan,
            "verification_policy": f.raw["verification_policy"],
            "checks": checks,
            "runner": "runner",
            "runner_queue": "reference-runner",
            "assessor_queue": "reference-assessor",
            "origin": {
                "base_url": "https://service.example.invalid",
                "authorization_env": "SRE_JOURNEY_SERVICE_AUTH",
                "ca_env": None,
            },
            "target_service": self.config,
        }
        result = prepare(raw)
        self.assertEqual(result["fixture_policy"]["checks"], checks)
        self.assertEqual(len(result["fixture_policy"]["scenarios"]), 3)
        self.assertEqual(len(result["fixture_policy"]["checks"]), 120)
        raw["plan"]["artifact_kind"] = "source-candidate-v1"
        with self.assertRaises(ValueError):
            prepare(raw)

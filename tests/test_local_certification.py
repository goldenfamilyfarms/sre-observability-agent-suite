"""Full-policy local bundle preparation and exact multi-query engineering contracts."""

import copy
import json
from dataclasses import asdict
from unittest.mock import patch

from sre_suite.demo_fixture.managed import ManagedPolicy
from sre_suite.execution.certification import EngineeringCertification
from sre_suite.execution.telemetry import EngineeringTelemetry
from sre_suite.execution.wire import canonical
from sre_suite.local_pilot.config import write_private
from sre_suite.local_telemetry.certification import prepare
from sre_suite.local_telemetry.config import initialize
from sre_suite.telemetry.schedule_contracts import TelemetrySchedule
from tests.managed_fixture import ManagedFixture
from tests.test_local_pilot import LocalFixture


def write_json(path, value):
    path.write_text(canonical(value))
    path.chmod(0o600)


class CertificationBundleTests(LocalFixture):
    def setUp(self):
        super().setUp()
        self.initialize()
        root = self.root / "managed"
        root.mkdir(mode=0o700)
        self.f = ManagedFixture(
            root, certificate=self.stack / "server.crt", key=self.stack / "server.key"
        )
        self.addCleanup(self.f.close)
        self.raw = copy.deepcopy(self.f.config)
        verification = self.raw["fixture_policy"]["verification_policy"]
        extra = copy.deepcopy(verification["sources"][0])
        extra["profile"].update(
            id="histogram", query_template="sre_demo_request_duration_seconds_bucket${scope}"
        )
        extra.update(step_seconds=1, limit=200)
        verification["sources"].append(extra)
        self.manifest["registration"]["preview"]["scope"] = asdict(
            self.f.d.policy.verification.scope
        )
        write_json(self.manifest_path, self.manifest)
        self.metrics = self.root / "metrics-token"
        write_private(self.metrics, b"Bearer " + b"m" * 40)
        self.telemetry_stack = self.root / "telemetry-stack"
        self.config = initialize(
            self.telemetry_stack,
            self.manifest_path,
            "https://localhost:9443",
            self.stack / "server.crt",
            self.metrics,
            mode="desktop",
        )
        self.input = self.root / "managed-policy.json"
        write_json(self.input, self.raw)
        self.output = self.root / "certification"

    def build(self, **kwargs):
        return prepare(self.telemetry_stack, self.input, self.output, **kwargs)

    def test_bundle_preserves_all_rules_oracles_scope_and_recipe_without_starting(self):
        with patch("sre_suite.local_telemetry.lifecycle.command") as docker:
            result = self.build()
        docker.assert_not_called()
        self.assertEqual(result["checks"], 120)
        self.assertEqual(result["sources"], 4)
        self.assertFalse(result["execution_started"])
        output = json.loads((self.output / "managed-demo.json").read_text())
        original = self.raw["fixture_policy"]
        for key in ("checks", "scenarios", "runner", "target"):
            self.assertEqual(original[key], output["fixture_policy"][key])
        for key in ("checks", "scope", "collector", "assessor", "verifier", "catalog_sha256"):
            self.assertEqual(
                original["verification_policy"][key],
                output["fixture_policy"]["verification_policy"][key],
            )
        self.assertEqual(output["recipe"], self.raw["recipe"])
        self.assertNotEqual(ManagedPolicy(self.raw).dispatch, ManagedPolicy(output).dispatch)
        telemetry = EngineeringTelemetry(self.output / "engineering-telemetry.json")
        EngineeringCertification(self.output / "engineering-certification.json", telemetry)
        self.assertEqual(telemetry.query_options["histogram"], (1, 200))
        self.assertEqual(len(telemetry.profiles), 4)
        for p in self.output.iterdir():
            self.assertEqual(p.stat().st_mode & 0o077, 0)
            self.assertNotIn("Bearer ", p.read_text())
        with self.assertRaises(ValueError):
            self.build()

    def test_opt_in_demo_instrumentation_builds_complete_reviewable_policy(self):
        verification = self.raw["fixture_policy"]["verification_policy"]
        verification["sources"] = verification["sources"][:3]
        metric = next(
            s["profile"] for s in verification["sources"] if s["profile"]["backend"] == "prometheus"
        )
        metric["scope_labels"] = {
            "service": "service_name",
            "environment": "deployment_environment",
            "revision": "service_version",
            "environment_id": "preview_id",
            "generation": "preview_generation",
        }
        write_json(self.input, self.raw)
        with patch("sre_suite.local_telemetry.lifecycle.command") as docker:
            result = self.build(demo_instrumentation=True)
        docker.assert_not_called()
        self.assertFalse(result["execution_started"])
        output = json.loads((self.output / "managed-demo.json").read_text())
        policy = ManagedPolicy(output)
        self.assertEqual(result["checks"], 120)
        self.assertEqual(result["sources"], 6)
        self.assertEqual(sum(r.applicable for r in policy.fixture.verification.rules), 76)
        self.assertEqual(output["recipe"], self.raw["recipe"])
        self.assertEqual(
            output["fixture_policy"]["verification_policy"]["scope"], verification["scope"]
        )
        self.assertNotEqual(policy.dispatch, ManagedPolicy(self.raw).dispatch)
        self.assertTrue(
            all(
                "/unavailable_measurement" not in r.selector
                for r in policy.fixture.verification.rules
            )
        )

    def test_preview_generation_target_and_source_mapping_substitution_rejected(self):
        for mutate in (
            lambda r: r["fixture_policy"]["verification_policy"]["scope"].update(generation=2),
            lambda r: r["fixture_policy"]["verification_policy"]["scope"].update(
                environment_id="other-preview"
            ),
            lambda r: r["fixture_policy"]["target"].update(base_url="https://localhost:9444"),
            lambda r: r.update(certificate=str(self.telemetry_stack / "server.crt")),
            lambda r: r["fixture_policy"]["verification_policy"]["sources"][-1]["profile"][
                "binding_labels"
            ].update(plan_digest="different_plan"),
            lambda r: r["fixture_policy"]["checks"]["TRACES-001"].update(justification="altered"),
        ):
            raw = copy.deepcopy(self.raw)
            mutate(raw)
            write_json(self.input, raw)
            with self.assertRaises(ValueError):
                self.build()
            self.assertFalse(self.output.exists())

    def test_queues_window_and_missing_rules_rejected_atomically(self):
        for kwargs in (
            {"window_seconds": 1},
            {"collector_queue": "same", "verifier_queue": "same"},
            {"collector_queue": self.f.d.policy.runner_queue},
        ):
            with self.assertRaises(ValueError):
                self.build(**kwargs)
        del self.raw["fixture_policy"]["verification_policy"]["checks"]["LOGS-040"]
        write_json(self.input, self.raw)
        with self.assertRaises(ValueError):
            self.build()
        self.assertFalse(self.output.exists())

    def test_query_resolution_is_bound_and_legacy_fingerprint_is_stable(self):
        engine = self.f.d.f.engineering
        before = EngineeringTelemetry(engine.telemetry_path)
        raw = copy.deepcopy(engine.telemetry)
        for item in raw["profiles"]:
            item.update(step_seconds=15, limit=100)
        write_json(engine.telemetry_path, raw)
        self.assertEqual(
            before.fingerprint, EngineeringTelemetry(engine.telemetry_path).fingerprint
        )
        raw["profiles"][0].update(step_seconds=1, limit=200)
        write_json(engine.telemetry_path, raw)
        after = EngineeringTelemetry(engine.telemetry_path)
        self.assertNotEqual(before.fingerprint, after.fingerprint)
        with self.assertRaises(ValueError):
            EngineeringCertification(engine.path, after)
        for value in (True, 0, 3601):
            raw["profiles"][0]["step_seconds"] = value
            write_json(engine.telemetry_path, raw)
            with self.assertRaises(ValueError):
                EngineeringTelemetry(engine.telemetry_path)

    def test_schedules_preserve_reviewed_step_limit_and_duplicate_ids_fail(self):
        engine = self.f.d.f.engineering
        raw = copy.deepcopy(engine.telemetry)
        raw["profiles"][0].update(step_seconds=1, limit=200)
        extra = copy.deepcopy(raw["policy"]["profiles"][0])
        extra["id"] = "additional-metric"
        raw["policy"]["profiles"].append(extra)
        raw["profiles"].append(
            {"profile": extra["id"], "trace_id": None, "step_seconds": 2, "limit": 150}
        )
        write_json(engine.telemetry_path, raw)
        telemetry = EngineeringTelemetry(engine.telemetry_path)
        with patch.object(engine.runtime, "telemetry", telemetry):
            # Use a fresh journal key for the new authorized configuration.
            args = copy.deepcopy(engine.args)
            args["reference"] = dict(args["reference"])
            with patch.object(
                engine.runtime.journal, "compute", side_effect=lambda k, v, f, *a: f()
            ):
                plan = telemetry.prepare(engine.runtime, args, lambda: None)
        requests = [TelemetrySchedule.parse(e["schedule"]).request for e in plan["schedules"]]
        self.assertEqual((requests[0].step_seconds, requests[0].limit), (1, 200))
        self.assertEqual((requests[-1].step_seconds, requests[-1].limit), (2, 150))
        raw["profiles"].append(raw["profiles"][0])
        write_json(engine.telemetry_path, raw)
        with self.assertRaises(ValueError):
            EngineeringTelemetry(engine.telemetry_path)

    def test_command_outputs_only_review_metadata_and_never_starts_execution(self):
        import io
        from contextlib import redirect_stdout

        from sre_suite.local_telemetry.__main__ import main

        stream = io.StringIO()
        with (
            patch(
                "sys.argv",
                [
                    "local-telemetry",
                    "prepare-certification",
                    "--directory",
                    str(self.telemetry_stack),
                    "--managed-policy",
                    str(self.input),
                    "--output",
                    str(self.output),
                ],
            ),
            patch("sre_suite.local_telemetry.lifecycle.command") as docker,
            redirect_stdout(stream),
        ):
            self.assertEqual(main(), 0)
        docker.assert_not_called()
        result = json.loads(stream.getvalue())
        self.assertEqual(result["checks"], 120)
        self.assertFalse(result["execution_started"])
        self.assertNotIn("Bearer", stream.getvalue())

    def test_more_than_twenty_sources_and_changed_recipe_cannot_be_prepared(self):
        for case in ("too-many", "recipe"):
            raw = copy.deepcopy(self.raw)
            if case == "recipe":
                raw["recipe"]["edits"][0]["content"] = "unreviewed candidate"
            else:
                sources = raw["fixture_policy"]["verification_policy"]["sources"]
                for i in range(17):
                    extra = copy.deepcopy(sources[0])
                    extra["profile"]["id"] = "additional-" + str(i)
                    sources.append(extra)
            write_json(self.input, raw)
            with self.assertRaises(ValueError):
                self.build()
            self.assertFalse(self.output.exists())

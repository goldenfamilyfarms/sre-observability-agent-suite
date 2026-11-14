"""Source pinning, durable controller recovery, independent assessment and honest gaps."""

import copy
import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from sre_suite.api_wire import evidence_from_wire
from sre_suite.demo_fixture.__main__ import main
from sre_suite.demo_fixture.controller import Controller, validate_target
from sre_suite.demo_fixture.export import acknowledge, flush, transports
from sre_suite.demo_fixture.scenarios import matrix, prepare_policy
from sre_suite.execution.http import RemoteError
from sre_suite.execution.journal import Journal
from sre_suite.execution.wire import canonical
from sre_suite.fixtures.contracts import FixturePolicy, FixtureRequest
from sre_suite.verification.contracts import VerificationInput
from sre_suite.verification.runtime import CertificationEngine
from tests.demo_fixture import DemoFixture


class DemoControllerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.d = DemoFixture(self.root)
        self.addCleanup(self.d.close)

    def command(self, index=1):
        scenario = self.d.policy.scenarios[index]
        return scenario.path, self.d.runner.command(FixtureRequest.parse(self.d.request), scenario)

    def rows(self, table):
        assert table in {"executions", "outbox"}
        with self.d.controller.connect() as conn:
            return conn.execute("SELECT * FROM " + table).fetchall()

    def test_real_controller_runner_assessor_emit_complete_bound_120_slot_document(self):
        artifact = self.d.runner.execute(self.d.request)
        self.d.f.now = self.d.f.end
        reference = self.d.runner.publish(self.d.request, artifact)
        result = self.d.assessor.assess(self.d.request, reference)
        doc = json.loads(self.d.f.v.assessor.read(evidence_from_wire(result["evidence"])))
        self.assertEqual(set(doc["checks"]), set(matrix()))
        self.assertEqual(doc["execution"]["evidence"], reference)
        for key, rule in matrix().items():
            self.assertEqual(doc["checks"][key]["applicable"], rule["applicable"])
        self.assertEqual(len(self.rows("executions")), 3)
        self.assertEqual(len(self.rows("outbox")), 2)
        # Fixture success cannot supply missing telemetry or manufacture a certificate.
        v = VerificationInput.parse(
            {
                "schema_version": 2,
                **{
                    k: doc[k]
                    for k in (
                        "release",
                        "artifact_kind",
                        "policy_sha256",
                        "context_version",
                        "window_start",
                        "window_end",
                    )
                },
                "fixture": result["evidence"],
                "observations": [],
            }
        )
        engine = CertificationEngine(
            self.d.policy.verification,
            self.d.f.v.api,
            Journal(self.root / "no-telemetry"),
            clock=lambda: self.d.f.now,
        )
        report = engine.run(v.wire())
        self.assertNotEqual(report["verdict"], "PASSED")
        self.assertEqual(len(report["results"]), 120)

    def test_lost_response_restart_and_concurrent_duplicates_commit_once(self):
        self.d.lose_once = True
        with self.assertRaises(RemoteError):
            self.d.runner.execute(self.d.request)
        self.d.controller = self.d.reopen()
        artifact = self.d.runner.execute(self.d.request)
        path, command = self.command()
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: self.d.controller.execute(path, command), range(8)))
        self.assertTrue(all(r == responses[0] for r in responses))
        self.assertEqual(len(self.rows("executions")), 3)
        self.assertEqual(len(self.rows("outbox")), 2)
        self.assertEqual(self.d.runner.execute(self.d.request), artifact)
        self.assertEqual(len(json.loads(self.rows("executions")[1][3])), 6)

    def test_candidate_recipe_bytes_revision_and_extra_edits_are_rejected(self):
        for mutate in (
            lambda r: r["edits"][0].update(content="malicious"),
            lambda r: r.update(base_revision="f" * 40),
            lambda r: r["edits"].append({"path": "other.py", "content": ""}),
        ):
            raw = copy.deepcopy(self.d.recipe)
            mutate(raw)
            with self.assertRaises(ValueError):
                validate_target(self.d.policy, FixtureRequest.parse(self.d.request), raw)

    def test_target_rejects_every_changed_binding_command_and_endpoint(self):
        path, command = self.command()
        self.d.controller.execute(path, command)
        for mutate in (
            lambda c: c["binding"]["release"].update(artifact_digest="sha256:" + "a" * 64),
            lambda c: c["binding"]["release"].update(plan_digest="sha256:" + "a" * 64),
            lambda c: c["binding"]["release"]["scope"].update(environment_id="preview-other"),
            lambda c: c["binding"]["release"]["scope"].update(generation=2),
            lambda c: c["binding"].update(context_version=1),
            lambda c: c["binding"].update(window_end=self.d.f.start.isoformat()),
            lambda c: c.update(policy_sha256="a" * 64),
            lambda c: c.update(trace_ids=["b" * 32]),
            lambda c: c.update(idempotency_key="b" * 64),
            lambda c: c.update(payload={"url": "https://forbidden.invalid"}),
            lambda c: c.update(schema_version=True),
        ):
            raw = copy.deepcopy(command)
            mutate(raw)
            with self.assertRaises(ValueError):
                self.d.controller.execute(path, raw)
        for endpoint in ("//fixtures/http-journey", path + "?x=1", "/unknown"):
            with self.assertRaises(ValueError):
                self.d.controller.execute(endpoint, command)
        self.assertEqual(len(self.rows("executions")), 1)

    def test_expiry_cached_recovery_and_generation_checks(self):
        path, command = self.command()
        original = self.d.controller.execute(path, command)
        self.d.f.now = self.d.f.end
        self.assertEqual(self.d.controller.execute(path, command), original)
        with self.assertRaises(ValueError):
            self.d.controller.execute(*self.command(0))
        self.d.f.now += timedelta(seconds=121)
        with self.assertRaises(ValueError):
            self.d.controller.execute(path, command)
        self.d.f.now = self.d.f.start
        self.d.f.f.registry.start(
            replace(
                self.d.f.f.preview, scope=replace(self.d.policy.verification.scope, generation=2)
            ),
            1,
            self.d.f.now,
        )
        with self.assertRaises(RemoteError):
            self.d.controller.execute(path, command)
        with self.assertRaises(RemoteError):
            self.d.controller.metrics()

    def test_failed_guard_or_mid_transaction_crash_rolls_back_all_effects(self):
        with (
            patch.object(
                self.d.controller, "telemetry", side_effect=RuntimeError("simulated crash")
            ),
            self.assertRaises(RuntimeError),
        ):
            self.d.controller.execute(*self.command())
        self.assertEqual(self.rows("executions"), [])
        self.assertEqual(self.rows("outbox"), [])
        self.d.controller = self.d.reopen()
        original = self.d.controller.perform

        def late(*args):
            result = original(*args)
            self.d.f.now = self.d.f.end
            return result

        with (
            patch.object(self.d.controller, "perform", side_effect=late),
            self.assertRaises(ValueError),
        ):
            self.d.controller.execute(*self.command())
        self.assertEqual(self.rows("executions"), [])

    def test_state_cannot_be_reused_for_another_plan(self):
        request = copy.deepcopy(self.d.request)
        request["plan"]["release"]["plan_digest"] = "sha256:" + "e" * 64
        with self.assertRaises(ValueError):
            Controller(self.d.policy, request, self.d.recipe, self.d.root, self.d.f.api)

    def test_reviewed_matrix_preserves_infrastructure_gates_and_requires_private_oracles(self):
        rules = matrix()
        self.assertEqual(len(rules), 120)
        for key in ("LOGS-036", "TRACES-039", "PROMQL-009", "PROMQL-038", "PROMQL-040"):
            self.assertTrue(rules[key]["applicable"])
        for key in ("TRACES-009", "TRACES-012", "PROMQL-016", "PROMQL-024"):
            self.assertFalse(rules[key]["applicable"])
            self.assertTrue(rules[key]["justification"])
        raw = copy.deepcopy(self.d.verification)
        raw["checks"]["LOGS-036"].update(applicable=False, source=None, selector={}, parameters={})
        with self.assertRaises(ValueError):
            prepare_policy(raw, self.d.expectations, self.d.raw["target"], "runner")
        with self.assertRaises(ValueError):
            prepare_policy(self.d.verification, {}, self.d.raw["target"], "runner")

    def test_edited_scenarios_cannot_use_reviewed_controller(self):
        for mutate in (
            lambda p: p["scenarios"][1].update(payload={"count": 10000}),
            lambda p: p["scenarios"][0]["assertions"].update({"/capabilities/grpc": True}),
            lambda p: p["checks"]["LOGS-001"].update(scenario="capabilities"),
        ):
            raw = copy.deepcopy(self.d.raw)
            mutate(raw)
            policy = FixturePolicy.parse(raw)
            with self.assertRaises(ValueError):
                validate_target(policy, FixtureRequest.parse(self.d.request), self.d.recipe)

    def test_failed_capability_or_behavior_never_passes_assessment(self):
        with patch("sre_suite.demo_fixture.controller.CAPABILITIES", {"grpc": True}):
            artifact = self.d.runner.execute(self.d.request)
        self.d.f.now = self.d.f.end
        proof = self.d.runner.publish(self.d.request, artifact)
        with self.assertRaises(ValueError), patch.object(self.d.f.v.assessor, "upload") as upload:
            self.d.assessor.assess(self.d.request, proof)
        upload.assert_not_called()

    def test_real_workload_telemetry_contains_correlated_safe_identity_and_bounded_metrics(self):
        self.d.runner.execute(self.d.request)
        out = {row[1]: json.loads(row[2]) for row in self.rows("outbox")}
        resource = out["traces"]["resourceSpans"][0]
        spans = resource["scopeSpans"][0]["spans"]
        logs = [json.loads(row[1]) for row in out["logs"]["streams"][0]["values"]]
        self.assertEqual(len(spans), 7)
        self.assertEqual(len(logs), 6)
        self.assertEqual(len({s["traceId"] for s in spans}), 1)
        self.assertTrue(all(log["span_id"] in {s["spanId"] for s in spans} for log in logs))
        self.assertEqual(sum(s.get("status", {}).get("code") == 2 for s in spans), 1)
        self.assertEqual([e["message"] for e in logs if e["case"] == "unicode"], ["Hello, 世界 🌾"])
        self.assertNotIn("DEMO_SECRET_CANARY", canonical(out))
        metrics = self.d.controller.metrics()
        self.assertIn('generation="1"', metrics)
        self.assertIn('plan_digest="sha256:', metrics)
        self.assertEqual(
            sum(line.startswith("sre_demo_requests_total{") for line in metrics.splitlines()), 6
        )
        self.assertNotIn(self.d.controller.trace_id, metrics)
        self.assertNotIn("DEMO_SECRET_CANARY", metrics)

    def test_export_retries_only_unacknowledged_batches_without_reexecuting_service(self):
        self.d.runner.execute(self.d.request)

        class Transport:
            def __init__(self, signal):
                self.signal, self.calls, self.fail = signal, [], False

            def call(self, method, path, payload):
                self.calls.append((method, path, payload))
                if self.fail:
                    self.fail = False
                    raise RemoteError(0)
                return {} if self.signal == "traces" else None

        clients = {s: Transport(s) for s in ("logs", "traces")}
        clients["traces"].fail = True
        journal = Journal(self.root / "export-journal")
        with self.assertRaises(RemoteError):
            flush(self.d.controller, clients, journal)
        self.d.controller = self.d.reopen()
        self.assertEqual(flush(self.d.controller, clients, journal), 1)
        self.assertEqual(flush(self.d.controller, clients, journal), 0)
        self.assertEqual(len(clients["logs"].calls), 1)
        self.assertEqual(clients["traces"].calls[0], clients["traces"].calls[1])
        self.assertEqual(len(self.rows("executions")), 3)

    def test_partial_otlp_acceptance_and_warning_never_mark_delivered(self):
        for value in (
            None,
            {"partialSuccess": {"rejectedSpans": "1"}},
            {"partialSuccess": {"errorMessage": "warning"}},
            {"partialSuccess": {"rejectedSpans": False}},
            {"unknown": True},
        ):
            with self.assertRaises(ValueError):
                acknowledge("traces", value)
        acknowledge("traces", {})
        acknowledge("traces", {"partialSuccess": {"rejectedSpans": "0"}})
        with self.assertRaises(ValueError):
            acknowledge("logs", {"status": "success"})

    def test_private_preparation_is_exclusive_and_no_implicit_start(self):
        output = self.root / "expectations.json"
        with (
            patch("sys.argv", ["demo", "expectations-template", "--output", str(output)]),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(), 0)
            self.assertEqual(main(), 1)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(json.loads(output.read_text())), set(self.d.expectations))
        with (
            patch(
                "sys.argv",
                [
                    "demo",
                    "export",
                    "--target",
                    "/missing",
                    "--state",
                    str(self.root),
                    "--export-config",
                    "/missing",
                ],
            ),
            redirect_stdout(io.StringIO()),
            patch("sre_suite.demo_fixture.__main__.client") as client,
        ):
            self.assertEqual(main(), 1)
        client.assert_not_called()

    def test_export_credentials_are_signal_specific_and_https_only(self):
        config = {
            s: {
                "base_url": "https://ingest.example.invalid",
                "authorization_env": "SRE_DEMO_" + s.upper() + "_AUTH",
                "tenant_env": None,
                "ca_env": None,
            }
            for s in ("traces", "logs")
        }
        env = {"SRE_DEMO_TRACES_AUTH": "Bearer traces", "SRE_DEMO_LOGS_AUTH": "Bearer logs"}
        self.assertEqual(set(transports(config, env)), {"traces", "logs"})
        for mutate in (
            lambda c: c["traces"].update(base_url="http://insecure.invalid"),
            lambda c: c["logs"].update(authorization_env="SRE_JIRA_API_TOKEN"),
            lambda c: c["traces"].update(base_url="https://ingest.example.invalid/path"),
        ):
            raw = copy.deepcopy(config)
            mutate(raw)
            with self.assertRaises(ValueError):
                transports(raw, env)

    def test_emitted_otlp_and_loki_roundtrip_through_real_adapters_and_correlation_predicate(self):
        from dataclasses import asdict

        from sre_suite.contracts import Release
        from sre_suite.telemetry.adapters import TelemetryAdapter
        from sre_suite.telemetry.contracts import epoch_ns
        from sre_suite.verification.predicates import evaluate
        from sre_suite.verification.records import records
        from tests.test_telemetry import Source

        self.d.runner.execute(self.d.request)
        out = {row[1]: json.loads(row[2]) for row in self.rows("outbox")}
        release = self.d.request["plan"]["release"]
        release = Release(
            self.d.policy.verification.scope,
            release["artifact_digest"],
            release["plan_digest"],
            ("certification",),
        )
        captured = {}
        for source in self.d.policy.verification.sources:
            if source.profile.backend == "prometheus":
                continue
            raw = (
                {"trace": out["traces"]}
                if source.profile.backend == "tempo"
                else {
                    "status": "success",
                    "data": {"resultType": "streams", "result": out["logs"]["streams"]},
                }
            )
            request = source.request(release, self.d.f.start, self.d.f.end)
            result = TelemetryAdapter(source.profile, Source(raw)).query(request)
            self.assertEqual(result.status, "observed")
            captured[source.profile.backend] = records(asdict(result), request)
        self.assertTrue(
            evaluate(
                "correlation",
                {"trace_path": "/body/trace_id", "span_path": "/body/span_id"},
                captured["loki"],
                [],
                epoch_ns(self.d.f.start),
                epoch_ns(self.d.f.end),
                captured["tempo"],
            )
        )
        captured["loki"][0]["body"]["span_id"] = "e" * 16
        self.assertFalse(
            evaluate(
                "correlation",
                {"trace_path": "/body/trace_id", "span_path": "/body/span_id"},
                captured["loki"],
                [],
                epoch_ns(self.d.f.start),
                epoch_ns(self.d.f.end),
                captured["tempo"],
            )
        )

    def test_prepare_pin_validate_commands_preserve_private_bound_inputs(self):
        def write(name, value):
            path = self.root / name
            path.write_text(canonical(value))
            path.chmod(0o600)
            return str(path)

        verification = write("verification.json", self.d.verification)
        expectations = write("oracles.json", self.d.expectations)
        source = write("recipe.json", self.d.recipe)
        request = write("request.json", self.d.request)
        policy = self.root / "policy.json"
        target = self.root / "target.json"
        calls = [
            [
                "prepare",
                "--verification-policy",
                verification,
                "--expectations",
                expectations,
                "--target-origin",
                self.d.raw["target"]["base_url"],
                "--runner",
                "runner",
                "--output",
                str(policy),
            ],
            [
                "pin",
                "--policy",
                str(policy),
                "--request",
                request,
                "--recipe",
                source,
                "--output",
                str(target),
            ],
            ["validate", "--target", str(target)],
        ]
        for args in calls:
            with patch("sys.argv", ["demo", *args]), redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 0)
        self.assertEqual(json.loads(policy.read_text()), self.d.raw)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.rows("executions"), [])

"""Measured values, missing-data behavior, durable retry accounting and private mappings."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sre_suite.demo_fixture.export import flush
from sre_suite.demo_fixture.instrumentation import process_metrics
from sre_suite.demo_fixture.service import handle
from sre_suite.execution.journal import Journal
from sre_suite.local_telemetry.demo_policy import reviewed
from sre_suite.local_telemetry.gateway import retention_seconds
from sre_suite.telemetry.contracts import epoch_ns
from sre_suite.verification.contracts import HEALTH_CHECKS, VerificationPolicy
from sre_suite.verification.predicates import evaluate
from tests.demo_fixture import DemoFixture
from tests.managed_fixture import ManagedFixture


class InstrumentationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def fixture(self):
        fixture = DemoFixture(self.root)
        self.addCleanup(fixture.close)
        return fixture

    def test_exception_frames_are_actual_bounded_and_do_not_leak_paths_or_locals(self):
        with patch("linecache.getlines", side_effect=AssertionError("source IO forbidden")):
            _, _, event = handle("GET", "/error", "a" * 32)
        self.assertEqual(event["exception_type"], "RuntimeError")
        self.assertTrue(0 < len(event["exception_stack"]) <= 8)
        self.assertEqual(event["exception_stack"][0]["function"], "handle")
        line = event["exception_stack"][0]["line"]
        from sre_suite.demo_fixture.controller import service_source

        self.assertIn(
            'raise RuntimeError("controlled demo failure")', service_source().splitlines()[line - 1]
        )
        self.assertEqual(set(event["exception_stack"][0]), {"function", "line"})
        self.assertNotIn(str(self.root), json.dumps(event))

    def test_process_measurements_parse_os_units_and_failed_os_reads_are_absent(self):
        with (
            patch("sre_suite.demo_fixture.instrumentation.sys.platform", "linux"),
            patch("pathlib.Path.read_text", return_value="100 40 20"),
            patch("os.sysconf", return_value=4096),
            patch("os.listdir", return_value=["0", "1", "2", "123456"]),
        ):
            measurements = process_metrics(0)
        self.assertEqual(measurements["sre_demo_process_resident_memory_bytes"], 40 * 4096)
        self.assertGreater(measurements["sre_demo_process_open_fds"], 0)
        with (
            patch("sre_suite.demo_fixture.instrumentation.sys.platform", "linux"),
            patch("pathlib.Path.read_text", side_effect=OSError),
            patch("os.listdir", side_effect=OSError),
        ):
            measurements = process_metrics(0)
        self.assertNotIn("sre_demo_process_resident_memory_bytes", measurements)
        self.assertNotIn("sre_demo_process_open_fds", measurements)
        self.assertGreaterEqual(measurements["sre_demo_process_cpu_seconds_total"], 0)

    def test_export_acknowledgements_are_durable_and_missing_ack_never_has_latency(self):
        d = self.fixture()
        d.runner.execute(d.request)
        self.assertNotIn("sre_demo_export_ack_latency_seconds", d.controller.metrics())
        clients = {"logs": Mock(), "traces": Mock()}
        clients["logs"].call.return_value = None
        clients["traces"].call.side_effect = [ValueError("lost acknowledgement"), {}]
        journal = Journal(self.root / "export")
        with self.assertRaises(ValueError):
            flush(d.controller, clients, journal)
        with d.controller.connect() as conn:
            rows = dict(
                (r[0], r[1:])
                for r in conn.execute(
                    "SELECT signal,attempts,failures,acknowledged_ns FROM delivery"
                )
            )
        self.assertEqual(rows["traces"], (1, 1, None))
        self.assertEqual(rows["logs"], (1, 0, epoch_ns(d.f.now)))
        d.controller = d.reopen()
        self.assertEqual(flush(d.controller, clients, journal), 1)
        self.assertEqual(flush(d.controller, clients, journal), 0)
        with d.controller.connect() as conn:
            trace = conn.execute(
                "SELECT attempts,failures,acknowledged_ns FROM delivery WHERE signal='traces'"
            ).fetchone()
        self.assertEqual(trace, (2, 1, epoch_ns(d.f.now)))
        self.assertEqual(
            clients["traces"].call.call_args_list[0], clients["traces"].call.call_args_list[1]
        )

    def test_partial_otlp_rejection_records_failure_without_acknowledgement(self):
        d = self.fixture()
        d.runner.execute(d.request)
        clients = {"logs": Mock(), "traces": Mock()}
        clients["logs"].call.return_value = None
        clients["traces"].call.return_value = {"partialSuccess": {"rejectedSpans": "1"}}
        with self.assertRaises(ValueError):
            flush(d.controller, clients, Journal(self.root / "partial"))
        with d.controller.connect() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT failures,acknowledged_ns FROM delivery WHERE signal='traces'"
                ).fetchone(),
                (1, None),
            )
            self.assertEqual(
                conn.execute("SELECT delivered FROM outbox WHERE signal='traces'").fetchone(), (0,)
            )

    def test_outbox_audit_detects_missing_payload_items(self):
        d = self.fixture()
        d.runner.execute(d.request)
        with d.controller.connect() as conn:
            payload = json.loads(
                conn.execute("SELECT payload FROM outbox WHERE signal='logs'").fetchone()[0]
            )
            payload["streams"][0]["values"].pop()
            conn.execute("UPDATE outbox SET payload=? WHERE signal='logs'", (json.dumps(payload),))
        lines = d.controller.metrics().splitlines()
        damaged = [
            line
            for line in lines
            if line.startswith("sre_demo_outbox_missing_items{") and 'signal="logs"' in line
        ]
        self.assertEqual(len(damaged), 1)
        self.assertTrue(damaged[0].endswith(" 1"))

    def test_export_generation_fence_does_not_publish_ack_after_remote_acceptance(self):
        d = self.fixture()
        d.runner.execute(d.request)
        clients = {"logs": Mock(), "traces": Mock()}

        def accepted(*args):
            d.controller.guard = Mock(side_effect=ValueError("stale generation"))
            return None

        clients["logs"].call.side_effect = accepted
        with self.assertRaises(ValueError):
            flush(d.controller, clients, Journal(self.root / "fenced"))
        with d.controller.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM outbox WHERE delivered=1").fetchone()[0], 0
            )
            self.assertEqual(
                conn.execute("SELECT acknowledged_ns FROM delivery").fetchone(), (None,)
            )
        clients["traces"].call.assert_not_called()

    def test_effective_retention_probe_rejects_missing_disabled_ambiguous_and_wrong_values(self):
        valid = (
            b"compactor:\n  retention_enabled: true\n  delete_request_store: filesystem\n"
            b"limits_config:\n  retention_period: 24h\n"
        )
        self.assertEqual(retention_seconds(valid), 86400)
        for changed in (
            b"",
            valid.replace(b"true", b"false"),
            valid.replace(b"24h", b"0s"),
            valid.replace(b"filesystem", b"unknown"),
            valid + valid,
            valid.replace(
                b"  retention_enabled: true",
                b"  retention_enabled: true\n  retention_enabled: false",
            ),
        ):
            with self.assertRaises(ValueError):
                retention_seconds(changed)

    def test_sample_freshness_uses_each_evaluation_time_and_rejects_stale_or_future(self):
        rows = [
            {
                "samples": [
                    {"timestamp_ns": 100_000_000_000, "value": 99},
                    {"timestamp_ns": 200_000_000_000, "value": 199},
                ]
            }
        ]
        params = {"maximum_seconds": 3, "minimum_samples": 2}
        self.assertTrue(evaluate("sample_age", params, rows, [], 0, 200_000_000_000, []))
        for value in (90, 101):
            rows[0]["samples"][0]["value"] = value
            self.assertFalse(evaluate("sample_age", params, rows, [], 0, 200_000_000_000, []))

    def test_reviewed_policy_has_all_mappings_and_requires_metric_backend_for_health(self):
        f = ManagedFixture(self.root)
        self.addCleanup(f.close)
        # Local recording rules require their explicit, documented label contract.
        metric = next(
            s
            for s in f.config["fixture_policy"]["verification_policy"]["sources"]
            if s["profile"]["backend"] == "prometheus"
        )
        metric["profile"]["scope_labels"].update(
            service="service_name",
            environment="deployment_environment",
            revision="service_version",
            environment_id="preview_id",
            generation="preview_generation",
        )
        raw = reviewed(f.config)["fixture_policy"]["verification_policy"]
        policy = VerificationPolicy.parse(raw)
        self.assertEqual(len(policy.sources), 6)
        self.assertEqual(sum(r.applicable for r in policy.rules), 76)
        for key in HEALTH_CHECKS:
            changed = copy.deepcopy(raw)
            changed["checks"][key]["source"] = "tempo" if key.startswith("TRACES") else "loki"
            with self.assertRaises(ValueError):
                VerificationPolicy.parse(changed)
        with self.assertRaises(ValueError):
            reviewed(reviewed(f.config))

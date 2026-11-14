import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sre_suite.execution.journal import Journal
from sre_suite.fixtures.journey_traces import publish, validate
from tests.test_target_service import identity


class JourneyTraceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.journal = Journal(Path(temp.name).resolve() / "journal")
        self.configuration = dict(
            base_url="https://tempo.example.invalid",
            authorization_env="SRE_JOURNEY_TRACES_AUTH",
            ca_env=None,
        )
        self.env = {"SRE_JOURNEY_TRACES_AUTH": "Bearer synthetic-trace-only"}
        self.responses = [
            {
                "binding": {"artifact": "synthetic-reviewed-binding"},
                "result": {
                    "steps": [
                        dict(
                            client_trace=dict(
                                trace_id="a" * 32,
                                span_id="b" * 16,
                                start_ns=100,
                                end_ns=200,
                                method="GET",
                                path="/health",
                                status=200,
                            )
                        )
                    ]
                },
            }
        ]

    def test_exports_measured_client_hierarchy_and_preserves_service_identity_separation(self):
        guard = Mock()
        with patch("sre_suite.fixtures.journey_traces.JsonHttp") as factory:
            factory.return_value.call.return_value = {"partialSuccess": {}}
            publish(self.configuration, self.env, identity(), self.responses, self.journal, guard)
            payload = factory.return_value.call.call_args.args[2]
            spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
            self.assertEqual(spans[1]["spanId"], "b" * 16)
            self.assertEqual(spans[1]["parentSpanId"], spans[0]["spanId"])
            self.assertEqual(
                (spans[0]["startTimeUnixNano"], spans[0]["endTimeUnixNano"]), ("100", "200")
            )
            attrs = {
                r["key"]: r["value"]["stringValue"]
                for r in payload["resourceSpans"][0]["resource"]["attributes"]
            }
            self.assertEqual(attrs, identity() | {"service.name": "sre-fixture-runner"})
            self.assertNotIn("synthetic-trace-only", str(payload))
            publish(self.configuration, self.env, identity(), self.responses, self.journal, guard)
            self.assertEqual(factory.return_value.call.call_count, 1)
            guard.side_effect = ValueError("generation advanced")
            with self.assertRaises(ValueError):
                publish(
                    self.configuration, self.env, identity(), self.responses, self.journal, guard
                )
            self.assertEqual(factory.return_value.call.call_count, 1)

    def test_partial_ack_retries_identical_bytes_and_changed_measurement_cannot_reuse_receipt(self):
        with patch("sre_suite.fixtures.journey_traces.JsonHttp") as factory:
            http = factory.return_value
            http.call.return_value = {"partialSuccess": {"rejectedSpans": "1"}}
            with self.assertRaises(ValueError):
                publish(
                    self.configuration,
                    self.env,
                    identity(),
                    self.responses,
                    self.journal,
                    lambda: None,
                )
            first = copy.deepcopy(http.call.call_args.args)
            http.call.return_value = {}
            publish(
                self.configuration, self.env, identity(), self.responses, self.journal, lambda: None
            )
            self.assertEqual(first, http.call.call_args.args)
            self.responses[0]["result"]["steps"][0]["client_trace"]["end_ns"] = 201
            with self.assertRaises(ValueError):
                publish(
                    self.configuration,
                    self.env,
                    identity(),
                    self.responses,
                    self.journal,
                    lambda: None,
                )

    def test_export_origin_and_credentials_are_explicit_and_separate(self):
        self.assertEqual(validate(self.configuration), self.configuration)
        for change in (
            {"base_url": "http://tempo.invalid"},
            {"base_url": "https://tempo.invalid/v1/traces"},
            {"authorization_env": "SRE_EVIDENCE_API_TOKEN"},
            {"ca_env": "SRE_EVIDENCE_CA_FILE"},
        ):
            with self.assertRaises(ValueError):
                validate(self.configuration | change)

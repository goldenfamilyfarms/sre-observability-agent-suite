"""Full Tempo V2 fixtures: OTLP preservation, identity and explicit partial semantics."""

import base64
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from sre_suite.execution.http import RemoteError
from sre_suite.telemetry.adapters import TelemetryAdapter, TelemetryError
from sre_suite.telemetry.collector import TelemetryCollector
from sre_suite.telemetry.contracts import Backend, ObservationStatus, QueryPolicy, epoch_ns
from tests.execution_fixture import Fixture
from tests.test_foundation import NOW
from tests.test_telemetry import SCOPE, Source, profile, query

TRACE_ID = "a" * 32
SPAN_ID = "b" * 16


def trace_profile():
    return replace(profile(Backend.TEMPO), allow_full_traces=True)


def trace_response(scope=SCOPE):
    return {
        "status": "COMPLETE",
        "trace": {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": k, "value": {"stringValue": v}}
                            for k, v in trace_profile().bindings(scope).items()
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "synthetic-instrumentation", "version": "1.0"},
                            "spans": [
                                {
                                    "traceId": TRACE_ID,
                                    "spanId": SPAN_ID,
                                    "name": "GET /demo",
                                    "startTimeUnixNano": str(epoch_ns(NOW) - 1_000_000_000),
                                    "endTimeUnixNano": str(epoch_ns(NOW)),
                                    "kind": "SPAN_KIND_SERVER",
                                    "status": {"code": "STATUS_CODE_OK"},
                                    "attributes": [
                                        {
                                            "key": "http.response.status_code",
                                            "value": {"intValue": "200"},
                                        }
                                    ],
                                    "events": [
                                        {"name": "synthetic", "timeUnixNano": str(epoch_ns(NOW))}
                                    ],
                                    "links": [{"traceId": "c" * 32, "spanId": "d" * 16}],
                                    "flags": 1,
                                }
                            ],
                        }
                    ],
                    "schemaUrl": "https://example.invalid/schema",
                }
            ]
        },
    }


def span(raw):
    return raw["trace"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]


class FullTraceTests(unittest.TestCase):
    def fetch(self, raw=None, **changes):
        source = Source(trace_response() if raw is None else raw)
        request = replace(query(Backend.TEMPO), trace_id=TRACE_ID, **changes)
        result = TelemetryAdapter(trace_profile(), source).query(request)
        return result, source

    def test_full_payload_and_typed_spans_preserved(self):
        raw = trace_response()
        result, source = self.fetch(raw)
        self.assertEqual(json.loads(result.full_trace.otlp_json), raw["trace"])
        self.assertEqual(result.full_trace.spans[0].span_id, SPAN_ID)
        self.assertTrue(result.full_trace.spans[0].matches_scope)
        self.assertEqual(result.status, ObservationStatus.OBSERVED)
        url = urlsplit(source.calls[0][1])
        self.assertEqual(url.path, "/api/v2/traces/" + TRACE_ID)
        self.assertEqual(parse_qs(url.query)["span_pruning"], ["false"])
        self.assertNotIn("q", parse_qs(url.query))

    def test_protobuf_omitted_complete_status(self):
        raw = trace_response()
        raw.pop("status")
        self.assertEqual(self.fetch(raw)[0].status, ObservationStatus.OBSERVED)

    def test_legacy_batches_scope_groups_and_base64_ids(self):
        raw = trace_response()
        record = span(raw)
        for field, identity in (("traceId", TRACE_ID), ("spanId", SPAN_ID)):
            record[field] = base64.b64encode(bytes.fromhex(identity)).decode()
        resource = raw["trace"]["resourceSpans"][0]
        resource["instrumentationLibrarySpans"] = resource.pop("scopeSpans")
        raw["trace"]["batches"] = raw["trace"].pop("resourceSpans")
        result, _ = self.fetch(raw)
        self.assertEqual(result.full_trace.trace_id, TRACE_ID)
        self.assertEqual(result.full_trace.spans[0].span_id, SPAN_ID)

    def test_partial_pruned_and_dropped_results_cannot_be_observed(self):
        for field, value in (
            ("status", "PARTIAL"),
            ("status", 1),
            ("pruneStatus", "PRUNED_ON_WRITE"),
            ("prune_status", 2),
            ("message", "private-backend-diagnostic"),
        ):
            raw = trace_response()
            raw[field] = value
            result, _ = self.fetch(raw)
            self.assertEqual(result.status, ObservationStatus.PARTIAL)
            self.assertNotIn("private-backend-diagnostic", result.full_trace.otlp_json)
        raw = trace_response()
        span(raw)["droppedEventsCount"] = 1
        self.assertIn("dropped_data", self.fetch(raw)[0].issues)

    def test_full_fetch_requires_separate_operator_permission(self):
        source = Source(trace_response())
        request = replace(query(Backend.TEMPO), trace_id=TRACE_ID)
        with self.assertRaises(TelemetryError):
            TelemetryAdapter(profile(Backend.TEMPO), source).query(request)
        with self.assertRaises(ValueError):
            QueryPolicy(SCOPE, (profile(Backend.TEMPO),)).resolve(request)
        self.assertFalse(source.calls)

    def test_wrong_trace_and_invalid_span_identity(self):
        for field, value in (
            ("traceId", "f" * 32),
            ("spanId", "bad"),
            ("spanId", "0" * 16),
            ("traceId", "a" * 16),
            ("parentSpanId", "oops"),
        ):
            raw = trace_response()
            span(raw)[field] = value
            with self.assertRaises(TelemetryError):
                self.fetch(raw)

    def test_request_rejects_path_injection_and_short_identity(self):
        for value in ("../secret", "a" * 16, "0" * 32, "A" * 32):
            with self.assertRaises(ValueError):
                replace(query(Backend.TEMPO), trace_id=value)

    def test_requires_matching_resource_scope_and_window(self):
        for label in trace_profile().bindings(SCOPE):
            raw = trace_response()
            for attribute in raw["trace"]["resourceSpans"][0]["resource"]["attributes"]:
                if attribute["key"] == label:
                    attribute["value"] = {"stringValue": "unrelated"}
            with self.assertRaisesRegex(TelemetryError, "scope_mismatch"):
                self.fetch(raw)
        raw = trace_response()
        span(raw)["startTimeUnixNano"] = str(epoch_ns(NOW) + 1)
        span(raw)["endTimeUnixNano"] = str(epoch_ns(NOW) + 2)
        with self.assertRaisesRegex(TelemetryError, "scope_mismatch"):
            self.fetch(raw)

    def test_cross_service_context_is_preserved_but_not_claimed_as_matching(self):
        raw = trace_response()
        resource = copy.deepcopy(raw["trace"]["resourceSpans"][0])
        resource["resource"]["attributes"] = [
            {"key": "service_name", "value": {"stringValue": "upstream"}}
        ]
        child = resource["scopeSpans"][0]["spans"][0]
        child.update(spanId="c" * 16, parentSpanId=SPAN_ID)
        raw["trace"]["resourceSpans"].append(resource)
        result, _ = self.fetch(raw)
        self.assertEqual([s.matches_scope for s in result.full_trace.spans], [True, False])

    def test_duplicate_span_and_cycle_rejected_missing_parent_partial(self):
        raw = trace_response()
        records = raw["trace"]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        records.append(copy.deepcopy(records[0]))
        with self.assertRaises(TelemetryError):
            self.fetch(raw)
        records[1].update(spanId="c" * 16, parentSpanId=SPAN_ID)
        records[0]["parentSpanId"] = "c" * 16
        with self.assertRaises(TelemetryError):
            self.fetch(raw)
        raw = trace_response()
        span(raw)["parentSpanId"] = "c" * 16
        self.assertIn("missing_parent", self.fetch(raw)[0].issues)

    def test_malformed_and_over_budget_trace_rejected(self):
        for raw in (
            {"trace": {}},
            {"trace": {"resourceSpans": []}},
            {"status": "UNKNOWN"},
            {"status": True},
            {"trace": {"resourceSpans": [], "batches": []}},
        ):
            with self.assertRaises(TelemetryError):
                self.fetch(raw)
        raw = trace_response()
        records = raw["trace"]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        second = copy.deepcopy(records[0])
        second.update(spanId="c" * 16, parentSpanId=SPAN_ID)
        records.append(second)
        with self.assertRaises(TelemetryError):
            self.fetch(raw, limit=1)

    def test_duplicate_attributes_and_invalid_duration_rejected(self):
        raw = trace_response()
        attrs = raw["trace"]["resourceSpans"][0]["resource"]["attributes"]
        attrs.append(copy.deepcopy(attrs[0]))
        with self.assertRaises(TelemetryError):
            self.fetch(raw)

        raw = trace_response()
        span(raw)["endTimeUnixNano"] = "0"
        with self.assertRaises(TelemetryError):
            self.fetch(raw)

    def test_invalid_anyvalue_and_pruning_status_are_rejected(self):
        for value in (
            {"boolValue": "false"},
            {"intValue": True},
            {"doubleValue": "NaN"},
            {"bytesValue": "bad"},
            {"stringValue": 42},
            {"unknown": "data"},
        ):
            raw = trace_response()
            span(raw)["attributes"] = [{"key": "synthetic", "value": value}]
            with self.assertRaises(TelemetryError):
                self.fetch(raw)
        raw = trace_response()
        raw["pruneStatus"] = "UNKNOWN"
        with self.assertRaises(TelemetryError):
            self.fetch(raw)

    def test_full_trace_evidence_upload_and_generation_fence(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            try:
                scope = fixture.ticket.scope
                source = Source(trace_response(scope))
                collector = TelemetryCollector(
                    QueryPolicy(scope, (trace_profile(),)),
                    fixture.evidence,
                    lambda p: source,
                    clock=lambda: NOW,
                )
                request = replace(query(Backend.TEMPO, scope), trace_id=TRACE_ID)
                collection = collector.collect(request, upload_evidence=True)
                fixture.evidence.verify(collection.evidence)
                self.assertEqual(
                    json.loads(collection.content)["attribution"],
                    "matching-resource-spans-with-cross-service-context",
                )
                source.callback = lambda: fixture.registry.start(
                    replace(fixture.preview, scope=replace(scope, generation=2)), 1, NOW
                )
                with self.assertRaises(RemoteError):
                    collector.collect(request, upload_evidence=True)
            finally:
                fixture.close()


if __name__ == "__main__":
    unittest.main()

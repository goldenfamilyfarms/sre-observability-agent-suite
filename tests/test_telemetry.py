"""Synthetic source protocols and real authenticated evidence API generation fences."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from sre_suite.api_wire import decode
from sre_suite.demo import example_ticket
from sre_suite.evidence import EvidenceStore
from sre_suite.execution.http import RemoteError
from sre_suite.execution.wire import canonical
from sre_suite.memory import Finding, KnowledgeStore, MemoryKind
from sre_suite.telemetry.__main__ import main
from sre_suite.telemetry.adapters import TelemetryAdapter, TelemetryError, transport
from sre_suite.telemetry.collector import TelemetryCollector
from sre_suite.telemetry.contracts import (
    Backend,
    ObservationStatus,
    QueryPolicy,
    QueryProfile,
    QueryRequest,
    epoch_ns,
)
from tests.execution_fixture import Fixture
from tests.test_foundation import NOW

SCOPE = example_ticket(NOW).scope


def profile(backend):
    names = (
        "service_name",
        "deployment_environment",
        "service_version",
        "preview_id",
        "preview_generation",
    )
    return QueryProfile(
        backend.value,
        backend,
        "https://telemetry.example.invalid",
        "SRE_TELEMETRY_AUTH",
        "SRE_TELEMETRY_TENANT",
        "up${scope}" if backend == Backend.PROMETHEUS else "${scope}",
        tuple(
            zip(
                ("service", "environment", "revision", "environment_id", "generation"),
                names,
                strict=True,
            )
        ),
    )


def query(backend=Backend.PROMETHEUS, scope=SCOPE, **kwargs):
    return QueryRequest(scope, backend.value, NOW - timedelta(minutes=5), NOW, **kwargs)


def response(backend, scope=SCOPE):
    identity = profile(backend).bindings(scope)
    if backend == Backend.PROMETHEUS:
        return {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {"metric": identity, "values": [[NOW.timestamp(), "1.25"]]},
                ],
            },
        }
    if backend == Backend.LOKI:
        return {
            "status": "success",
            "data": {
                "resultType": "streams",
                "result": [
                    {
                        "stream": identity,
                        "values": [
                            [str(epoch_ns(NOW) - 1), "synthetic log", {"trace_id": "a" * 32}]
                        ],
                    },
                ],
            },
        }
    return {
        "traces": [
            {
                "traceID": "a" * 32,
                "rootServiceName": "upstream-service",
                "rootTraceName": "GET /demo",
                "startTimeUnixNano": str(epoch_ns(NOW) - 1),
                "durationMs": 10,
            }
        ],
        "metrics": {"inspectedTraces": 42},
    }


class Source:
    def __init__(self, raw, callback=lambda: None):
        self.raw = raw
        self.calls = []
        self.callback = callback

    def call(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        self.callback()
        if isinstance(self.raw, Exception):
            raise self.raw
        return copy.deepcopy(self.raw)


class AdapterTests(unittest.TestCase):
    def run_query(self, backend, raw=None, request=None):
        source = Source(response(backend) if raw is None else raw)
        result = TelemetryAdapter(profile(backend), source).query(request or query(backend))
        self.assertEqual(source.calls[0][0], "GET")
        self.assertIsNone(source.calls[0][2])
        return result, urlsplit(source.calls[0][1])

    def test_prometheus_range_protocol_and_typed_result(self):
        result, url = self.run_query(Backend.PROMETHEUS)
        self.assertEqual(url.path, "/api/v1/query_range")
        params = parse_qs(url.query)
        self.assertEqual(params["query"], [profile(Backend.PROMETHEUS).query(SCOPE)])
        self.assertEqual(params["timeout"], ["15s"])
        self.assertEqual(params["step"], ["15"])
        self.assertEqual(result.metrics[0].samples[0].value, 1.25)
        self.assertEqual(result.status, ObservationStatus.OBSERVED)

    def test_loki_nanoseconds_end_exclusive_and_metadata(self):
        result, url = self.run_query(Backend.LOKI)
        params = parse_qs(url.query)
        self.assertEqual(url.path, "/loki/api/v1/query_range")
        self.assertEqual(params["end"], [str(epoch_ns(NOW))])
        self.assertEqual(params["direction"], ["forward"])
        self.assertEqual(result.logs[0].timestamp_ns, epoch_ns(NOW) - 1)
        self.assertEqual(result.logs[0].metadata, (("trace_id", "a" * 32),))

    def test_tempo_search_is_partial_even_below_limit(self):
        result, url = self.run_query(Backend.TEMPO)
        self.assertEqual(url.path, "/api/search")
        self.assertEqual(parse_qs(url.query)["q"], [profile(Backend.TEMPO).query(SCOPE)])
        self.assertEqual(result.status, ObservationStatus.PARTIAL)
        self.assertIn("search_subset", result.issues)
        self.assertEqual(result.traces[0].root_service, "upstream-service")

    def test_tempo_requires_whole_second_window(self):
        with self.assertRaisesRegex(TelemetryError, "whole_seconds"):
            self.run_query(
                Backend.TEMPO,
                request=replace(query(Backend.TEMPO), end=NOW - timedelta(microseconds=1)),
            )

    def test_empty_is_not_success(self):
        for backend in (Backend.PROMETHEUS, Backend.LOKI):
            raw = response(backend)
            raw["data"]["result"] = []
            result, _ = self.run_query(backend, raw)
            self.assertEqual(result.status, ObservationStatus.EMPTY)
        result, _ = self.run_query(Backend.TEMPO, {"metrics": {}})
        self.assertEqual(result.status, ObservationStatus.PARTIAL)
        self.assertFalse(result.traces)

    def test_limits_are_partial_not_implicitly_complete(self):
        for backend in Backend:
            result, _ = self.run_query(backend, request=query(backend, limit=1))
            self.assertEqual(result.status, ObservationStatus.PARTIAL)
            self.assertIn("result_limit", result.issues)

    def test_warning_or_info_is_partial_and_text_is_not_forwarded(self):
        for backend in (Backend.PROMETHEUS, Backend.LOKI):
            for key in ("warnings", "infos"):
                raw = response(backend)
                raw[key] = ["private-backend-diagnostic"]
                result, _ = self.run_query(backend, raw)
                self.assertEqual(result.status, ObservationStatus.PARTIAL)
                self.assertNotIn("private-backend-diagnostic", canonical(asdict(result)))

    def test_mismatched_and_missing_scope_labels_rejected(self):
        for backend, field in ((Backend.PROMETHEUS, "metric"), (Backend.LOKI, "stream")):
            for label in profile(backend).bindings(SCOPE):
                for value in (None, "unrelated"):
                    raw = response(backend)
                    identity = raw["data"]["result"][0][field]
                    if value is None:
                        identity.pop(label)
                    else:
                        identity[label] = value
                    with self.subTest(backend=backend, label=label, value=value):
                        with self.assertRaisesRegex(TelemetryError, "scope_mismatch"):
                            self.run_query(backend, raw)

    def test_nonfinite_and_unsupported_samples_fail_closed(self):
        for value in ("NaN", "+Inf", "-Inf", "1e9999", True, 1, None):
            raw = response(Backend.PROMETHEUS)
            raw["data"]["result"][0]["values"][0][1] = value
            with self.assertRaisesRegex(TelemetryError, "invalid_response"):
                self.run_query(Backend.PROMETHEUS, raw)
        raw = response(Backend.PROMETHEUS)
        raw["data"]["result"][0]["histograms"] = []
        with self.assertRaises(TelemetryError):
            self.run_query(Backend.PROMETHEUS, raw)

    def test_sample_order_duplicates_and_window(self):
        raw = response(Backend.PROMETHEUS)
        series = raw["data"]["result"][0]
        for values in (
            [],
            [[NOW.timestamp() + 1, "1"]],
            [[True, "1"]],
            [[NOW.timestamp(), "1"], [NOW.timestamp(), "2"]],
        ):
            series["values"] = values
            with self.assertRaises(TelemetryError):
                self.run_query(Backend.PROMETHEUS, raw)

    def test_loki_rejects_end_timestamp_large_lines_and_overflow(self):
        for value in (str(epoch_ns(NOW)), str(2**63), 1, "-1", True):
            raw = response(Backend.LOKI)
            raw["data"]["result"][0]["values"][0][0] = value
            with self.assertRaises(TelemetryError):
                self.run_query(Backend.LOKI, raw)
        raw = response(Backend.LOKI)
        raw["data"]["result"][0]["values"][0][1] = "x" * 16385
        with self.assertRaises(TelemetryError):
            self.run_query(Backend.LOKI, raw)

    def test_equal_log_timestamps_are_preserved_without_deduplication(self):
        raw = response(Backend.LOKI)
        values = raw["data"]["result"][0]["values"]
        values.append(copy.deepcopy(values[0]))
        result, _ = self.run_query(Backend.LOKI, raw)
        self.assertEqual(len(result.logs), 2)

    def test_duplicate_stream_series_and_trace_rejected(self):
        for backend in Backend:
            raw = response(backend)
            rows = raw["traces"] if backend == Backend.TEMPO else raw["data"]["result"]
            rows.append(copy.deepcopy(rows[0]))
            with self.assertRaises(TelemetryError):
                self.run_query(backend, raw)

    def test_tempo_trace_id_and_duration_validation(self):
        for field, value in (
            ("traceID", "../admin"),
            ("traceID", "0" * 32),
            ("durationMs", True),
            ("durationMs", -1),
        ):
            raw = response(Backend.TEMPO)
            raw["traces"][0][field] = value
            with self.assertRaises(TelemetryError):
                self.run_query(Backend.TEMPO, raw)

    def test_tempo_unpadded_hex_ids_are_normalized(self):
        raw = response(Backend.TEMPO)
        raw["traces"][0]["traceID"] = "2f3e0cee77ae5dc9c17ade3689eb2e54"
        result, _ = self.run_query(Backend.TEMPO, raw)
        self.assertEqual(result.traces[0].trace_id, raw["traces"][0]["traceID"].zfill(32))

    def test_http_and_backend_errors_are_redacted(self):
        for code in (0, 401, 403, 404, 429, 500, 503):
            source = Source(RemoteError(code))
            with self.assertRaisesRegex(
                TelemetryError, "transport_error" if not code else f"http_{code}"
            ):
                TelemetryAdapter(profile(Backend.PROMETHEUS), source).query(query())
            self.assertEqual(len(source.calls), 1)  # No implicit retries or source fallback.
        for backend in Backend:
            with self.assertRaisesRegex(TelemetryError, "backend_query_error"):
                self.run_query(backend, {"status": "error", "error": "SECRET"})

    def test_malformed_complex_and_oversized_responses(self):
        for raw in ([], {}, {"garbage": "x" * 500_001}, {"garbage": [0] * 10001}):
            for backend in Backend:
                with self.assertRaises(TelemetryError):
                    self.run_query(backend, raw)

    def test_query_encoding_cannot_add_parameters_or_traverse(self):
        selected = replace(profile(Backend.LOKI), query_template='${scope} |~ "a..b&limit=999"')
        source = Source(response(Backend.LOKI))
        TelemetryAdapter(selected, source).query(query(Backend.LOKI))
        path = source.calls[0][1]
        self.assertNotIn("..", path)
        params = parse_qs(urlsplit(path).query)
        self.assertEqual(params["query"], [selected.query(SCOPE)])
        self.assertEqual(params["limit"], ["100"])

    def test_scope_substitution_quotes_untrusted_values(self):
        scope = replace(SCOPE, service='bad"} or up{job="other')
        rendered = profile(Backend.LOKI).query(scope)
        self.assertIn("service_name=" + json.dumps(scope.service), rendered)

    def test_private_auth_and_single_tenant_headers(self):
        env = {"SRE_TELEMETRY_AUTH": "Bearer synthetic-secret", "SRE_TELEMETRY_TENANT": "demo"}
        with patch("sre_suite.telemetry.adapters.JsonHttp") as http:
            transport(profile(Backend.LOKI), env)
            self.assertEqual(http.call_args.kwargs["headers"], {"X-Scope-OrgID": "demo"})
            self.assertEqual(http.call_args.args[1], env["SRE_TELEMETRY_AUTH"])
        for key, value in (
            ("SRE_TELEMETRY_AUTH", "Bearer bad\r\nInjected: yes"),
            ("SRE_TELEMETRY_TENANT", "a|b"),
            ("SRE_TELEMETRY_AUTH", ""),
        ):
            with self.assertRaises(TelemetryError):
                transport(profile(Backend.LOKI), {**env, key: value})

    def test_policy_and_request_strict_parsing(self):
        policy = QueryPolicy(SCOPE, tuple(profile(b) for b in Backend))
        raw = {"schema_version": 2, **asdict(policy)}
        raw["profiles"] = [
            {**asdict(p), "scope_labels": dict(p.scope_labels)} for p in policy.profiles
        ]
        self.assertEqual(QueryPolicy.parse(decode(canonical(raw).encode())).scope, SCOPE)
        request = {"schema_version": 2, **asdict(query())}
        self.assertEqual(QueryRequest.parse(decode(canonical(request).encode())), query())
        with self.assertRaises(ValueError):
            QueryRequest.parse({**request, "query": "up"})
        with self.assertRaises(ValueError):
            QueryRequest.parse({**request, "limit": True})

    def test_invalid_profiles_and_source_urls(self):
        selected = profile(Backend.LOKI)
        for kwargs in (
            {"base_url": "http://example.invalid"},
            {"base_url": "https://u:p@example.invalid"},
            {"base_url": "https://example.invalid?token=SECRET"},
            {"authorization_env": "ANTHROPIC_API_KEY"},
            {"query_template": "{}"},
            {"query_template": "${scope}${scope}"},
            {"scope_labels": ()},
        ):
            with self.assertRaises(ValueError):
                replace(selected, **kwargs)
        with self.assertRaises(ValueError):
            QueryPolicy(SCOPE, (selected, selected))

    def test_window_and_budget_validation(self):
        for kwargs in (
            {"end": NOW + timedelta(hours=2)},
            {"start": NOW},
            {"limit": 501},
            {"step_seconds": False},
            {"start": NOW.replace(tzinfo=None)},
        ):
            with self.assertRaises(ValueError):
                replace(query(), **kwargs)
        with self.assertRaises(ValueError):
            query().current(NOW - timedelta(seconds=1))
        with self.assertRaises(ValueError):
            query().current(NOW + timedelta(days=2))


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fixture = Fixture(Path(self.directory.name))
        self.addCleanup(self.fixture.close)
        self.scope = self.fixture.ticket.scope
        self.request = query(scope=self.scope)
        self.policy = QueryPolicy(self.scope, (profile(Backend.PROMETHEUS),))
        self.source = Source(response(Backend.PROMETHEUS, self.scope))
        self.collector = TelemetryCollector(
            self.policy, self.fixture.evidence, lambda p: self.source, clock=lambda: NOW
        )

    def supersede(self):
        preview = replace(self.fixture.preview, scope=replace(self.scope, generation=2))
        self.fixture.registry.start(preview, 1, NOW)

    def test_authenticated_evidence_upload_and_roundtrip(self):
        result = self.collector.collect(self.request, upload_evidence=True)
        self.fixture.evidence.verify(result.evidence)
        document = json.loads(result.content)
        self.assertEqual(result.evidence.scope, self.scope)
        self.assertIsNone(document["certification_verdict"])
        self.assertEqual(document["request"]["scope"]["generation"], 1)
        self.assertNotIn("synthetic-private-token", result.content.decode())

    def test_no_upload_without_explicit_option(self):
        with patch.object(
            self.fixture.evidence, "upload", wraps=self.fixture.evidence.upload
        ) as upload:
            result = self.collector.collect(self.request)
            self.assertIsNone(result.evidence)
            upload.assert_not_called()

    def test_wrong_scope_or_profile_makes_no_remote_call(self):
        for request in (
            replace(self.request, scope=replace(self.scope, generation=2)),
            replace(self.request, profile="unknown"),
        ):
            with self.assertRaises(ValueError):
                self.collector.collect(request)
        self.assertFalse(self.source.calls)

    def test_stale_generation_rejected_before_source_call(self):
        self.supersede()
        with self.assertRaises(RemoteError):
            self.collector.collect(self.request)
        self.assertFalse(self.source.calls)

    def test_terminated_preview_rejected_before_source_call(self):
        self.fixture.registry.terminate(self.scope, "synthetic closure", NOW)
        with self.assertRaises(RemoteError):
            self.collector.collect(self.request)
        self.assertFalse(self.source.calls)

    def test_knowledge_version_changed_during_query_rejected(self):
        path = str(Path(self.directory.name) / "evidence.db")
        store = EvidenceStore(path)
        knowledge = KnowledgeStore(path, store)
        self.addCleanup(store.close)
        self.addCleanup(knowledge.close)
        fact = Finding(
            "source-changed",
            self.scope,
            MemoryKind.SEMANTIC,
            "service",
            "uses",
            "http",
            self.fixture.ticket.evidence[0],
        )
        knowledge.propose_finding(fact)
        self.source.callback = lambda: knowledge.reconcile_finding(fact.id, 0, NOW)
        with patch.object(self.fixture.evidence, "upload") as upload:
            with self.assertRaises(RemoteError):
                self.collector.collect(self.request, upload_evidence=True)
            upload.assert_not_called()

    def test_generation_changed_during_query_cannot_publish(self):
        self.source.callback = self.supersede
        with patch.object(self.fixture.evidence, "upload") as upload:
            with self.assertRaises(RemoteError):
                self.collector.collect(self.request, upload_evidence=True)
            upload.assert_not_called()

    def test_generation_changed_before_upload_rejected_by_api(self):
        original = self.fixture.evidence.upload

        def race(*args):
            self.supersede()
            return original(*args)

        with patch.object(self.fixture.evidence, "upload", side_effect=race):
            with self.assertRaises(RemoteError):
                self.collector.collect(self.request, upload_evidence=True)

    def test_generation_changed_after_upload_cannot_report_success(self):
        original = self.fixture.evidence.upload

        def race(*args):
            reference = original(*args)
            self.supersede()
            return reference

        with patch.object(self.fixture.evidence, "upload", side_effect=race):
            with self.assertRaises(RemoteError):
                self.collector.collect(self.request, upload_evidence=True)

    def test_cli_redacts_failures(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(
                [
                    "--policy",
                    "/does-not-exist-SECRET",
                    "--request",
                    "/missing",
                    "--output",
                    "/missing",
                ],
                env={},
            )
        self.assertEqual(result, 1)
        self.assertNotIn("SECRET", stdout.getvalue() + stderr.getvalue())

    def test_cli_writes_private_bundle_and_returns_partial_nonzero(self):
        root = Path(self.directory.name).resolve()
        root.chmod(0o700)
        policy = {
            "schema_version": 2,
            "scope": asdict(self.scope),
            "profiles": [
                {**asdict(p), "scope_labels": dict(p.scope_labels)} for p in self.policy.profiles
            ],
        }
        for name, value in (
            ("policy.json", policy),
            ("request.json", {"schema_version": 2, **asdict(self.request)}),
        ):
            (root / name).write_text(canonical(value))
            (root / name).chmod(0o600)
        collection = self.collector.collect(self.request)
        partial = replace(
            collection, result=replace(collection.result, status=ObservationStatus.PARTIAL)
        )
        with patch("sre_suite.telemetry.__main__.TelemetryCollector") as factory:
            factory.return_value.collect.return_value = partial
            with redirect_stdout(io.StringIO()):
                result = main(
                    [
                        "--policy",
                        str(root / "policy.json"),
                        "--request",
                        str(root / "request.json"),
                        "--output",
                        str(root / "output"),
                    ],
                    env=self.fixture.env,
                )
        self.assertEqual(result, 2)
        self.assertEqual((root / "output" / "observation.json").stat().st_mode & 0o777, 0o600)
        self.assertFalse((root / "output" / "evidence.json").exists())


if __name__ == "__main__":
    unittest.main()

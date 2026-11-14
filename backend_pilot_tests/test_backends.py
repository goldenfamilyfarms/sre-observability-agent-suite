"""Actual Prometheus scraping, Loki/Tempo ingestion and scoped pilot evidence through Temporal."""

import asyncio
import json
import socket
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from orchestration_tests import test_managed_demo as managed
from sre_suite.demo_fixture.managed import ManagedPolicy
from sre_suite.demo_fixture.scenarios import prepare_policy
from sre_suite.execution.http import JsonHttp, RemoteError
from sre_suite.execution.wire import canonical
from sre_suite.fixtures.contracts import FixturePolicy, FixtureRequest
from sre_suite.local_pilot.config import write_json, write_private
from sre_suite.local_telemetry.config import DEFAULT_PORTS, initialize
from sre_suite.local_telemetry.lifecycle import running
from sre_suite.previews import PreviewRegistry
from sre_suite.telemetry.adapters import transport
from sre_suite.telemetry.collector import TelemetryCollector
from sre_suite.telemetry.contracts import (
    CandidateBinding,
    ObservationStatus,
    QueryPolicy,
    QueryRequest,
)
from sre_suite.verification.records import records


def ports():
    sockets = [socket.socket() for _ in DEFAULT_PORTS]
    try:
        for s in sockets:
            s.bind(("127.0.0.1", 0))
        return dict(zip(DEFAULT_PORTS, (s.getsockname()[1] for s in sockets), strict=True))
    finally:
        for s in sockets:
            s.close()


class BackendHarness(managed.ManagedHarness):
    async def configure_backends(self):
        fixture = self.f.d.f.f
        now = datetime.now(UTC)
        manifest = {
            "schema_version": 2,
            "registration": {
                "schema_version": 2,
                "preview": asdict(fixture.preview),
                "expected_generation": 0,
            },
            "ticket_key": "DEMO-1",
            "run_id": "backend-pilot",
            "baseline": {
                "sha256": "d" * 64,
                "observed_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=30)).isoformat(),
            },
        }
        path = self.root / "manifest.json"
        write_json(path, manifest)
        token = self.root / "metrics.token"
        write_private(token, self.f.env["SRE_DEMO_METRICS_AUTH"].encode())
        self.stack = self.root / "backends"
        self.config = initialize(
            self.stack,
            path,
            f"https://localhost:{self.port}",
            self.cert,
            token,
            mode="native" if sys.platform == "linux" else "desktop",
            ports=ports(),
        )
        self.backend_context = None
        self.addAsyncCleanup(self.stop_backends)
        await self.start_backends()
        for source in self.f.d.verification["sources"]:
            p = source["profile"]
            backend = p["backend"]
            p.update(
                base_url=self.config.url(backend),
                ca_env="SRE_TELEMETRY_CA_FILE",
                tenant_env=None,
                authorization_env="SRE_TELEMETRY_" + backend.upper() + "_AUTH",
            )
            if backend == "prometheus":
                p["query_template"] = "sre_demo_requests_total${scope}"
        self.f.d.raw = prepare_policy(
            self.f.d.verification, self.f.d.expectations, self.f.d.raw["target"], "runner"
        )
        self.f.d.policy = FixturePolicy.parse(self.f.d.raw)
        self.f.d.runner.policy = self.f.d.assessor.policy = self.f.d.policy
        self.f.config.update(
            fixture_policy=self.f.d.raw,
            export=json.loads((self.stack / "export.json").read_text()),
            settle_seconds=3,
        )
        self.f.policy = ManagedPolicy(self.f.config)
        self.f.path.write_text(canonical(self.f.config))
        self.f.env.update(json.loads((self.stack / "export-env.json").read_text()))
        self.f.request["plan"].update(
            policy_sha256=self.f.d.policy.verification.fingerprint,
            fixture=self.f.d.policy.dispatch,
            demo=self.f.policy.dispatch,
        )
        self.f.request["dispatch"] = self.f.d.policy.dispatch
        self.collector_env = json.loads((self.stack / "collector-env.json").read_text())

    async def start_backends(self):
        self.backend_context = running(self.stack, self.config, authorized=True)
        await asyncio.to_thread(self.backend_context.__enter__)

    async def stop_backends(self):
        if self.backend_context:
            context, self.backend_context = self.backend_context, None
            await asyncio.to_thread(context.__exit__, None, None, None)

    def collect(self, binding=None):
        request = FixtureRequest.parse(self.f.request)
        sources = self.f.d.policy.verification.sources
        policy = QueryPolicy(request.scope, tuple(s.profile for s in sources))
        collector = TelemetryCollector(
            policy,
            self.f.d.f.engineering.runtime.evidence,
            lambda p: transport(p, self.collector_env),
        )
        # Use the collector identity already registered by the real evidence API fixture.
        actual = binding or CandidateBinding(
            request.binding["release"]["artifact_digest"], request.binding["release"]["plan_digest"]
        )
        result = {}
        for s in sources:
            q = QueryRequest(
                request.scope, s.profile.id, request.start, request.end, 1, 100, s.trace_id, actual
            )
            result[s.profile.backend] = (q, collector.collect(q, upload_evidence=True))
        return result


class BackendPilotTests(BackendHarness):
    async def test_real_scrape_export_query_evidence_restart_and_generation_fences(self):
        workers = self.workers()
        async with workers[0], workers[1], workers[2]:
            handle = await self.start()
            result = await handle.result()
        self.assertEqual(result["outcome"], "assessed")
        self.assert_stopped()
        collected = await asyncio.to_thread(self.collect)
        for q, observation in collected.values():
            self.assertEqual(observation.result.status, ObservationStatus.OBSERVED)
            self.assertIsNotNone(observation.evidence)
            self.assertEqual(observation.evidence.scope, q.scope)
        metrics = collected["prometheus"][1].result.metrics
        self.assertEqual(len(metrics), 6)
        self.assertTrue(all(any(s.value == 1 for s in m.samples) for m in metrics))
        logs = collected["loki"][1].result.logs
        trace = collected["tempo"][1].result.full_trace
        self.assertEqual(len(logs), 6)
        self.assertEqual(len(trace.spans), 7)
        ids = {s.span_id for s in trace.spans}
        self.assertTrue(all(json.loads(row.line)["span_id"] in ids for row in logs))
        self.assertTrue(all(json.loads(row.line)["trace_id"] == trace.trace_id for row in logs))
        for query, observation in collected.values():
            self.assertTrue(records(asdict(observation.result), query))
        # Wrong artifact/plan cannot reuse observations from this exact run.
        for field in ("artifact_digest", "plan_digest"):
            binding = collected["loki"][0].binding
            wrong = replace(binding, **{field: "sha256:" + "e" * 64})
            p = self.f.d.policy.verification.sources[1].profile
            from sre_suite.telemetry.adapters import TelemetryAdapter

            q = replace(collected["loki"][0], binding=wrong)
            self.assertEqual(
                TelemetryAdapter(p, transport(p, self.collector_env)).query(q).status,
                ObservationStatus.EMPTY,
            )
        tokens = json.loads((self.stack / "tokens.json").read_text())
        for backend, path in (("loki", "/loki/api/v1/push"), ("tempo", "/v1/traces")):
            http = JsonHttp(
                self.config.url(backend),
                tokens[backend + "_read"],
                local_ca_file=str(self.stack / "server.crt"),
            )
            with self.assertRaises(RemoteError) as error:
                http.call("POST", path, {})
            self.assertEqual(error.exception.status, 401)
        await self.stop_backends()
        await self.start_backends()
        recovered = await asyncio.to_thread(self.collect)
        self.assertEqual(len(recovered["loki"][1].result.logs), 6)
        self.assertEqual(len(recovered["tempo"][1].result.full_trace.spans), 7)
        self.assertEqual(len(recovered["prometheus"][1].result.metrics), 6)
        # The preview ID is enforced both by backend selectors and before collector I/O.
        p = self.f.d.policy.verification.sources[1].profile
        q = replace(
            collected["loki"][0],
            scope=replace(collected["loki"][0].scope, environment_id="preview-other"),
        )
        from sre_suite.telemetry.adapters import TelemetryAdapter

        self.assertEqual(
            TelemetryAdapter(p, transport(p, self.collector_env)).query(q).status,
            ObservationStatus.EMPTY,
        )
        with patch("backend_pilot_tests.test_backends.transport") as connect:
            collector = TelemetryCollector(
                QueryPolicy(self.config.scope, (p,)),
                self.f.d.f.engineering.runtime.evidence,
                connect,
            )
            with self.assertRaises(ValueError):
                collector.collect(q, upload_evidence=True)
            connect.assert_not_called()
        registry = PreviewRegistry(str(self.root / "evidence.db"))
        try:
            registry.start(
                replace(
                    self.f.d.f.f.preview,
                    scope=replace(self.f.d.policy.verification.scope, generation=2),
                ),
                1,
                datetime.now(UTC),
            )
        finally:
            registry.close()
        with (
            patch("backend_pilot_tests.test_backends.transport") as connect,
            self.assertRaises(RemoteError),
        ):
            self.collect()
        connect.assert_not_called()

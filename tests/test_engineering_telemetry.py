"""Candidate-bound telemetry integration with authenticated API and synthetic sources."""

import copy
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path

from sre_suite.execution.config import Settings
from sre_suite.execution.http import RemoteError
from sre_suite.execution.journal import Journal
from sre_suite.execution.runtime import ExecutionRuntime
from sre_suite.execution.telemetry import EngineeringTelemetry
from sre_suite.execution.wire import canonical, digest
from sre_suite.telemetry.adapters import TelemetryAdapter, TelemetryError
from sre_suite.telemetry.collector import TelemetryCollector
from sre_suite.telemetry.contracts import Backend, CandidateBinding, QueryPolicy, epoch_ns
from sre_suite.telemetry.schedule_contracts import TelemetrySchedule
from sre_suite.telemetry.scheduled_runtime import ScheduledRuntime
from tests.execution_fixture import Fixture
from tests.publication_fixture import publication_runtime
from tests.test_foundation import NOW
from tests.test_telemetry import Source, profile, query, response
from tests.test_tempo_traces import TRACE_ID, span, trace_response

BINDINGS = (("artifact_digest", "artifact_digest"), ("plan_digest", "plan_digest"))


def configure(fixture, backends=(Backend.PROMETHEUS,), *, publication=False, delay=0, window=1):
    profiles = tuple(
        replace(profile(b), binding_labels=BINDINGS, allow_full_traces=b == Backend.TEMPO)
        for b in backends
    )
    policy = QueryPolicy(fixture.ticket.scope, profiles)
    raw_policy = {
        "schema_version": 2,
        "scope": asdict(policy.scope),
        "profiles": [
            {
                **asdict(p),
                "scope_labels": dict(p.scope_labels),
                "binding_labels": dict(p.binding_labels),
            }
            for p in profiles
        ],
    }
    config = {
        "schema_version": 2,
        "policy": raw_policy,
        "profiles": [
            {"profile": p.id, "trace_id": TRACE_ID if p.backend == Backend.TEMPO else None}
            for p in profiles
        ],
        "task_queue": "synthetic-telemetry",
        "producer": "worker-one",
        "delay_seconds": delay,
        "window_seconds": window,
    }
    path = fixture.root / "engineering-telemetry.json"
    path.write_text(canonical(config))
    path.chmod(0o600)
    original = publication_runtime(fixture) if publication else (fixture.runtime, None, None, None)
    old = original[0]
    runtime = ExecutionRuntime(
        replace(old.settings, telemetry_config=str(path)),
        jira=old.jira,
        github=old.github,
        evidence=fixture.evidence,
        tests=old.tests,
        clock=lambda: NOW,
    )
    return runtime, policy, config, path, original[1:]


def run_stages(runtime, spec):
    reference = runtime.prepare(spec)
    results = []
    for role in ("implement", "review", "test", "qa"):
        results.append(runtime.stage({"reference": reference, "role": role, "results": results}))
    return {"reference": reference, "results": results}


def source_payload(request, profile):
    scope = request.scope
    labels = profile.bindings(scope, request.binding)
    if profile.backend == Backend.TEMPO:
        raw = trace_response(scope)
        raw["trace"]["resourceSpans"][0]["resource"]["attributes"] = [
            {"key": k, "value": {"stringValue": v}} for k, v in labels.items()
        ]
        span(raw)["startTimeUnixNano"] = str(epoch_ns(request.start))
        span(raw)["endTimeUnixNano"] = str(epoch_ns(request.end))
        return raw
    raw = response(profile.backend, scope)
    row = raw["data"]["result"][0]
    if profile.backend == Backend.PROMETHEUS:
        row["metric"] = labels
        row["values"] = [[request.end.timestamp(), "1.25"]]
    else:
        row["stream"] = labels
        row["values"] = [[str(epoch_ns(request.end) - 1), "synthetic log"]]
    return raw


class EngineeringTelemetryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fixture = Fixture(Path(temporary.name).resolve())
        self.addCleanup(self.fixture.close)
        self.runtime, self.policy, self.config, self.path, _ = configure(self.fixture)
        self.arguments = run_stages(self.runtime, self.fixture.spec)
        self.plan = self.runtime.prepare_telemetry(self.arguments)

    def collect(self, *, mutate=None):
        reports = []
        for entry in self.plan["schedules"]:
            schedule = TelemetrySchedule.parse(entry["schedule"])
            now = schedule.first_run_at
            self.runtime.clock = lambda now=now: now
            self.fixture.app._clock = lambda now=now: now
            profile = self.policy.resolve(schedule.request)
            raw = source_payload(schedule.request, profile)
            if mutate:
                mutate(raw)
            collector = TelemetryCollector(
                self.policy,
                self.fixture.evidence,
                lambda p, raw=raw: Source(raw),
                clock=lambda now=now: now,
            )
            runtime = ScheduledRuntime(collector, Journal(self.fixture.root / "telemetry-journal"))
            receipt = runtime.collect({"schedule": schedule.wire(), "slot": 0})
            reports.append({"outcome": "collected", "collections": [receipt]})
        self.arguments["telemetry"] = {"plan": self.plan, "reports": reports}
        return reports

    def test_all_three_sources_bound_end_to_end(self):
        # A separate execution journal ensures the changed policy cannot reuse old stages.
        self.fixture.spec["run_id"] = "all-backends"
        self.fixture.env["SRE_WORK_ROOT"] = str(self.fixture.root / "all-journal")
        self.fixture.runtime = ExecutionRuntime(
            Settings.from_env(self.fixture.env),
            jira=self.fixture.jira,
            evidence=self.fixture.evidence,
            tests=self.runtime.tests,
            clock=lambda: NOW,
        )
        self.runtime, self.policy, _, _, _ = configure(self.fixture, tuple(Backend))
        self.arguments = run_stages(self.runtime, self.fixture.spec)
        self.plan = self.runtime.prepare_telemetry(self.arguments)
        self.collect()
        report = self.runtime.finish(self.arguments)
        self.assertEqual(report["outcome"], "ready_for_review")
        self.assertEqual(len(report["telemetry"]["evidence"]), 3)
        self.assertIsNone(report["telemetry"]["certification_verdict"])
        self.assertEqual(
            report["telemetry"]["binding"]["plan_digest"],
            "sha256:" + self.arguments["reference"]["manifest"],
        )

    def test_plan_retry_is_identical_and_does_not_shift_windows(self):
        self.runtime.clock = lambda: NOW + timedelta(seconds=20)
        self.assertEqual(self.plan, self.runtime.prepare_telemetry(self.arguments))
        self.assertNotIn("synthetic-private-token", canonical(self.plan))

    def test_missing_telemetry_or_stripped_marker_cannot_bypass_finish(self):
        self.arguments["reference"].pop("telemetry_required")
        with self.assertRaises(ValueError):
            self.runtime.finish(self.arguments)
        self.assertEqual(self.fixture.jira_http.puts, 0)

    def test_policy_change_cannot_reuse_manifest(self):
        self.config["delay_seconds"] = 10
        self.path.write_text(canonical(self.config))
        changed = ExecutionRuntime(
            self.runtime.settings,
            jira=self.runtime.jira,
            evidence=self.runtime.evidence,
            tests=self.runtime.tests,
            clock=lambda: NOW,
        )
        with self.assertRaises(ValueError):
            changed.prepare(self.fixture.spec)

    def test_tampered_plan_and_cross_execution_receipt_rejected(self):
        self.collect()
        for mutation in (
            lambda a: a["telemetry"]["plan"]["binding"].update(plan_digest="sha256:" + "a" * 64),
            lambda a: a["telemetry"]["plan"].update(implementation="b" * 64),
            lambda a: a["telemetry"].update(reports=[]),
            lambda a: a["telemetry"]["reports"][0]["collections"][0].update(
                evidence_sha256="c" * 64
            ),
        ):
            arguments = copy.deepcopy(self.arguments)
            mutation(arguments)
            with self.assertRaises(ValueError):
                self.runtime.finish(arguments)
        self.assertEqual(self.fixture.jira_http.puts, 0)

    def test_empty_partial_and_failed_collection_block_readiness(self):
        for result in ({"outcome": "blocked", "collections": []},):
            self.arguments["telemetry"] = {"plan": self.plan, "reports": [result]}
            self.assertEqual(self.runtime.finish(self.arguments)["outcome"], "blocked")
        # Separate test methods below exercise uploaded partial and empty observations.

    def test_partial_observation_is_not_ready(self):
        self.collect(mutate=lambda raw: raw.update(warnings=["synthetic warning"]))
        self.assertEqual(self.runtime.finish(self.arguments)["outcome"], "blocked")

    def test_empty_observation_is_not_ready(self):
        self.collect(mutate=lambda raw: raw["data"].update(result=[]))
        self.assertEqual(self.runtime.finish(self.arguments)["outcome"], "blocked")

    def test_wrong_artifact_label_rejected_before_upload(self):
        with self.assertRaises(TelemetryError):
            self.collect(
                mutate=lambda raw: raw["data"]["result"][0]["metric"].update(
                    artifact_digest="sha256:" + "e" * 64
                )
            )

    def test_generation_advance_and_expiry_rejected(self):
        self.collect()
        now = self.runtime.clock()
        self.runtime.clock = lambda now=now: now + timedelta(minutes=16)
        with self.assertRaises(ValueError):
            self.runtime.finish(self.arguments)
        self.runtime.clock = lambda now=now: now
        self.fixture.registry.start(
            replace(self.fixture.preview, scope=replace(self.policy.scope, generation=2)), 1, now
        )
        with self.assertRaises(RemoteError):
            self.runtime.finish(self.arguments)

    def test_candidate_recipe_changes_with_edits_and_preserves_base_identity(self):
        manifest = self.runtime.journal.read(
            self.arguments["reference"]["key"] + ":manifest",
            self.arguments["reference"]["manifest"],
        )
        implementation = self.runtime.journal.read(
            self.arguments["reference"]["key"] + ":implement",
            self.arguments["results"][0]["artifact"],
        )
        recipe = {
            "kind": "source-candidate-v1",
            "repository": self.runtime.settings.repository,
            "base_revision": manifest["ticket"]["scope"]["revision"],
            "edits": sorted(implementation["edits"], key=lambda e: e["path"]),
        }
        self.assertEqual(self.plan["binding"]["artifact_digest"], "sha256:" + digest(recipe))
        recipe["edits"][0]["content"] += "# changed\n"
        self.assertNotEqual(self.plan["binding"]["artifact_digest"], "sha256:" + digest(recipe))

    def test_forged_uploaded_binding_labels_are_rejected_from_actual_bytes(self):
        reports = self.collect()
        import hashlib

        from sre_suite.api import evidence_id
        from sre_suite.api_wire import decode, evidence_from_wire

        receipt = reports[0]["collections"][0]
        old = evidence_from_wire(receipt["evidence"])
        document = decode(self.runtime.evidence.read(old))
        document["result"]["metrics"][0]["labels"] = [
            [k, "sha256:" + "f" * 64 if k == "plan_digest" else v]
            for k, v in document["result"]["metrics"][0]["labels"]
        ]
        content = (canonical(document) + "\n").encode()
        sha = hashlib.sha256(content).hexdigest()
        forged = self.runtime.evidence.upload(
            "telemetry-" + sha, old.scope, content, old.observed_at, old.expires_at
        )
        self.assertEqual(forged.id, evidence_id("worker-one", old.scope, "telemetry-" + sha))
        receipt.update(
            evidence=json.loads(canonical(asdict(forged))),
            evidence_id=forged.id,
            evidence_sha256=sha,
        )
        with self.assertRaisesRegex(ValueError, "returned telemetry candidate"):
            self.runtime.finish(self.arguments)
        self.assertEqual(self.fixture.jira_http.puts, 0)

    def test_draft_published_before_collection_reused_after_lost_ack_and_head_fenced(self):
        self.fixture.spec["run_id"] = "publication-binding"
        self.runtime, self.policy, _, _, (github, jira, _) = configure(
            self.fixture, publication=True
        )
        self.arguments = run_stages(self.runtime, self.fixture.spec)
        github.lose_once = "/pulls"
        with self.assertRaises(RemoteError):
            self.runtime.prepare_telemetry(self.arguments)
        self.plan = self.runtime.prepare_telemetry(self.arguments)
        self.assertEqual(len(github.pulls), 1)
        self.assertEqual(jira.status, "200")
        self.collect()
        self.assertEqual(self.runtime.finish(self.arguments)["outcome"], "ready_for_review")
        self.assertEqual(jira.status, "300")
        self.assertEqual(len(github.pulls), 1)
        github.refs[self.plan["pull_request"]["branch"]] = "a" * 40
        with self.assertRaises(ValueError):
            self.runtime.finish(self.arguments)

    def test_config_requires_private_bound_profiles_and_full_tempo_traces(self):
        for mutation in (
            lambda c: c["policy"]["profiles"][0].update(binding_labels={}),
            lambda c: c.update(delay_seconds=True),
            lambda c: c["profiles"].append(c["profiles"][0]),
            lambda c: c["profiles"][0].update(trace_id=TRACE_ID),
        ):
            config = copy.deepcopy(self.config)
            mutation(config)
            self.path.write_text(canonical(config))
            with self.assertRaises(ValueError):
                EngineeringTelemetry(self.path)
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            EngineeringTelemetry(self.path)


class CandidateProtocolTests(unittest.TestCase):
    def test_all_backends_require_returned_candidate_and_plan_binding(self):
        binding = CandidateBinding("sha256:" + "a" * 64, "sha256:" + "b" * 64)
        for backend in Backend:
            p = replace(
                profile(backend),
                binding_labels=BINDINGS,
                allow_full_traces=backend == Backend.TEMPO,
            )
            request = replace(
                query(backend),
                binding=binding,
                trace_id=TRACE_ID if backend == Backend.TEMPO else None,
            )
            raw = source_payload(request, p)
            self.assertEqual(TelemetryAdapter(p, Source(raw)).query(request).status, "observed")
            for field in ("artifact_digest", "plan_digest"):
                mismatched = replace(
                    request, binding=replace(binding, **{field: "sha256:" + "c" * 64})
                )
                with self.assertRaises(TelemetryError):
                    TelemetryAdapter(p, Source(raw)).query(mismatched)
            with self.assertRaises(ValueError):
                replace(
                    p,
                    binding_labels=(
                        ("artifact_digest", "service_name"),
                        ("plan_digest", "plan_digest"),
                    ),
                )

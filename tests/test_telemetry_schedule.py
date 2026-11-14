"""Schedule bounds, saved capture retry and generation/context fences without a Temporal SDK."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from sre_suite.execution.http import RemoteError
from sre_suite.execution.journal import Journal
from sre_suite.execution.wire import canonical
from sre_suite.telemetry.collector import TelemetryCollector
from sre_suite.telemetry.contracts import Backend, QueryPolicy
from sre_suite.telemetry.schedule_child import main as child_main
from sre_suite.telemetry.schedule_config import child_environment, load_policy
from sre_suite.telemetry.schedule_contracts import TelemetrySchedule, policy_digest
from sre_suite.telemetry.scheduled_runtime import ScheduledRuntime
from sre_suite.telemetry.scheduling import main
from tests.execution_fixture import Fixture
from tests.test_foundation import NOW
from tests.test_telemetry import SCOPE, Source, profile, query, response


def schedule(policy, *, now=NOW, **kwargs):
    request = replace(query(scope=policy.scope), start=now - timedelta(minutes=5), end=now)
    return TelemetrySchedule(
        "pilot-observation", policy_digest(policy), request, now, 2, 2, 120, **kwargs
    )


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.fixture = Fixture(self.root)
        self.addCleanup(self.fixture.close)
        self.scope = self.fixture.ticket.scope
        self.policy = QueryPolicy(self.scope, (profile(Backend.PROMETHEUS),))
        self.source = Source(response(Backend.PROMETHEUS, self.scope))
        self.collector = TelemetryCollector(
            self.policy, self.fixture.evidence, lambda p: self.source, clock=lambda: NOW
        )
        self.runtime = ScheduledRuntime(self.collector, Journal(self.root / "scheduled"))
        self.schedule = schedule(self.policy)
        self.value = {"schedule": self.schedule.wire(), "slot": 0}

    def test_contract_roundtrip_deterministic_windows_and_identity(self):
        parsed = TelemetrySchedule.parse(self.schedule.wire())
        self.assertEqual(parsed, self.schedule)
        self.assertEqual(parsed.for_slot(1).end, NOW + timedelta(seconds=2))
        changed = replace(parsed, interval_seconds=60)
        self.assertEqual(changed.workflow_id, parsed.workflow_id)
        self.assertNotEqual(replace(parsed, schedule_id="another").workflow_id, parsed.workflow_id)

    def test_invalid_bounds_and_early_or_late_slots(self):
        for kwargs in (
            {"occurrences": 25},
            {"occurrences": True},
            {"interval_seconds": 0},
            {"max_lateness_seconds": 301},
            {"policy_sha256": "bad"},
            {"first_run_at": NOW - timedelta(seconds=1)},
        ):
            with self.assertRaises(ValueError):
                replace(self.schedule, **kwargs)
        for now in (NOW - timedelta(seconds=1), NOW + timedelta(seconds=121)):
            with self.assertRaises(ValueError):
                self.schedule.runnable(0, now)
        with self.assertRaises(ValueError):
            self.schedule.startable(NOW - timedelta(hours=2))

    def test_policy_change_wrong_generation_and_unapproved_profile_rejected(self):
        for policy in (
            replace(self.policy, scope=replace(self.scope, generation=2)),
            replace(
                self.policy,
                profiles=(replace(self.policy.profiles[0], query_template="other${scope}"),),
            ),
        ):
            with self.assertRaises(ValueError):
                self.schedule.authorize(policy)
        with self.assertRaises(ValueError):
            replace(
                self.schedule, request=replace(self.schedule.request, profile="unapproved")
            ).authorize(self.policy)

    def test_saved_capture_reused_after_lost_upload_response(self):
        original = self.fixture.evidence.upload
        attempts = []

        def lose_once(*args):
            attempts.append(args)
            reference = original(*args)
            if len(attempts) == 1:
                raise RemoteError(0)
            return reference

        with patch.object(self.fixture.evidence, "upload", side_effect=lose_once):
            with self.assertRaises(RemoteError):
                self.runtime.collect(self.value)
            result = self.runtime.collect(self.value)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(len(self.source.calls), 1)
        self.assertEqual(result["status"], "observed")
        self.assertNotIn("query", result)
        self.assertEqual(set(result), {"slot", "status", "evidence_id", "evidence_sha256"})

    def test_lost_activity_ack_reuses_capture_on_new_runtime(self):
        first = self.runtime.collect(self.value)
        restarted = ScheduledRuntime(self.collector, Journal(self.root / "scheduled"))
        self.assertEqual(restarted.collect(self.value), first)
        self.assertEqual(len(self.source.calls), 1)

    def test_stale_generation_blocks_saved_result_reuse(self):
        self.runtime.collect(self.value)
        self.fixture.registry.start(
            replace(self.fixture.preview, scope=replace(self.scope, generation=2)), 1, NOW
        )
        with self.assertRaises(RemoteError):
            self.runtime.collect(self.value)
        self.assertEqual(len(self.source.calls), 1)

    def test_changed_schedule_cannot_overwrite_occurrence(self):
        self.runtime.collect(self.value)
        value = {"schedule": replace(self.schedule, interval_seconds=60).wire(), "slot": 0}
        with self.assertRaises(ValueError):
            self.runtime.collect(value)
        self.assertEqual(len(self.source.calls), 1)

    def test_partial_is_recorded_as_partial_not_pass(self):
        self.source.raw["warnings"] = ["synthetic warning"]
        result = self.runtime.collect(self.value)
        self.assertEqual(result["status"], "partial")
        self.assertNotIn("passed", canonical(result))

    def test_private_capture_is_atomic_and_tampering_is_rejected(self):
        self.runtime.collect(self.value)
        files = list((self.root / "scheduled").glob("*.json"))
        self.assertEqual(len(files), 2)
        for file in files:
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
            data = json.loads(file.read_text())
            if "content" in data["result"]:
                data["result"]["content"] = "tampered"
                file.write_text(canonical(data))
        with self.assertRaises(ValueError):
            self.runtime.collect(self.value)

    def test_child_environment_excludes_unrelated_and_temporal_credentials(self):
        env = {
            "SRE_TELEMETRY_AUTH": "Bearer synthetic",
            "SRE_TEMPORAL_API_KEY": "secret",
            "ANTHROPIC_API_KEY": "secret",
            "SRE_JIRA_API_TOKEN": "secret",
            "PYTHONPATH": "bad",
            "SRE_EVIDENCE_API_TOKEN": "scoped",
            "PATH": "/usr/bin",
        }
        child = child_environment(self.policy, env)
        self.assertEqual(set(child), {"SRE_TELEMETRY_AUTH", "SRE_EVIDENCE_API_TOKEN", "PATH"})

    def test_offline_fingerprint_needs_no_temporal_sdk_or_credentials(self):
        file = self.root / "policy.json"
        file.write_text(
            canonical(
                {
                    "schema_version": 2,
                    "scope": asdict(self.scope),
                    "profiles": [
                        {**asdict(p), "scope_labels": dict(p.scope_labels)}
                        for p in self.policy.profiles
                    ],
                }
            )
        )
        file.chmod(0o600)
        stream = io.StringIO()
        with (
            patch("sys.argv", ["scheduling", "fingerprint", "--policy", str(file)]),
            redirect_stdout(stream),
        ):
            self.assertEqual(main(), 0)
        self.assertEqual(json.loads(stream.getvalue())["policy_sha256"], policy_digest(self.policy))
        file.chmod(0o644)
        with self.assertRaises(ValueError):
            load_policy(file)

    def test_policy_scope_hash_contains_preview_identity(self):
        first = QueryPolicy(SCOPE, (profile(Backend.PROMETHEUS),))
        second = replace(first, scope=replace(SCOPE, environment_id="another-preview"))
        self.assertNotEqual(policy_digest(first), policy_digest(second))

    def test_child_entrypoint_publishes_only_compact_receipt(self):
        path = self.root / "policy.json"
        path.write_text(
            canonical(
                {
                    "schema_version": 2,
                    "scope": asdict(self.scope),
                    "profiles": [
                        {**asdict(p), "scope_labels": dict(p.scope_labels)}
                        for p in self.policy.profiles
                    ],
                }
            )
        )
        path.chmod(0o600)

        class Input:
            buffer = io.BytesIO(canonical(self.value).encode())

        stream = io.StringIO()
        with (
            patch(
                "sys.argv",
                ["child", "--policy", str(path), "--journal", str(self.root / "scheduled")],
            ),
            patch("sys.stdin", Input()),
            redirect_stdout(stream),
            patch(
                "sre_suite.telemetry.schedule_child.collector_from_env", return_value=self.collector
            ),
        ):
            self.assertEqual(child_main(), 0)
        result = json.loads(stream.getvalue())
        self.assertEqual(result["status"], "observed")
        self.assertNotIn("rendered_query", stream.getvalue())
        self.assertEqual(len(self.source.calls), 1)

    def test_child_error_codes_are_redacted_and_retry_classified(self):
        for status, expected in ((0, 75), (429, 75), (503, 75), (403, 1)):
            stream = io.StringIO()
            with (
                patch(
                    "sys.argv",
                    ["child", "--policy", "/private/missing", "--journal", "/private/missing"],
                ),
                patch(
                    "sre_suite.telemetry.schedule_child.load_policy",
                    side_effect=RemoteError(status),
                ),
                redirect_stdout(stream),
            ):
                self.assertEqual(child_main(), expected)
            self.assertEqual(stream.getvalue(), "")


if __name__ == "__main__":
    unittest.main()

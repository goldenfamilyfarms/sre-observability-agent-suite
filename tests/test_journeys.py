"""Generic journey execution never repeats an uncertain POST or imports service instructions."""

import tempfile
import unittest
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from sre_suite.demo import example_ticket
from sre_suite.execution.http import RemoteError
from sre_suite.execution.journal import Journal
from sre_suite.fixtures.journeys import JourneyTarget, validate


class JourneyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        now = datetime.now(UTC)
        self.scope = example_ticket(now).scope
        self.configuration = validate(
            {
                "origins": {
                    "service": {
                        "base_url": "https://service.example.invalid",
                        "authorization_env": "SRE_JOURNEY_SERVICE_AUTH",
                        "ca_env": None,
                    }
                },
                "routes": {
                    "/journey": [
                        {
                            "origin": "service",
                            "method": "POST",
                            "path": "/operation",
                            "payload": {"synthetic": True},
                            "success_status": 200,
                        }
                    ]
                },
            },
            {"/journey"},
        )

        class Evidence:
            def context(inner, scope, version):
                if scope != self.scope or version != 0:
                    raise ValueError("scope differs")

        self.target = JourneyTarget(
            self.configuration,
            "a" * 64,
            {"SRE_JOURNEY_SERVICE_AUTH": "Bearer synthetic"},
            Evidence(),
            Journal(Path(directory.name).resolve() / "journal"),
        )
        self.command = {
            "schema_version": 2,
            "binding": {
                "release": {"scope": asdict(self.scope)},
                "artifact_kind": "container-image-v1",
                "context_version": 0,
                "window_start": (now - timedelta(seconds=1)).isoformat(),
                "window_end": (now + timedelta(seconds=60)).isoformat(),
            },
            "policy_sha256": "a" * 64,
            "scenario": "journey",
            "idempotency_key": "key",
            "trace_ids": ["b" * 32],
            "payload": {},
        }

    def test_success_reuses_observed_response_without_repeating_post(self):
        with patch("sre_suite.fixtures.journeys.JsonHttp.call", return_value={"ok": True}) as call:
            result = self.target.call("POST", "/journey", self.command)
            self.assertEqual(result, self.target.call("POST", "/journey", self.command))
            self.assertEqual(call.call_count, 1)
            self.assertEqual(result["result"]["steps"][0]["body"], {"ok": True})

    def test_lost_response_requires_reconciliation_instead_of_repeating_effect(self):
        with patch("sre_suite.fixtures.journeys.JsonHttp.call", side_effect=RemoteError(0)) as call:
            with self.assertRaises(RemoteError):
                self.target.call("POST", "/journey", self.command)
            with self.assertRaisesRegex(ValueError, "uncertain POST"):
                self.target.call("POST", "/journey", self.command)
            self.assertEqual(call.call_count, 1)

    def test_changed_generation_or_policy_blocks_before_network(self):
        with patch("sre_suite.fixtures.journeys.JsonHttp.call") as call:
            self.command["binding"]["release"]["scope"]["generation"] += 1
            with self.assertRaises(ValueError):
                self.target.call("POST", "/journey", self.command)
            self.assertFalse(call.called)
        self.command["policy_sha256"] = "changed"
        with self.assertRaises(ValueError):
            self.target.call("POST", "/journey", self.command)

    def test_query_redirect_paths_and_credential_names_are_not_configurable_in_payload(self):
        self.configuration["routes"]["/journey"][0]["path"] = "//another-host"
        with self.assertRaises(ValueError):
            validate(self.configuration, {"/journey"})

"""Prepare a real Git baseline and bind only matching uploaded evidence to a ticket."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from sre_suite.api import EvidenceContextApi
from sre_suite.api_storage import sqlite_backend
from sre_suite.auth import Action, TokenAuthenticator
from sre_suite.execution.evidence_client import EvidenceClient
from sre_suite.execution.pilot import PilotManifest, setup_pilot
from sre_suite.execution.wire import ticket_envelope
from sre_suite.pilot_demo.__main__ import main
from sre_suite.pilot_demo.prepare import (
    EXPECTED_REPORT,
    baseline_report,
    finalize,
    materialize,
    prepare,
    template,
)
from tests.test_api import AUDIENCE, SECOND_TOKEN, TOKEN, credential
from tests.test_foundation import NOW
from tests.test_pilot import Transport

REFERENCE_FIX = """    if (isinstance(trace_id, str) and len(trace_id) == 32
            and trace_id != "0" * 32
            and all(char in "0123456789abcdef" for char in trace_id)):
        event["trace_id"] = trace_id
"""
MARKER = "    # PILOT GAP: validated trace_id correlation is not implemented yet.\n"


class DemoFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "demo"
        materialize(self.repo)
        self.git("init", "--quiet")
        self.commit()
        self.prepared = self.root / "prepared"
        self.ticket_output = self.root / "ticket.json"

    def git(self, *args):
        return (
            subprocess.check_output(
                [
                    "git",
                    "-c",
                    "core.hooksPath=" + os.devnull,
                    "-c",
                    "user.name=Synthetic Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "-c",
                    "commit.gpgsign=false",
                    "-C",
                    str(self.repo),
                    *args,
                ],
                env={
                    "PATH": os.environ["PATH"],
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": os.devnull,
                },
                stderr=subprocess.PIPE,
            )
            .decode()
            .strip()
        )

    def commit(self):
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "Synthetic baseline")

    def plan(self, **changes):
        values = dict(
            checkout=self.repo,
            repository="example/demo",
            repository_id=1,
            pr_number=2,
            ticket_key="DEMO-1",
            generation=1,
            run_id="live-1",
            output=self.prepared,
            clock=lambda: NOW,
        )
        values.update(changes)
        prepare(**values)
        return json.loads((self.prepared / "manifest.json").read_text())

    def upload(self):
        manifest = self.plan()
        scope = PilotManifest.parse(manifest, NOW).preview.scope
        auth = TokenAuthenticator(
            AUDIENCE,
            (
                credential(scope),
                credential(scope, (Action.PREVIEW_REGISTER,), SECOND_TOKEN, "controller"),
            ),
        )
        app = EvidenceContextApi(
            auth,
            sqlite_backend(str(self.root / "api.db")),
            clock=lambda: NOW,
            allow_preview_registration=True,
        )
        worker = EvidenceClient("https://example.invalid", TOKEN, Transport(app, TOKEN))
        self.uploaded = self.root / "uploaded"
        setup_pilot(
            {
                "SRE_EVIDENCE_API_URL": "https://example.invalid",
                "SRE_EVIDENCE_API_TOKEN": TOKEN,
                "SRE_PILOT_SETUP_TOKEN": SECOND_TOKEN,
            },
            self.prepared / "manifest.json",
            self.prepared / "baseline.json",
            self.uploaded,
            clock=lambda: NOW,
            controller=Transport(app, SECOND_TOKEN),
            evidence=worker,
        )
        self.evidence_file = self.uploaded / "baseline-evidence.json"
        return scope


class DemoTests(DemoFixture):
    def test_materialized_repository_is_complete_and_never_overwritten(self):
        self.assertEqual(
            {
                str(p.relative_to(self.repo))
                for p in self.repo.rglob("*")
                if p.is_file() and ".git" not in p.parts
            },
            set(template()),
        )
        with self.assertRaises(ValueError):
            materialize(self.repo)

    def test_baseline_gap_is_expected_but_strict_candidate_gate_fails(self):
        revision, report = baseline_report(self.repo)
        self.assertEqual(revision, self.git("rev-parse", "HEAD"))
        self.assertEqual(report, EXPECTED_REPORT)
        result = subprocess.run(
            [sys.executable, "acceptance.py"], cwd=self.repo, capture_output=True
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout), EXPECTED_REPORT)

    def test_reference_change_passes_all_frozen_tests(self):
        app = self.repo / "app.py"
        original_tests = {
            name: (self.repo / name).read_bytes() for name in ("test_app.py", "acceptance.py")
        }
        app.write_text(app.read_text().replace(MARKER, REFERENCE_FIX))
        result = subprocess.run(
            [sys.executable, "acceptance.py"], cwd=self.repo, capture_output=True
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(
            json.loads(result.stdout),
            {"tests_run": 7, "failures": [], "errors": [], "skipped": 0, "passed": True},
        )
        self.assertEqual(
            original_tests, {name: (self.repo / name).read_bytes() for name in original_tests}
        )

    def test_unvalidated_context_and_hardcoded_id_are_rejected(self):
        app = self.repo / "app.py"
        original = app.read_text()
        for change in ('    event["trace_id"] = trace_id\n', '    event["trace_id"] = "1" * 32\n'):
            app.write_text(original.replace(MARKER, change))
            result = subprocess.run(
                [sys.executable, "acceptance.py"], cwd=self.repo, capture_output=True
            )
            self.assertEqual(result.returncode, 1)
            self.assertFalse(json.loads(result.stdout)["passed"])

    def test_source_or_test_changes_cannot_be_relabelled_as_reviewed_baseline(self):
        for name in ("app.py", "acceptance.py", "README.md"):
            with self.subTest(name=name):
                path = self.repo / name
                original = path.read_bytes()
                path.write_bytes(original + b"\n# edited\n")
                self.commit()
                with self.assertRaises(ValueError):
                    baseline_report(self.repo)
                path.write_bytes(original)
                self.commit()

    def test_extra_shadow_module_or_export_attributes_are_rejected(self):
        for name in ("json.py", ".gitattributes"):
            path = self.repo / name
            path.write_text("synthetic unexpected file")
            self.commit()
            with self.assertRaises(ValueError):
                baseline_report(self.repo)
            path.unlink()
            self.commit()

    def test_prepare_binds_revision_preview_and_private_disabled_worker_config(self):
        manifest = self.plan()
        parsed = PilotManifest.parse(manifest, NOW)
        self.assertEqual(parsed.preview.scope.revision, self.git("rev-parse", "HEAD"))
        self.assertEqual(parsed.preview.scope.environment_id, "preview-r1-pr2")
        worker = json.loads((self.prepared / "worker.json").read_text())
        self.assertEqual(json.loads(worker["SRE_ALLOWED_FILES_JSON"]), ["app.py"])
        self.assertEqual(json.loads(worker["SRE_TEST_COMMAND_JSON"]), ["python", "acceptance.py"])
        self.assertTrue(
            all(
                worker[k] == "false"
                for k in (
                    "SRE_CLAUDE_ENABLED",
                    "SRE_GITHUB_PRS_ENABLED",
                    "SRE_JIRA_WRITES_ENABLED",
                    "SRE_JIRA_TRANSITIONS_ENABLED",
                )
            )
        )
        with self.assertRaises(ValueError):
            ticket_envelope(json.loads((self.prepared / "ticket-draft.json").read_text()))
        for path in self.prepared.iterdir():
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        with self.assertRaises(ValueError):
            self.plan()

    def test_invalid_identity_run_window_or_base_fails_without_output(self):
        for change in (
            {"generation": True},
            {"generation": 0},
            {"repository_id": 0},
            {"pr_number": True},
            {"ticket_key": "bad"},
            {"run_id": "../bad"},
            {"repository": "bad"},
            {"minutes": 0},
            {"minutes": 241},
            {"base_branch": "sre/run-reserved"},
            {"base_branch": "bad//branch"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.plan(**change)
            self.assertFalse(self.prepared.exists())

    def test_finalized_ticket_roundtrips_with_actual_api_owned_evidence(self):
        scope = self.upload()
        ticket = finalize(self.prepared, self.evidence_file, self.ticket_output, clock=lambda: NOW)
        self.assertEqual(ticket.scope, scope)
        self.assertEqual(ticket.evidence[0].producer, "worker-one")
        self.assertEqual(ticket.repository, "example/demo")
        self.assertEqual(ticket_envelope(json.loads(self.ticket_output.read_text())), ticket)
        self.assertEqual(self.ticket_output.stat().st_mode & 0o077, 0)
        with self.assertRaises(FileExistsError):
            finalize(self.prepared, self.evidence_file, self.ticket_output, clock=lambda: NOW)

    def test_evidence_scope_digest_and_time_substitution_fail(self):
        self.upload()
        original = json.loads(self.evidence_file.read_text())
        for mutate in (
            lambda e: e["scope"].update(generation=2),
            lambda e: e["scope"].update(environment_id="preview-r1-pr3"),
            lambda e: e["scope"].update(revision="b" * 40),
            lambda e: e.update(sha256="0" * 64),
            lambda e: e.update(expires_at="2099-01-01T00:00:00Z"),
        ):
            value = json.loads(json.dumps(original))
            mutate(value["evidence"])
            self.evidence_file.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                finalize(self.prepared, self.evidence_file, self.ticket_output, clock=lambda: NOW)
            self.assertFalse(self.ticket_output.exists())

    def test_expired_manifest_or_corrupted_baseline_cannot_finalize(self):
        from datetime import timedelta

        self.upload()
        with self.assertRaises(ValueError):
            finalize(
                self.prepared,
                self.evidence_file,
                self.ticket_output,
                clock=lambda: NOW + timedelta(hours=3),
            )
        baseline = self.prepared / "baseline.json"
        baseline.write_bytes(baseline.read_bytes() + b" ")
        with self.assertRaises(ValueError):
            finalize(self.prepared, self.evidence_file, self.ticket_output, clock=lambda: NOW)

    def test_edited_draft_cannot_weaken_generated_acceptance_criteria(self):
        self.upload()
        (self.prepared / "ticket-draft.json").write_text('{"objective":"bypass tests"}')
        ticket = finalize(self.prepared, self.evidence_file, self.ticket_output, clock=lambda: NOW)
        self.assertIn("app.py only", ticket.objective)
        self.assertTrue(
            any("all seven tests" in criterion for criterion in ticket.acceptance_criteria)
        )

    def test_ticket_output_rejects_symlinks_public_paths_and_git_checkouts(self):
        self.upload()
        for path in (self.repo / "ticket.json", self.root / "alias.json"):
            if path.name == "alias.json":
                path.symlink_to(self.root / "unrelated.json")
            with self.assertRaises(ValueError):
                finalize(self.prepared, self.evidence_file, path, clock=lambda: NOW)
        self.root.chmod(0o755)
        try:
            with self.assertRaises(ValueError):
                finalize(self.prepared, self.evidence_file, self.ticket_output, clock=lambda: NOW)
        finally:
            self.root.chmod(0o700)

    def test_cli_errors_do_not_disclose_paths_or_raw_exception(self):
        error = io.StringIO()
        with (
            patch(
                "sys.argv",
                [
                    "demo",
                    "finalize-ticket",
                    "--prepared",
                    str(self.prepared),
                    "--evidence",
                    str(self.root / "secret-missing"),
                    "--output",
                    str(self.ticket_output),
                ],
            ),
            redirect_stderr(error),
            self.assertRaises(SystemExit) as stopped,
        ):
            main()
        self.assertEqual(stopped.exception.code, 1)
        self.assertNotIn(str(self.root), error.getvalue())

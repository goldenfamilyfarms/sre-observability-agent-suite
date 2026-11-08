"""Synthetic Jira and API fixtures; no external issue/account/model calls."""

import os
import subprocess
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path

from sre_suite.api import EvidenceContextApi
from sre_suite.api_storage import sqlite_backend
from sre_suite.auth import TokenAuthenticator
from sre_suite.demo import example_ticket
from sre_suite.evidence import EvidenceStore
from sre_suite.execution.config import Settings
from sre_suite.execution.evidence_client import EvidenceClient
from sre_suite.execution.http import RemoteError
from sre_suite.execution.jira import JiraClient
from sre_suite.execution.runtime import ExecutionRuntime
from sre_suite.execution.wire import canonical, ticket_json
from sre_suite.previews import PreviewRegistry, PreviewRequest
from tests.test_api import AUDIENCE, TOKEN, credential, request
from tests.test_foundation import NOW


class JiraTransport:
    def __init__(self, ticket):
        self.ticket = ticket
        self.properties = {}
        self.puts = 0
        self.lose_response = False

    def call(self, method, path, payload=None):
        if "/properties/" in path:
            if method == "GET":
                if path not in self.properties:
                    raise RemoteError(404)
                return {"value": self.properties[path]}
            self.puts += 1
            self.properties[path] = payload
            if self.lose_response:
                self.lose_response = False
                raise RemoteError(0)
            return None
        return {
            "key": self.ticket.key,
            "fields": {
                "customfield_12345": canonical(
                    {"schema_version": 2, "ticket": ticket_json(self.ticket)}
                ),
                "status": {"name": "Done"},
            },
        }


class ApiTransport:
    def __init__(self, app):
        self.app = app

    def call(self, method, path, payload=None):
        status, body, _ = request(self.app, path, payload)
        if status != 200:
            raise RemoteError(status)
        return body


class SyntheticTests:
    def execute(self, revision, edits, heartbeat=lambda: None):
        heartbeat()
        return {"passed": True, "exit_code": 0, "output": "synthetic assertion passed"}


class Fixture:
    def __init__(self, root: Path):
        self.root = root
        self.repo = root / "repo"
        self.repo.mkdir()
        (self.repo / "app.py").write_text("def answer():\n    return 1\n")
        (self.repo / "test_app.py").write_text(
            "import unittest\nfrom app import answer\n"
            "class Test(unittest.TestCase):\n    def test_answer(self):\n"
            "        self.assertEqual(answer(), 2)\n"
        )
        env = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        for command in (
            ["git", "init", "--quiet"],
            ["git", "add", "."],
            [
                "git",
                "-c",
                "user.name=Synthetic Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=" + os.devnull,
                "commit",
                "--quiet",
                "-m",
                "fixture",
            ],
        ):
            subprocess.run(command, cwd=self.repo, env=env, check=True, capture_output=True)
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, env=env, text=True
        ).strip()
        original = example_ticket(NOW)
        scope = replace(original.scope, revision=revision)
        self.ticket = replace(
            original, scope=scope, evidence=(replace(original.evidence[0], scope=scope),)
        )
        self.spec = {
            "schema_version": 2,
            "ticket_key": self.ticket.key,
            "scope": asdict(scope),
            "run_id": "fixture-1",
        }
        binary = root / "claude-fixture"
        binary.write_text("""#!/usr/bin/env python3
import json, os, sys
assert 'SRE_JIRA_API_TOKEN' not in os.environ
assert '--tools' in sys.argv and sys.argv[sys.argv.index('--tools')+1] == ''
assert '--restricted' in sys.argv and '--bare' in sys.argv
role = json.load(sys.stdin)['role']
result = {'verdict':'passed','summary':'Synthetic CLI fixture: '+role,'edits':[]}
if role == 'implement':
    result['edits'] = [{'path':'app.py','content':'def answer():\\n    return 2\\n'}]
print(json.dumps({'is_error':False,'subtype':'success','structured_output':result}))
""")
        binary.chmod(0o755)
        self.env = {
            "SRE_JIRA_BASE_URL": "https://jira.example.invalid",
            "SRE_JIRA_EMAIL": "fixture@example.invalid",
            "SRE_JIRA_API_TOKEN": "synthetic-private-token",
            "SRE_JIRA_PROJECT": "DEMO",
            "SRE_JIRA_CONTRACT_FIELD": "customfield_12345",
            "SRE_JIRA_DONE_STATUSES": "Done",
            "SRE_JIRA_WRITES_ENABLED": "true",
            "SRE_EVIDENCE_API_URL": "https://evidence.example.invalid",
            "SRE_EVIDENCE_API_TOKEN": TOKEN,
            "SRE_REPOSITORY": "example/demo-service",
            "SRE_REPO_DIR": str(self.repo),
            "SRE_WORK_ROOT": str(root / "private-journal"),
            "SRE_ALLOWED_FILES_JSON": '["app.py", "test_app.py", "new.py"]',
            "SRE_TEST_IMAGE": "python@sha256:" + "a" * 64,
            "SRE_TEST_COMMAND_JSON": '["python", "-m", "unittest", "discover", "-v"]',
            "SRE_CLAUDE_BINARY": str(binary),
            "SRE_CLAUDE_MODEL": "fixture-model",
            "ANTHROPIC_API_KEY": "synthetic-model-key",
            "SRE_CLAUDE_ENABLED": "true",
        }
        self.settings = Settings.from_env(self.env)
        path = str(root / "evidence.db")
        auth = TokenAuthenticator(AUDIENCE, (credential(scope),))
        self.app = EvidenceContextApi(auth, sqlite_backend(path), clock=lambda: NOW)
        store = EvidenceStore(path)
        store.put(self.ticket.evidence[0], b"synthetic baseline", NOW)
        store.close()
        self.registry = PreviewRegistry(path)
        self.preview = PreviewRequest(1, 1, scope, NOW, NOW + timedelta(hours=1))
        self.registry.start(self.preview, 0, NOW)
        self.jira_http = JiraTransport(self.ticket)
        self.jira = JiraClient(self.settings, self.jira_http)
        self.evidence = EvidenceClient(self.settings.evidence_url, TOKEN, ApiTransport(self.app))
        self.runtime = ExecutionRuntime(
            self.settings,
            jira=self.jira,
            evidence=self.evidence,
            tests=SyntheticTests(),
            clock=lambda: NOW,
        )

    def close(self):
        self.registry.close()

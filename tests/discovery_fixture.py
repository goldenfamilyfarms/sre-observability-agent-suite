"""Real Git objects and authenticated scoped evidence, with separate synthetic agent identities."""

from dataclasses import asdict
from datetime import timedelta

from sre_suite.auth import TokenAuthenticator
from sre_suite.discovery.contracts import AnalysisRequest
from sre_suite.discovery.runtime import DiscoveryRuntime
from sre_suite.discovery.source import snapshot
from sre_suite.execution.evidence_client import EvidenceClient
from sre_suite.execution.journal import Journal
from tests.execution_fixture import Fixture
from tests.test_api import AUDIENCE, SECOND_TOKEN, credential
from tests.test_foundation import NOW
from tests.verification_fixture import TokenTransport


class DiscoveryFixture:
    def __init__(self, root):
        self.f = Fixture(root)
        self.now = NOW
        self.f.app._clock = lambda: self.now
        self.scope = self.f.ticket.scope
        self.f.app._auth = TokenAuthenticator(
            AUDIENCE,
            (
                credential(self.scope),
                credential(self.scope, token=SECOND_TOKEN, subject="assessor"),
            ),
        )
        inventory, _ = snapshot(
            self.f.repo, self.f.ticket.repository, self.scope.revision, ("app.py", "test_app.py")
        )
        self.raw = {
            "schema_version": 2,
            "execution_id": "assessed-demo",
            "repository": self.f.ticket.repository,
            "scope": asdict(self.scope),
            "context_version": 0,
            "files": ["app.py", "test_app.py"],
            "source_artifact_digest": inventory["artifact_digest"],
            "intake_subject": "worker-one",
            "assessor_subject": "assessor",
            "project": "DEMO",
            "workstream": "demo",
            "rollback_strategy": "Revert the reviewed change and rerun regression checks",
            "expires_at": (self.now + timedelta(minutes=30)).isoformat(),
            "intake_queue": "sre-intake-v1",
            "assessment_queue": "sre-assessment-v1",
        }
        self.request = AnalysisRequest.parse(self.raw)
        self.assessor_api = EvidenceClient(
            "https://evidence.example.invalid",
            SECOND_TOKEN,
            TokenTransport(self.f.app, SECOND_TOKEN),
        )
        self.intake = DiscoveryRuntime(
            self.request,
            self.f.evidence,
            Journal(root / "intake"),
            "intake",
            self.f.repo,
            clock=lambda: self.now,
        )
        self.assessor = DiscoveryRuntime(
            self.request,
            self.assessor_api,
            Journal(root / "assessor"),
            "assessment",
            clock=lambda: self.now,
        )

    def close(self):
        self.f.close()

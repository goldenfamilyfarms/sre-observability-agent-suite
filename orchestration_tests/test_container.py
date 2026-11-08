"""Real constrained container execution, without a model or Jira connection."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from sre_suite.execution.workspace import ContainerTests
from tests.execution_fixture import Fixture

TEST_IMAGE = "python@sha256:65bd3d9f1d7fa268dd1909973ba7eb4bdf41f6dcf2cfbe76ba9528b170f3fd79"


class ContainerTestsIntegration(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.fixture = Fixture(Path(temp.name))
        self.addCleanup(self.fixture.close)
        self.settings = replace(self.fixture.settings, test_image=TEST_IMAGE)

    def test_candidate_passes_and_baseline_fails_in_real_container(self):
        runner = ContainerTests(self.settings)
        revision = self.fixture.ticket.scope.revision
        baseline = runner.execute(revision, [])
        self.assertFalse(baseline["passed"])
        candidate = runner.execute(
            revision, [{"path": "app.py", "content": "def answer():\n    return 2\n"}]
        )
        self.assertTrue(candidate["passed"], candidate)
        self.assertIn("Ran 1 test", candidate["output"])
        self.assertIn("return 1", (self.fixture.repo / "app.py").read_text())

    def test_container_is_read_only_nonroot_without_runtime_secrets(self):
        probe = """
import os, socket
assert os.getuid() != 0
assert 'ANTHROPIC_API_KEY' not in os.environ
assert 'SRE_JIRA_API_TOKEN' not in os.environ
try:
    open('/workspace/app.py','w').write('tamper')
except OSError:
    pass
else:
    raise AssertionError('source is writable')
# Some kernels expose dormant tunnel devices even in a network-none namespace.
assert len(open('/proc/net/route').read().splitlines()) == 1
sock = socket.socket()
sock.settimeout(0.2)
try:
    sock.connect(('192.0.2.1', 443))
except OSError:
    pass
else:
    raise AssertionError('container has an outbound route')
finally:
    sock.close()
print('isolation checks passed')
"""
        settings = replace(self.settings, test_command=("python", "-c", probe))
        result = ContainerTests(settings).execute(self.fixture.ticket.scope.revision, [])
        self.assertTrue(result["passed"], result)
        self.assertIn("isolation checks passed", result["output"])

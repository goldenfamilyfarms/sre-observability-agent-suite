"""Run the demo's unchanged acceptance gate in the actual isolated test container."""

from dataclasses import replace

from sre_suite.execution.workspace import ContainerTests
from sre_suite.pilot_demo.prepare import TEST_IMAGE
from tests.execution_fixture import Fixture
from tests.test_pilot_demo import MARKER, REFERENCE_FIX, DemoFixture


class DemoContainerTests(DemoFixture):
    def test_baseline_fails_and_reviewed_candidate_passes_the_same_container_gate(self):
        runtime_root = self.root / "runtime"
        runtime_root.mkdir()
        fixture = Fixture(runtime_root)
        self.addCleanup(fixture.close)
        settings = replace(
            fixture.settings,
            repository_path=self.repo,
            allowed_files=("app.py",),
            test_image=TEST_IMAGE,
            test_command=("python", "acceptance.py"),
        )
        runner = ContainerTests(settings)
        revision = self.git("rev-parse", "HEAD")
        baseline = runner.execute(revision, [])
        self.assertFalse(baseline["passed"])
        self.assertEqual(baseline["exit_code"], 1)
        before = (self.repo / "app.py").read_text()
        fixed = before.replace(MARKER, REFERENCE_FIX)
        candidate = runner.execute(revision, [{"path": "app.py", "content": fixed}])
        self.assertTrue(candidate["passed"], candidate)
        self.assertEqual(candidate["exit_code"], 0)
        self.assertEqual((self.repo / "app.py").read_text(), before)

    def test_worker_cannot_replace_the_acceptance_gate(self):
        runtime_root = self.root / "runtime"
        runtime_root.mkdir()
        fixture = Fixture(runtime_root)
        self.addCleanup(fixture.close)
        settings = replace(
            fixture.settings,
            repository_path=self.repo,
            allowed_files=("app.py",),
            test_image=TEST_IMAGE,
            test_command=("python", "acceptance.py"),
        )
        with self.assertRaisesRegex(ValueError, "unauthorized edit"):
            ContainerTests(settings).execute(
                self.git("rev-parse", "HEAD"),
                [{"path": "acceptance.py", "content": "raise SystemExit(0)\n"}],
            )

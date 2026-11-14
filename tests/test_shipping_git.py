"""Actual local Git transport proves the final compare-and-swap, without remote credentials."""

import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from sre_suite.delivery.shipping import ShippingAgent
from sre_suite.execution.process import run_process as real_run
from tests import test_shipping


class AtomicShippingTests(unittest.TestCase):
    def exercise(self, race):
        case = test_shipping.ShippingTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        repository = root / "remote.git"
        repository.mkdir()
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}

        def git(*args, input=None):
            return (
                subprocess.check_output(
                    [
                        "git",
                        "-c",
                        "user.name=Synthetic",
                        "-c",
                        "user.email=synthetic@example.invalid",
                        *args,
                    ],
                    cwd=repository,
                    env=env,
                    input=input,
                    stderr=subprocess.PIPE,
                )
                .decode()
                .strip()
            )

        git("init", "--bare", "--quiet")
        tree = git("mktree", input=b"")
        base = git("commit-tree", tree, "-m", "base")
        head = git("commit-tree", tree, "-p", base, "-m", "reviewed")
        other = git("commit-tree", tree, "-p", base, "-m", "concurrent")
        git("update-ref", "refs/heads/main", base)
        git("update-ref", "refs/heads/candidate", head)
        case.http.base, case.http.head = base, head
        case.http.pr["head"]["sha"] = head
        case.http.reviews[0]["commit_id"] = head
        case.http.checks["check_runs"][0]["head_sha"] = head
        binding = replace(
            case.binding, target={**case.binding.target, "head_sha": head, "base_sha": base}
        )
        agent = ShippingAgent(binding, case.control, case.http, "synthetic", lambda: None)

        def run(argv, workdir, environment, **options):
            command = [
                "protocol.file.allow=always" if a == "protocol.file.allow=never" else a
                for a in argv
            ]
            if "remote" in command and "add" in command:
                command[-1] = str(repository)
            if "push" in command and race:
                git("update-ref", "refs/heads/main", other)
            result = real_run(command, workdir, environment, **options)
            if "push" in command and not result.code:
                case.http.base = head
                case.http.pr["merged"] = True
            return result

        with patch("sre_suite.delivery.shipping.run_process", side_effect=run):
            if race:
                with self.assertRaises(ValueError):
                    agent.merge()
            else:
                self.assertTrue(agent.merge()["merged"])
        self.assertEqual(git("rev-parse", "refs/heads/main"), other if race else head)

    def test_exact_tested_commit_fast_forwards_real_git(self):
        self.exercise(False)

    def test_concurrent_ref_update_is_preserved_by_real_git_lease(self):
        self.exercise(True)

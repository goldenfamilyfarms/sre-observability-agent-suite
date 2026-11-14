"""No live GitHub writes: shipping rejects untested trees, stale refs and changed authority."""

import copy
import hashlib
import unittest
from dataclasses import replace
from unittest.mock import patch

from sre_suite.delivery.shipping import ShippingAgent
from sre_suite.execution.process import ProcessResult
from tests.delivery_fixture import DeliveryFixture


class GitHub:
    def __init__(self):
        self.base_url = "https://api.github.com"
        self.head, self.base = "d" * 40, "a" * 40
        self.pr = {
            "state": "open",
            "draft": False,
            "merged": False,
            "user": {"login": "author"},
            "head": {"sha": self.head, "repo": {"full_name": "example/service"}},
            "base": {"ref": "main", "repo": {"full_name": "example/service"}},
        }
        self.checks = {
            "total_count": 1,
            "check_runs": [
                {
                    "name": "tests",
                    "app": {"id": 1},
                    "head_sha": self.head,
                    "status": "completed",
                    "conclusion": "success",
                }
            ],
        }
        self.reviews = [
            {"id": 1, "user": {"login": "reviewer"}, "state": "APPROVED", "commit_id": self.head}
        ]
        self.calls = []

    def call(self, method, path, payload=None):
        if method != "GET":
            raise AssertionError("GitHub HTTP adapter must not mutate refs")
        self.calls.append(path)
        if "/git/ref/" in path:
            return {"object": {"sha": self.base}}
        if "/check-runs" in path:
            return copy.deepcopy(self.checks)
        if "/reviews?" in path:
            return copy.deepcopy(self.reviews)
        return copy.deepcopy(self.pr)


class Control:
    def __init__(self):
        self.active = True
        self.count = 0

    def require_approval(self, binding):
        self.count += 1
        if not self.active:
            raise ValueError("revoked")
        return {"approver": "human"}


class ShippingTests(unittest.TestCase):
    def setUp(self):
        from tests.test_control import ControlTests

        # Reuse only the exact-scope fixture, not its tests.
        fixture = ControlTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.http, self.control = GitHub(), Control()
        self.binding = replace(
            fixture.binding,
            action="merge",
            target={
                "environment": "development",
                "repository": "example/service",
                "git_origin": "https://github.com",
                "pull_request": 1,
                "head_sha": self.http.head,
                "base_sha": self.http.base,
                "base_branch": "main",
                "required_checks": [{"name": "tests", "app_id": 1}],
                "reviewers": ["reviewer"],
            },
        )
        self.agent = ShippingAgent(self.binding, self.control, self.http, "synthetic", lambda: None)

    def test_requires_independent_current_head_review_and_complete_trusted_ci(self):
        self.assertFalse(self.agent.inspect()["merged"])
        original = copy.deepcopy(self.http.checks)
        for key, bad in (("conclusion", "neutral"), ("head_sha", "e" * 40), ("app", {"id": 2})):
            self.http.checks = copy.deepcopy(original)
            self.http.checks["check_runs"][0][key] = bad
            with self.assertRaises(ValueError):
                self.agent.inspect()
        self.http.checks = original
        self.http.reviews[0]["user"]["login"] = "author"
        with self.assertRaises(ValueError):
            self.agent.inspect()
        self.http.reviews[0]["user"]["login"] = "reviewer"
        self.http.reviews.append(
            {
                "id": 2,
                "user": {"login": "second"},
                "state": "CHANGES_REQUESTED",
                "commit_id": self.http.head,
            }
        )
        with self.assertRaises(ValueError):
            self.agent.inspect()

    def test_atomic_push_exact_ref_lease_no_secrets_in_arguments(self):
        calls = []

        def run(argv, root, env, **kw):
            self.assertNotIn("synthetic", " ".join(argv))
            self.assertEqual(env["GIT_CONFIG_VALUE_1"], "false")
            self.assertIn("protocol.file.allow=never", argv)
            calls.append(argv)
            if "push" in argv:
                self.assertIn("--force-with-lease=refs/heads/main:" + self.http.base, argv)
                self.http.base = self.http.head
                self.http.pr["merged"] = True
            return ProcessResult(0, b"")

        with patch("sre_suite.delivery.shipping.run_process", side_effect=run):
            self.assertTrue(self.agent.merge()["merged"])
            count = len(calls)
            self.assertTrue(self.agent.merge()["merged"])
            self.assertEqual(count, len(calls))
        self.assertTrue(any("--is-ancestor" in c for c in calls))

    def test_base_movement_or_revocation_before_push_prevents_write(self):
        for mode in ("base", "revoke", "nonancestor"):
            self.setUp()
            calls = []

            def run(argv, *args, calls=calls, mode=mode, **kwargs):
                calls.append(argv)
                if "--is-ancestor" in argv:
                    if mode == "base":
                        self.http.base = "c" * 40
                    elif mode == "revoke":
                        self.control.active = False
                    else:
                        return ProcessResult(1, b"")
                return ProcessResult(0, b"")

            with (
                patch("sre_suite.delivery.shipping.run_process", side_effect=run),
                self.assertRaises(ValueError),
            ):
                self.agent.merge()
            self.assertFalse(any("push" in c for c in calls))

    def test_complete_candidate_tree_bound_to_authenticated_implementation(self):
        import tempfile
        from dataclasses import asdict
        from datetime import UTC, datetime, timedelta
        from pathlib import Path

        from sre_suite.delivery.shipping_runtime import ShippingRuntime
        from sre_suite.execution.wire import canonical

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        f = DeliveryFixture(Path(directory.name).resolve())
        self.addCleanup(f.close)
        target = {**self.binding.target, "base_sha": f.scope.revision}
        binding = replace(
            self.binding,
            scope=f.scope,
            artifact_digest="sha256:" + f.request.stages["implement"].sha256,
            plan_digest=f.request.binding.plan_digest,
            target=target,
        )
        # The fixture has no edits; use an evidence adapter retaining the authenticated receipts.
        docs = {ref.id: f.evidence.read(ref) for ref in f.request.stages.values()}
        import json

        stage = json.loads(docs[f.request.stages["implement"].id])
        stage["edits"] = [{"path": "app.py", "content": "print('reviewed')\n"}]
        docs[f.request.stages["implement"].id] = canonical(stage).encode()

        class Evidence:
            def context(self, *args):
                return {"version": 0}

            def read(self, ref):
                return docs[ref.id]

        content = stage["edits"][0]["content"].encode()
        blob = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
        base_tree, head_tree = "b" * 40, "c" * 40
        rows = [{"path": "app.py", "mode": "100644", "type": "blob", "sha": blob}]

        def get(method, path):
            if "/git/commits/" in path:
                head = path.endswith(target["head_sha"])
                return {
                    "sha": target["head_sha"] if head else target["base_sha"],
                    "tree": {"sha": head_tree if head else base_tree},
                    "parents": [{"sha": target["base_sha"]}] if head else [],
                }
            head = head_tree in path
            return {
                "sha": head_tree if head else base_tree,
                "truncated": False,
                "tree": copy.deepcopy(rows) if head else [],
            }

        from types import SimpleNamespace

        api = SimpleNamespace(base="https://api.github.com", call=get)
        runtime = ShippingRuntime(
            {
                "schema_version": 2,
                "binding": binding.wire(),
                "stages": json.loads(
                    canonical({r: asdict(ref) for r, ref in f.request.stages.items()})
                ),
                "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
            },
            f.roles,
            Evidence(),
            self.control,
            api,
            "synthetic",
        )
        runtime.candidate()
        rows.append({"path": "extra.py", "mode": "100644", "type": "blob", "sha": "e" * 40})
        with self.assertRaisesRegex(ValueError, "differs"):
            runtime.candidate()

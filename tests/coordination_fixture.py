"""Synthetic reviewed suite graph; no provider credentials."""

from dataclasses import asdict
from datetime import timedelta

from sre_suite.demo import example_ticket
from sre_suite.execution.wire import digest
from tests.test_foundation import NOW


def plan(kind="engineering", corrections=False, checkpoint=False):
    scope = example_ticket(NOW).scope
    value = {
        "schema_version": 2,
        "source": {"repository": "example/service", "base_branch": "main"},
        "task_queue": "suite-controller",
        "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        "nodes": [
            {
                "id": "first",
                "kind": kind,
                "depends_on": [],
                "task_queue": "agent",
                "request_sha256": "sha256:" + "a" * 64,
                "corrections": ["sha256:" + "b" * 64] if corrections else [],
                "checkpoint_after": checkpoint,
            }
        ],
    }
    value["binding"] = {
        "scope": asdict(scope),
        "context_version": 0,
        "artifact_digest": "sha256:" + "c" * 64,
        "plan_digest": "sha256:" + "d" * 64,
        "action": "remediate" if corrections else "execute",
        "target": {
            "environment": str(scope.environment),
            "graph_sha256": digest(
                {k: value[k] for k in ("nodes", "source", "task_queue", "expires_at")}
            ),
        },
    }
    return value


def rebind(value):
    value["binding"]["target"]["graph_sha256"] = digest(
        {k: value[k] for k in ("nodes", "source", "task_queue", "expires_at")}
    )
    return value

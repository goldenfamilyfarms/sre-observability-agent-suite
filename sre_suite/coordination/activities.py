"""Bounded processes separate local agent credentials from workflow history."""

import os
import sys
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError, CancelledError

from ..api_wire import decode
from ..execution.process import run_process
from ..execution.temporal_activities import heartbeat
from ..execution.wire import canonical, digest
from ..verification.config import private_document
from .runtime import SuiteRuntime


class SuiteActivities:
    def __init__(self, path: Path) -> None:
        self.path, self.pinned = path, digest(private_document(path))
        self.agent = SuiteRuntime(path)

    def call(self, operation: str, raw: dict[str, Any]) -> dict[str, Any]:
        try:
            if digest(private_document(self.path)) != self.pinned:
                raise ValueError("suite worker configuration changed")
            # Controller never needs model, cloud, Kubernetes or fixture credentials.
            # Per-node workers are separate processes with independently provisioned tokens.
            allowed = {
                "PATH",
                "LANG",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "SRE_EVIDENCE_API_URL",
                "SRE_EVIDENCE_API_TOKEN",
                "SRE_EVIDENCE_CA_FILE",
                "SRE_CONTROL_URL",
                "SRE_CONTROL_TOKEN",
                "SRE_CONTROL_CA_FILE",
                "SRE_GITHUB_TOKEN",
            }
            if self.agent.config["role"] != "controller":
                kind = next(
                    n["kind"] for n in self.agent.plan.nodes if n["id"] == self.agent.config["role"]
                )
                if kind in {"epic", "publish", "handoff"}:
                    allowed.update(
                        k
                        for k in os.environ
                        if k.startswith("SRE_JIRA_")
                        or k
                        in {
                            "SRE_INTAKE_SUBJECT",
                            "SRE_ASSESSMENT_SUBJECT",
                            "SRE_REPOSITORY",
                            "SRE_REQUIRE_AUTHENTICATED_APPROVALS",
                            "SRE_ALLOWED_FILES_JSON",
                            "SRE_REPO_DIR",
                            "SRE_WORK_ROOT",
                            "SRE_TEMPORAL_ADDRESS",
                            "SRE_TEMPORAL_NAMESPACE",
                            "SRE_TEMPORAL_API_KEY",
                            "SRE_TEMPORAL_TLS",
                            "SRE_TEST_IMAGE",
                            "SRE_TEST_COMMAND_JSON",
                            "SRE_CLAUDE_ENABLED",
                            "SRE_CLAUDE_MODEL",
                            "SRE_CLAUDE_BINARY",
                            "SRE_CLAUDE_BUDGET_USD",
                            "SRE_WORKER_TIMEOUT_SECONDS",
                            "SRE_GITHUB_BASE_BRANCH",
                            "SRE_GITHUB_PRS_ENABLED",
                            "SRE_ENGINEERING_ROLE",
                            "SRE_ENGINEERING_ROLES_CONFIG",
                            "SRE_ENGINEERING_TELEMETRY_CONFIG",
                            "SRE_ENGINEERING_CERTIFICATION_CONFIG",
                        }
                    )
                    # Handoff runs read-only preflight on a controller's configuration. It must
                    # not receive a model secret or impersonate a reviewer to satisfy Settings.
                    if kind == "handoff" and os.environ.get("SRE_ENGINEERING_ROLE") != "controller":
                        raise ValueError("handoff requires independent controller configuration")
            result = run_process(
                [
                    sys.executable,
                    "-m",
                    "sre_suite.coordination",
                    "child",
                    "--config",
                    str(self.path),
                ],
                Path(self.agent.config["journal"]),
                {k: v for k, v in os.environ.items() if k in allowed},
                input_bytes=canonical({"operation": operation, "request": raw}).encode(),
                timeout=560,
                max_output=262144,
                heartbeat=heartbeat,
            )
            if result.code:
                raise ApplicationError("suite agent unavailable", non_retryable=result.code != 75)
            value = decode(result.output)
            if not isinstance(value, dict):
                raise ValueError("agent response must be an object")
            return value
        except (ApplicationError, CancelledError):
            raise
        except Exception:
            raise ApplicationError("suite agent blocked", non_retryable=True) from None

    @activity.defn(name="suite_prepare")
    def prepare(self, raw: dict[str, Any]) -> dict[str, Any]:
        return self.call("prepare", raw)

    @activity.defn(name="suite_execute")
    def execute(self, raw: dict[str, Any]) -> dict[str, Any]:
        return self.call("execute", raw)

    @activity.defn(name="suite_record")
    def record(self, raw: dict[str, Any]) -> dict[str, Any]:
        return self.call("record", raw)

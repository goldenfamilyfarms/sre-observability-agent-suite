"""Private dispatch policy, authenticated approval, source fences and immutable attempt receipts."""

import asyncio
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..api_wire import evidence_from_wire, fields, text
from ..control.client import ControlClient
from ..control.contracts import timestamp, wire
from ..execution.http import JsonHttp
from ..execution.journal import Journal
from ..execution.wire import canonical, digest
from ..serialization import scope_from_dict
from ..verification.config import client, private_document
from .contracts import CHILDREN, SuitePlan, passed


class SuiteRuntime:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.config = wire(
            fields(private_document(path), {"plan", "requests", "journal", "role", "github_url"})
        )
        self.plan = SuitePlan.parse(private_document(Path(self.config["plan"])))
        if self.config["role"] != "controller" and self.config["role"] not in {
            n["id"] for n in self.plan.nodes
        }:
            raise ValueError("explicit worker role required")
        self.pinned = digest(self.config)
        self.evidence = client(os.environ)
        self.control = ControlClient(
            JsonHttp(
                os.environ["SRE_CONTROL_URL"],
                "Bearer " + os.environ["SRE_CONTROL_TOKEN"],
                local_ca_file=os.environ.get("SRE_CONTROL_CA_FILE"),
            )
        )
        self.github = JsonHttp(
            self.config["github_url"], "Bearer " + os.environ["SRE_GITHUB_TOKEN"]
        )
        self.journal = Journal(Path(self.config["journal"]))

    def guard(self, *, source: bool = True, audit_only: bool = False) -> None:
        if (
            digest(private_document(self.path)) != self.pinned
            or SuitePlan.parse(private_document(Path(self.config["plan"]))) != self.plan
        ):
            raise ValueError("suite configuration changed")
        now = datetime.now(UTC)
        if not now < timestamp(self.plan.raw["expires_at"]) <= now + timedelta(hours=24):
            raise ValueError("suite deadline exceeded")
        self.evidence.context(
            self.plan.binding.scope, None if audit_only else self.plan.binding.context_version
        )
        if not audit_only:
            self.control.require_approval(self.plan.binding)
        if source:
            origin = self.plan.raw["source"]
            ref = self.github.call(
                "GET",
                "/repos/"
                + origin["repository"]
                + "/git/ref/heads/"
                + quote(origin["base_branch"], safe=""),
            )
            if (
                not isinstance(ref, dict)
                or ref.get("object", {}).get("sha") != self.plan.binding.scope.revision
            ):
                raise ValueError("source changed; discover and approve a new scoped plan")

    def request(
        self, raw: object, *, source: bool = True, audit_only: bool = False
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        value = fields(raw, {"plan", "node", "request", "previous", "attempt"})
        if value["plan"] != self.plan.identity:
            raise ValueError("suite plan mismatch")
        node = next((n for n in self.plan.nodes if n["id"] == value["node"]), None)
        if node is None:
            raise ValueError("unknown agent")
        attempts = [node["request_sha256"], *node["corrections"]]
        index = value["attempt"]
        if (
            type(index) is not int
            or not 0 <= index < len(attempts)
            or value["request"] != attempts[index]
        ):
            raise ValueError("attempt is not authorized")
        self.guard(source=source, audit_only=audit_only)
        if index:
            ref = evidence_from_wire(value["previous"])
            if ref.scope != self.plan.binding.scope:
                raise ValueError("correction evidence scope differs")
            document = json.loads(self.evidence.read(ref))
            prior_key = digest([self.plan.identity, node["id"], index - 1])
            saved = self.journal.read(prior_key, digest(document))
            if (
                saved != document
                or saved["attempt"] != index - 1
                or saved["node"] != node["id"]
                or saved["plan"] != self.plan.identity
                or passed(node["kind"], saved["result"])
            ):
                raise ValueError("correction requires the preceding failed attempt")
        elif value["previous"] is not None:
            raise ValueError("initial attempt cannot import a previous result")
        paths = self.config["requests"]
        if not isinstance(paths, dict) or value["request"] not in paths:
            raise ValueError("missing private request")
        request = wire(
            fields(private_document(Path(paths[value["request"]])), {"scope", "payload"})
        )
        if (
            "sha256:" + digest(request) != value["request"]
            or scope_from_dict(request["scope"]) != self.plan.binding.scope
        ):
            raise ValueError("request content or preview generation changed")
        return node, wire(request["payload"])

    def prepare(self, raw: object) -> dict[str, Any]:
        if self.config["role"] != "controller":
            raise ValueError("controller role required")
        node, payload = self.request(raw)
        if node["kind"] in CHILDREN:
            self.validate_child(node["kind"], payload)
        return {"input": payload, "workflow_id": "sre-agent-" + digest([node["kind"], payload])}

    def validate_child(self, kind: str, payload: dict[str, Any]) -> None:
        scope = self.plan.binding.scope
        actual = scope
        if kind == "engineering":
            from ..execution.runtime import execution_key

            execution_key(payload)
            actual = scope_from_dict(payload["scope"])
        elif kind == "discovery":
            from ..discovery.analysis import POLICY_SHA256
            from ..discovery.contracts import AnalysisRequest

            fields(payload, {"request", "policy_sha256"})
            actual = AnalysisRequest.parse(payload["request"]).scope
            if payload["policy_sha256"] != POLICY_SHA256:
                raise ValueError("discovery policy differs")
        elif kind == "telemetry":
            from ..telemetry.schedule_contracts import TelemetrySchedule

            actual = TelemetrySchedule.parse(payload).request.scope
        elif kind == "certification":
            from ..verification.contracts import VerificationInput

            actual = VerificationInput.parse(payload).release.scope
        elif kind == "fixtures":
            from ..fixtures.contracts import FixtureRequest

            actual = FixtureRequest.parse(payload).scope
        elif kind == "delivery":
            fields(payload, {"policy", "action", "timeout_seconds"})
            if payload["action"] not in {"promote", "rollback", "cleanup"}:
                raise ValueError("invalid delivery dispatch")
        if actual != scope:
            raise ValueError("child preview identity or generation differs")

    def execute(self, raw: object) -> dict[str, Any]:
        node, value = self.request(raw)
        if self.config["role"] != node["id"] or node["kind"] in CHILDREN:
            raise ValueError("wrong operation worker")
        kind = node["kind"]
        if kind == "epic":
            from ..control.contracts import Binding
            from ..jira_planning.client import Planner
            from ..jira_planning.epic import EpicAgent

            fields(value, {"binding", "journal"})
            binding = Binding.parse(value["binding"])
            if binding.scope != self.plan.binding.scope:
                raise ValueError("epic scope differs")
            result = EpicAgent(Planner.from_env(os.environ), binding).publish(
                Path(value["journal"])
            )
        elif kind == "publish":
            from ..jira_planning.client import Planner
            from ..jira_planning.contracts import Plan

            fields(value, {"plan", "journal", "approval_digest"})
            planner = Planner.from_env(os.environ)
            if not planner.config.authenticated_approval:
                raise ValueError("suite planning requires authenticated approval")
            proposed = Plan.parse(private_document(Path(value["plan"])))
            if proposed.scope != self.plan.binding.scope:
                raise ValueError("planning scope differs")
            result = planner.publish(proposed, value["approval_digest"], Path(value["journal"]))
        elif kind == "handoff":
            from ..jira_planning.contracts import Plan
            from ..jira_planning.handoff import handoff

            fields(value, {"plan", "publication", "journal", "node", "run_id", "output"})
            if Plan.parse(private_document(Path(value["plan"]))).scope != self.plan.binding.scope:
                raise ValueError("handoff scope differs")
            result = asyncio.run(
                handoff(
                    os.environ,
                    Path(value["plan"]),
                    Path(value["publication"]),
                    Path(value["journal"]),
                    value["node"],
                    value["run_id"],
                    Path(value["output"]),
                )
            )
        elif kind == "steward":
            from ..control.steward import KnowledgeSteward, StewardRules

            fields(value, {"producers", "predicates", "maximum"})
            result = KnowledgeSteward(
                self.control,
                self.evidence,
                StewardRules(
                    frozenset(value["producers"]), frozenset(value["predicates"]), value["maximum"]
                ),
            ).run(self.plan.binding.scope)
        elif kind == "shipping":
            from ..delivery.shipping_runtime import load_runtime

            fields(value, {"config", "config_sha256"})
            path = Path(text(value["config"]))
            if digest(private_document(path)) != value["config_sha256"]:
                raise ValueError("shipping configuration changed")
            agent = load_runtime(path)
            if agent.binding.scope != self.plan.binding.scope:
                raise ValueError("shipping scope differs")
            result = agent.merge()
        else:
            from ..operations.runtime import AnalysisAgent

            if scope_from_dict(value["scope"]) != self.plan.binding.scope:
                raise ValueError("analysis scope differs")
            result = AnalysisAgent(self.evidence, self.journal).run(value)
        # These operations intentionally change the source/context; recording remains authorized
        # by the old exact binding but cannot authorize dispatch of more work on the old plan.
        if kind not in {"shipping", "steward"}:
            self.guard()
        return result

    def record(self, raw: object) -> dict[str, Any]:
        if self.config["role"] != "controller":
            raise ValueError("controller role required")
        value = wire(raw)
        result = wire(value.pop("result"))
        # Revalidate all authority after external effects. A revoked token requires reconciliation,
        # even if the side effect succeeded; the service's operation audit remains authoritative.
        kind = next(n["kind"] for n in self.plan.nodes if n["id"] == value["node"])
        node, _ = self.request(value, source=False, audit_only=kind == "steward")
        key = digest([self.plan.identity, node["id"], value["attempt"]])
        document = {
            "schema_version": 2,
            "kind": "suite-attempt",
            "plan": self.plan.identity,
            "node": node["id"],
            "attempt": value["attempt"],
            "request": value["request"],
            "result": result,
        }
        saved = self.journal.compute(key, value, lambda: document)
        if saved != document:
            raise ValueError("attempt result changed; reconcile the original receipt")
        # Stable time lives in the journal, so upload retries retain the same evidence bytes.
        stamp = self.journal.compute(
            key + "-stamp", value, lambda: {"at": datetime.now(UTC).isoformat()}
        )
        ref = self.evidence.upload(
            key,
            self.plan.binding.scope,
            canonical(saved).encode(),
            timestamp(stamp["at"]),
            timestamp(self.plan.raw["expires_at"]),
        )
        return wire(asdict(ref))

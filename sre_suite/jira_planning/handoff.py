"""Verify published planning state and run read-only preflight; never start work."""

import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..api_wire import decode, envelope
from ..execution.config import Settings
from ..execution.evidence_client import EvidenceClient
from ..execution.jira import JiraClient
from ..execution.jira_text import plain
from ..execution.pilot import _destination, _private, _read
from ..execution.readiness import ReadOnlyHttp, exit_code, run_preflight
from ..execution.runtime import execution_key
from ..execution.wire import canonical, digest, ticket_envelope
from ..local_pilot.config import write_private
from ..pilot_demo.prepare import publish_directory
from .client import Planner, obj
from .contracts import Plan

Preflight = Callable[[Mapping[str, str], Path], Awaitable[dict[str, Any]]]


def publication_keys(plan: Plan, planner: Planner, publication: object) -> dict[str, str]:
    raw = envelope(publication, {"approval_digest", "issues", "execution_started", "outcome"})
    issues = obj(raw["issues"])
    if (
        raw["approval_digest"] != planner.approval(plan)
        or raw["execution_started"] is not False
        or raw["outcome"] != "tickets_prepared"
        or set(issues) != {node.id for node in plan.nodes}
    ):
        raise ValueError("publication receipt does not match the reviewed plan")
    keys = {name: planner.key(key) for name, key in issues.items()}
    if len(set(keys.values())) != len(keys):
        raise ValueError("duplicate published Jira keys")
    return keys


def journal_state(root: Path, plan: Plan, planner: Planner, keys: dict[str, str]) -> dict[str, Any]:
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("use a physical private journal path")
    for parent in (root, *root.parents):
        if parent.is_symlink() or (parent / ".git").exists():
            raise ValueError("journal must be outside Git and symlinks")
    _private(root, directory=True)
    path = root / (digest([planner.config.url, planner.config.project, plan.id]) + ".json")
    _private(path, directory=False)
    stored = obj(decode(_read(path, 100_000)))
    state = obj(stored.get("state"))
    expected = {name: {"key": key, "ready": True} for name, key in keys.items()}
    if (
        set(stored) != {"state", "checksum"}
        or stored["checksum"] != digest(state)
        or set(state) != {"approval", "context_version", "nodes"}
        or state["approval"] != planner.approval(plan)
        or type(state["context_version"]) is not int
        or state["nodes"] != expected
    ):
        raise ValueError("publication journal is incomplete or conflicts with receipt")
    if any(obj(record).get("ready") is not True for record in obj(state["nodes"]).values()):
        raise ValueError("journal completion flags must be booleans")
    return state


async def handoff(
    env: Mapping[str, str],
    plan_path: Path,
    publication_path: Path,
    journal: Path,
    node_id: str,
    run_id: str,
    output: Path,
    *,
    planner: Planner | None = None,
    preflight: Preflight = run_preflight,
) -> dict[str, Any]:
    if not output.is_absolute() or ".." in output.parts:
        raise ValueError("use a physical absolute output path")
    plan = Plan.parse(decode(_read(plan_path, 100_000)))
    source = planner or Planner.from_env(env)
    settings = Settings.from_env(env)
    if (
        source.config.url != settings.jira_url
        or source.config.project != settings.jira_project
        or source.config.repository != settings.repository
        or source.config.contract_field != settings.jira_contract_field
    ):
        raise ValueError("planning and runtime configuration disagree")
    if source.evidence is None:
        raise ValueError("authenticated evidence reads required")
    deadline = time.monotonic() + 90

    def pulse() -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("handoff validation deadline exceeded")

    # Copy adapters behind read-only transports, even if private write flags are enabled.
    readonly = Planner(
        source.config,
        ReadOnlyHttp(source.http, pulse),
        EvidenceClient(
            settings.evidence_url,
            settings.evidence_token,
            ReadOnlyHttp(source.evidence.http, pulse, evidence=True),
        ),
        source.clock,
    )
    keys = publication_keys(plan, readonly, decode(_read(publication_path, 32_768)))
    selected = [node for node in plan.nodes if node.id == node_id]
    if len(selected) != 1:
        raise ValueError("select one published planned ID")
    expected_ticket = plan.ticket(selected[0], keys)
    spec = {
        "schema_version": 2,
        "ticket_key": keys[node_id],
        "scope": asdict(plan.scope),
        "run_id": run_id,
    }
    identity = execution_key(spec)
    state = journal_state(journal, plan, readonly, keys)
    # Check destinations before external probes and again at final publication.
    with _destination(output, "new-handoff") as destination:
        if destination.exists():
            raise ValueError("use a new output directory for each preflight observation")

    def verify() -> None:
        pulse()
        if journal_state(journal, plan, readonly, keys) != state:
            raise ValueError("planning journal changed during handoff")
        readonly.guard(plan, state["context_version"])
        for node in plan.order():
            fields = readonly.issue(plan, node, keys[node.id], allow_completed=node.id != node_id)
            if ticket_envelope(
                decode(plain(fields.get(settings.jira_contract_field)).encode())
            ) != plan.ticket(node, keys):
                raise ValueError("published Jira contract differs from the approved plan")
        # Uses the normal execution adapter, including accepted prerequisite statuses.
        if JiraClient(settings, readonly.http).get_ticket(keys[node_id], pulse) != expected_ticket:
            raise ValueError("selected runtime ticket differs from the published plan")
        readonly.guard(plan, state["context_version"])

    verify()
    with tempfile.TemporaryDirectory(prefix=".handoff-probe-", dir=output.parent) as directory:
        temporary = Path(directory) / "execution.json"
        content = (canonical(spec) + "\n").encode()
        write_private(temporary, content)
        result = await preflight(env, temporary)
        if temporary.read_bytes() != content:
            raise ValueError("execution specification changed during preflight")
    # A preflight failure is an observation, not authority to mutate or start work.
    if result.get("read_only") is not True or result.get("execution_id") != identity:
        raise ValueError("preflight result not bound to generated execution")
    code = exit_code(result)
    # Preflight has its own deadline. Renew only the bounded final consistency read window.
    deadline = time.monotonic() + 90
    verify()
    receipt = {
        "schema_version": 2,
        "approval_digest": readonly.approval(plan),
        "node_id": node_id,
        "workflow_id": identity,
        "execution_sha256": digest(spec),
        "preflight_sha256": digest(result),
        "context_version": state["context_version"],
        "execution_started": False,
        "preflight_exit_code": code,
        "status": result["status"],
    }
    publish_directory(
        output,
        {
            name: (canonical(value) + "\n").encode()
            for name, value in {
                "execution.json": spec,
                "preflight.json": result,
                "handoff.json": receipt,
            }.items()
        },
    )
    return receipt

"""Prepare immutable synthetic baseline evidence and finalize a scoped Jira contract."""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from typing import Any

from ..api_wire import decode, envelope, evidence_from_wire, text
from ..contracts import Environment, JiraExecutionTicket, Scope
from ..execution.pilot import PilotManifest, _destination, _read, _sync_directory
from ..execution.process import minimal_environment
from ..execution.wire import canonical, ticket_envelope, ticket_json
from ..local_pilot.config import write_json, write_private
from ..previews import preview_environment_id

TEMPLATE = {
    "app.py": "app.py.txt",
    "test_app.py": "test_app.py.txt",
    "acceptance.py": "acceptance.py.txt",
    "README.md": "README.md.txt",
    ".gitignore": "gitignore.txt",
    ".github/workflows/ci.yml": "ci.yml.txt",
}
SOURCE_FILES = ("app.py", "test_app.py", "acceptance.py")
TEST_IMAGE = "python@sha256:65bd3d9f1d7fa268dd1909973ba7eb4bdf41f6dcf2cfbe76ba9528b170f3fd79"
EXPECTED_REPORT = {
    "tests_run": 7,
    "failures": ["test_valid_context_is_correlated"],
    "errors": [],
    "skipped": 0,
    "passed": False,
}


def template() -> dict[str, bytes]:
    assets = files("sre_suite.pilot_demo").joinpath("assets")
    return {name: assets.joinpath(asset).read_bytes() for name, asset in TEMPLATE.items()}


def publish_directory(output: Path, contents: dict[str, bytes]) -> None:
    with _destination(output, "new-demo-bundle") as target:
        if target.exists():
            raise ValueError("output already exists; never overwrite pilot inputs")
        with tempfile.TemporaryDirectory(prefix=".demo-prepare-", dir=target.parent) as temporary:
            bundle = Path(temporary) / "bundle"
            bundle.mkdir(mode=0o700)
            for name, content in contents.items():
                destination = bundle / name
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                write_private(destination, content)
            _sync_directory(bundle)
            bundle.rename(target)
            _sync_directory(target.parent)


def materialize(output: Path) -> None:
    publish_directory(output, template())


def git(checkout: Path, *args: str) -> bytes:
    env = minimal_environment()
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=" + os.devnull,
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(checkout),
            *args,
        ],
        env=env,
        check=True,
        capture_output=True,
        timeout=15,
    )
    if len(result.stdout) > 100_000:
        raise ValueError("Git output exceeded limit")
    return result.stdout


def baseline_report(checkout: Path) -> tuple[str, dict[str, object]]:
    revision = git(checkout, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("full baseline revision required")
    known = template()
    entries = git(checkout, "ls-tree", "-r", revision).decode().splitlines()
    names = []
    for entry in entries:
        metadata, name = entry.split("\t", 1)
        if not metadata.startswith("100644 blob "):
            raise ValueError("demo baseline must contain only regular non-executable files")
        names.append(name)
    if set(names) != set(known) or len(names) != len(known):
        raise ValueError("demo baseline contains unexpected or missing files")
    with tempfile.TemporaryDirectory(prefix="sre-demo-baseline-") as directory:
        root = Path(directory)
        for name in known:
            source = git(checkout, "show", f"{revision}:{name}")
            if source != known[name]:
                raise ValueError("source differs from the reviewed demo baseline")
            if name in SOURCE_FILES:
                (root / name).write_bytes(source)
        # Only byte-verified packaged code executes, without account credentials or site packages.
        result = subprocess.run(
            [sys.executable, "-I", str(root / "acceptance.py"), "--baseline-report"],
            cwd=root,
            env=minimal_environment(),
            capture_output=True,
            timeout=15,
        )
        if result.returncode != 0 or len(result.stdout) > 8192:
            raise ValueError("baseline did not demonstrate the expected gap")
        report = decode(result.stdout)
        if report != EXPECTED_REPORT:
            raise ValueError("unexpected baseline results")
    return revision, dict(EXPECTED_REPORT)


def draft_ticket(repository: str, key: str, scope: Scope) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "ticket": {
            "key": key,
            "workstream": "synthetic-request-log-correlation",
            "scope": asdict(scope),
            "repository": repository,
            "current_state": (
                "The demo returns bounded JSON request logs without trace_id correlation."
            ),
            "desired_state": "Valid synthetic request-context trace IDs appear in structured logs.",
            "objective": "Implement validated trace_id correlation by changing app.py only.",
            "implementation_scope": [
                "Edit only app.py; test_app.py, acceptance.py and CI are immutable.",
                "No packages, network calls, listeners, infrastructure or configuration changes.",
            ],
            "dependencies": [],
            "acceptance_criteria": [
                "For GET /health, unknown routes and unsupported methods, "
                "preserve a valid trace_id exactly.",
                "Valid means a string of exactly 32 lowercase hexadecimal "
                "characters, not all zeroes.",
                "Omit trace_id for absent, nonstring, uppercase, wrong-length, "
                "nonhex, all-zero or newline input.",
                "Preserve response status/body, route sanitization and the "
                "event/route/status log fields.",
                "Never log raw context, arbitrary headers, request bodies, raw "
                "paths or extra fields.",
                "Run python acceptance.py: all seven tests must pass with no "
                "failures, errors or skips.",
            ],
            "qa_requirements": [
                "Use the unchanged acceptance.py gate in the pinned "
                "network-disabled test container.",
                "Confirm the baseline has exactly the expected missing-correlation failure.",
            ],
            "telemetry_requirements": [
                "Verify synthetic JSON log correlation only; do not claim real "
                "telemetry export or service certification."
            ],
            "delivery_requirements": [
                "Open a draft PR against the explicitly configured demo base branch.",
                "Record evidence and configured nonterminal Jira transitions; no "
                "merge or deployment.",
            ],
            "rollback_strategy": (
                "Discard the unmerged candidate branch/PR after review; no "
                "running service is changed."
            ),
            "evidence": [],
        },
    }


def prepare(
    checkout: Path,
    repository: str,
    repository_id: int,
    pr_number: int,
    ticket_key: str,
    generation: int,
    run_id: str,
    output: Path,
    *,
    base_branch: str = "feat/demo-baseline",
    minutes: int = 120,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository must be owner/name")
    if type(minutes) is not int or not 5 <= minutes <= 240:
        raise ValueError("pilot window must be 5..240 minutes")
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_/-]{0,150}", base_branch)
        or "//" in base_branch
        or base_branch.endswith("/")
        or base_branch.startswith("sre/run-")
    ):
        raise ValueError("invalid demo publication base")
    now = clock()
    revision, report = baseline_report(checkout)
    scope = Scope(
        "pilot-log-demo",
        Environment.DEVELOPMENT,
        revision,
        preview_environment_id(repository_id, pr_number),
        generation,
    )
    expires = now + timedelta(minutes=minutes)
    baseline = {
        "schema_version": 2,
        "kind": "synthetic-demo-baseline",
        "repository": repository,
        "scope": asdict(scope),
        "observed_at": now.isoformat(),
        "report": report,
        "source_sha256": {
            name: hashlib.sha256(template()[name]).hexdigest() for name in SOURCE_FILES
        },
    }
    content = (canonical(baseline) + "\n").encode()
    manifest = {
        "schema_version": 2,
        "registration": {
            "schema_version": 2,
            "preview": {
                "repository_id": repository_id,
                "pr_number": pr_number,
                "scope": asdict(scope),
                "requested_at": now.isoformat(),
                "expires_at": expires.isoformat(),
            },
            "expected_generation": generation - 1,
        },
        "ticket_key": ticket_key,
        "run_id": run_id,
        "baseline": {
            "sha256": hashlib.sha256(content).hexdigest(),
            "observed_at": now.isoformat(),
            "expires_at": expires.isoformat(),
        },
    }
    PilotManifest.parse(json.loads(canonical(manifest)), now)
    draft = draft_ticket(repository, ticket_key, scope)
    worker = {
        "SRE_JIRA_BASE_URL": "https://your-site.atlassian.net",
        "SRE_JIRA_EMAIL": "replace-privately",
        "SRE_JIRA_API_TOKEN": "replace-privately",
        "SRE_JIRA_PROJECT": ticket_key.rsplit("-", 1)[0],
        "SRE_JIRA_CONTRACT_FIELD": "customfield_12345",
        "SRE_JIRA_DONE_STATUSES": "Done",
        "SRE_JIRA_WRITES_ENABLED": "false",
        "SRE_REPOSITORY": repository,
        "SRE_REPO_DIR": str(checkout.resolve()),
        "SRE_ALLOWED_FILES_JSON": '["app.py"]',
        "SRE_TEST_IMAGE": TEST_IMAGE,
        "SRE_TEST_COMMAND_JSON": '["python","acceptance.py"]',
        "SRE_CLAUDE_BINARY": "/absolute/path/to/claude",
        "SRE_CLAUDE_MODEL": "replace-with-approved-model-id",
        "ANTHROPIC_API_KEY": "replace-privately",
        "SRE_CLAUDE_ENABLED": "false",
        "SRE_CLAUDE_BUDGET_USD": "2",
        "SRE_WORKER_TIMEOUT_SECONDS": "300",
        "SRE_GITHUB_PRS_ENABLED": "false",
        "SRE_GITHUB_TOKEN": "replace-privately",
        "SRE_GITHUB_BASE_BRANCH": base_branch,
        "SRE_JIRA_TRANSITIONS_ENABLED": "false",
    }
    publish_directory(
        output,
        {
            "baseline.json": content,
            "manifest.json": (canonical(manifest) + "\n").encode(),
            "ticket-draft.json": (canonical(draft) + "\n").encode(),
            "worker.json": (canonical(worker) + "\n").encode(),
        },
    )


def finalize(
    prepared: Path,
    evidence_path: Path,
    output: Path,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> JiraExecutionTicket:
    now = clock()
    manifest = PilotManifest.parse(decode(_read(prepared / "manifest.json", 32_768)), now)
    content = _read(prepared / "baseline.json", 32_768)
    baseline = envelope(
        decode(content), {"kind", "repository", "scope", "observed_at", "report", "source_sha256"}
    )
    reference = envelope(decode(_read(evidence_path, 32_768)), {"evidence"})
    evidence = evidence_from_wire(reference["evidence"])
    if (
        hashlib.sha256(content).hexdigest() != manifest.baseline.sha256
        or evidence.scope != manifest.preview.scope
        or evidence.sha256 != manifest.baseline.sha256
        or evidence.observed_at != manifest.baseline.observed_at
        or evidence.expires_at != manifest.baseline.expires_at
        or not evidence.valid_for(evidence.scope, now)
    ):
        raise ValueError("uploaded evidence does not match the current prepared baseline")
    from ..serialization import scope_from_dict

    if (
        scope_from_dict(baseline["scope"]) != evidence.scope
        or baseline["observed_at"] != evidence.observed_at.isoformat()
        or baseline["kind"] != "synthetic-demo-baseline"
        or baseline["report"] != EXPECTED_REPORT
        or baseline["source_sha256"]
        != {name: hashlib.sha256(template()[name]).hexdigest() for name in SOURCE_FILES}
    ):
        raise ValueError("baseline provenance mismatch")
    raw = draft_ticket(text(baseline["repository"]), manifest.ticket_key, evidence.scope)
    # Ignore edited drafts: requirements are reconstructed from the reviewed template.
    raw["ticket"]["evidence"] = [asdict(evidence)]
    ticket = ticket_envelope(json.loads(canonical(raw)))
    # A local metadata consistency check, not an online evidence verification or new grant.
    from ..execution.pilot import _private

    for parent in (output, *output.parents):
        if parent.is_symlink() or (parent / ".git").exists():
            raise ValueError("ticket output must remain private and outside Git")
    if not output.is_absolute() or ".." in output.parts:
        raise ValueError("ticket output must use a physical absolute path")
    _private(output.parent, directory=True)
    write_json(output, {"schema_version": 2, "ticket": ticket_json(ticket)})
    return ticket

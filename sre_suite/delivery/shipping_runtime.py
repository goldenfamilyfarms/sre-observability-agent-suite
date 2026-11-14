"""Shipping entrypoint binds authenticated stage evidence to the complete Git candidate tree."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..api_wire import envelope, evidence_from_wire, fields, text
from ..control.client import ControlClient
from ..control.contracts import Binding, timestamp, wire
from ..execution.evidence_client import EvidenceClient
from ..execution.http import JsonHttp
from ..verification.config import private_document
from .shipping import ShippingAgent


class ShippingRuntime:
    def __init__(
        self,
        request: object,
        roles: dict[str, str],
        evidence: EvidenceClient,
        control: ControlClient,
        github: JsonHttp,
        token: str,
    ) -> None:
        raw = envelope(request, {"binding", "stages", "expires_at"})
        self.binding = Binding.parse(raw["binding"])
        self.expires = timestamp(raw["expires_at"])
        self.stages = {
            r: evidence_from_wire(v)
            for r, v in fields(raw["stages"], {"implement", "review", "test", "qa"}).items()
        }
        if set(roles) != set(self.stages) or len(set(roles.values())) != 4:
            raise ValueError("independent engineering identities required")
        self.roles, self.evidence, self.control, self.http = roles, evidence, control, github
        self.agent = ShippingAgent(self.binding, control, github, token, self.guard)
        from urllib.parse import urlsplit

        api, git = urlsplit(github.base), urlsplit(self.binding.target["git_origin"])
        if not (
            (api.hostname == "api.github.com" and not api.path and git.hostname == "github.com")
            or (api.hostname == git.hostname and api.path == "/api/v3")
        ):
            raise ValueError("Git remote must match the reviewed GitHub API host")
        if self.binding.target["base_sha"] != self.binding.scope.revision:
            raise ValueError("shipping base differs from reviewed source")

    def guard(self) -> None:
        if datetime.now(UTC) >= self.expires:
            raise ValueError("shipping request expired")
        self.evidence.context(self.binding.scope, self.binding.context_version)
        approval = self.control.require_approval(self.binding)
        if approval.get("approver") in self.roles.values():
            raise ValueError("shipping approver must be independent of engineering")
        predecessors: list[str] = []
        for role in ("implement", "review", "test", "qa"):
            ref = self.stages[role]
            if ref.scope != self.binding.scope or ref.producer != self.roles[role]:
                raise ValueError("stage identity differs")
            stage = json.loads(self.evidence.read(ref))
            if stage.get("prior_evidence_sha256") != predecessors:
                raise ValueError("shipping gates do not attest the same candidate chain")
            predecessors.append(ref.sha256)
            if (
                stage.get("role") != role
                or stage.get("verdict") != "passed"
                or stage.get("manifest") != self.binding.plan_digest.removeprefix("sha256:")
            ):
                raise ValueError("stage does not approve the exact plan")
        if self.binding.artifact_digest != "sha256:" + self.stages["implement"].sha256:
            raise ValueError("shipping artifact differs")

    def candidate(self) -> None:
        """Read every leaf; an approved patch cannot conceal an extra file or symlink."""
        self.guard()
        target = self.binding.target
        root = "/repos/" + target["repository"]

        def get(path: str) -> Any:
            self.guard()
            result = self.http.call("GET", root + path)
            self.guard()
            return result

        commits = [get("/git/commits/" + target[k]) for k in ("base_sha", "head_sha")]
        if [p.get("sha") for p in commits[1].get("parents", [])] != [target["base_sha"]] or any(
            c.get("sha") != target[k]
            for c, k in zip(commits, ("base_sha", "head_sha"), strict=True)
        ):
            raise ValueError("candidate must be one exact tested commit on the pinned base")
        trees = []
        for commit in commits:
            sha = text(commit["tree"]["sha"], 40)
            tree = get("/git/trees/" + sha + "?recursive=1")
            if tree.get("sha") != sha or tree.get("truncated") is not False:
                raise ValueError("complete candidate tree required")
            rows = tree.get("tree")
            if not isinstance(rows, list) or len(rows) > 100000:
                raise ValueError("candidate tree exceeds budget")
            leaves = {
                r["path"]: (r["mode"], r["type"], r["sha"]) for r in rows if r["type"] != "tree"
            }
            if len({r["path"] for r in rows}) != len(rows):
                raise ValueError("duplicate tree paths")
            trees.append(leaves)
        expected = dict(trees[0])
        stage = json.loads(self.evidence.read(self.stages["implement"]))
        edits = stage.get("edits")
        if not isinstance(edits, list) or not 1 <= len(edits) <= 50:
            raise ValueError("bounded implementation edits required")
        seen = set()
        for edit in edits:
            item = fields(edit, {"path", "content"})
            from ..execution.config import safe_path

            path = safe_path(text(item["path"], 240))
            if not isinstance(item["content"], str) or len(item["content"].encode()) > 100000:
                raise ValueError("bounded file contents required")
            content = item["content"].encode()
            previous = expected.get(path, ("100644", "blob", None))
            if previous[0] not in {"100644", "100755"} or previous[1] != "blob" or path in seen:
                raise ValueError("candidate changes a non-regular file")
            seen.add(path)
            blob = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
            expected[path] = (previous[0], "blob", blob)
        if trees[1] != expected or trees[0] == expected:
            raise ValueError("Git candidate differs from approved implementation")
        self.guard()

    def inspect(self) -> dict[str, Any]:
        self.candidate()
        return self.agent.inspect()

    def merge(self) -> dict[str, Any]:
        self.candidate()
        # Candidate commit objects are immutable. Live permission, scope, CI, reviews and
        # destination ref are rechecked by ShippingAgent immediately before its atomic push.
        return self.agent.merge()


def load_runtime(path: Path) -> ShippingRuntime:
    import os

    from ..verification.config import client

    raw = fields(private_document(path), {"request", "roles", "github_url"})
    roles = wire(fields(raw["roles"], {"implement", "review", "test", "qa"}))
    for value in roles.values():
        text(value, 128)
    token = os.environ["SRE_GITHUB_TOKEN"]
    return ShippingRuntime(
        private_document(Path(text(raw["request"]))),
        roles,
        client(os.environ),
        ControlClient(
            JsonHttp(
                os.environ["SRE_CONTROL_URL"],
                "Bearer " + os.environ["SRE_CONTROL_TOKEN"],
                local_ca_file=os.environ.get("SRE_CONTROL_CA_FILE"),
            )
        ),
        JsonHttp(text(raw["github_url"]), "Bearer " + token),
        token,
    )

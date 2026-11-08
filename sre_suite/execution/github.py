"""Create one draft PR for a tested artifact; never update, merge or delete refs."""

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode

from ..contracts import Scope
from .claude import validate_output
from .config import Settings
from .http import JsonHttp, RemoteError


def object_sha(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("invalid Git object SHA")
    return value


class GitHubClient:
    def __init__(self, settings: Settings, http: JsonHttp | None = None) -> None:
        self.settings = settings
        self.http = http or JsonHttp("https://api.github.com", "Bearer " + settings.github_token)
        self.root = "/repos/" + settings.repository

    def _call(self, method: str, path: str, payload: object = None) -> Any:
        return self.http.call(method, self.root + path, payload)

    def _ref(self, branch: str, heartbeat: Callable[[], None] = lambda: None) -> str | None:
        heartbeat()
        try:
            result = self._call("GET", "/git/ref/heads/" + quote(branch, safe=""))
        except RemoteError as error:
            if error.status == 404:
                return None
            raise
        if (
            not isinstance(result, dict)
            or result.get("ref") != "refs/heads/" + branch
            or result.get("object", {}).get("type") != "commit"
        ):
            raise ValueError("unexpected Git reference")
        return object_sha(result["object"]["sha"])

    def publish(
        self,
        execution_id: str,
        manifest: str,
        artifact_digest: str,
        scope: Scope,
        artifact: dict[str, Any],
        guard: Callable[[], None],
        heartbeat: Callable[[], None] = lambda: None,
    ) -> dict[str, Any]:
        def call(method: str, path: str, payload: object = None) -> Any:
            heartbeat()
            return self._call(method, path, payload)

        if not self.settings.github_prs:
            raise ValueError("GitHub PR publication disabled")
        if not re.fullmatch(r"sre-[0-9a-f]{64}", execution_id):
            raise ValueError("invalid execution identity")
        if any(not re.fullmatch(r"[0-9a-f]{64}", d) for d in (manifest, artifact_digest)):
            raise ValueError("invalid artifact identity")
        validated = validate_output(
            {k: artifact[k] for k in ("verdict", "summary", "edits")},
            "implement",
            self.settings.allowed_files,
        )
        if validated["verdict"] != "passed" or artifact.get("manifest") != manifest:
            raise ValueError("implementation is not publishable")
        revision = object_sha(scope.revision)
        branch = "sre/run-" + execution_id[4:]
        base = self.settings.github_base

        def check() -> None:
            guard()
            if self._ref(base, heartbeat) != revision:
                raise ValueError("PR base moved; a new tested execution is required")

        check()
        commit = call("GET", "/git/commits/" + revision)
        if not isinstance(commit, dict) or commit.get("sha") != revision:
            raise ValueError("source commit identity mismatch")
        base_tree = object_sha(commit["tree"]["sha"])
        tree = call("GET", "/git/trees/" + base_tree + "?recursive=1")
        if (
            not isinstance(tree, dict)
            or tree.get("sha") != base_tree
            or tree.get("truncated") is not False
            or not isinstance(tree.get("tree"), list)
        ):
            raise ValueError("complete source tree required")
        entries = {entry["path"]: entry for entry in tree["tree"]}
        if len(entries) != len(tree["tree"]):
            raise ValueError("duplicate source tree paths")
        changes = []
        for edit in validated["edits"]:
            entry = entries.get(edit["path"])
            mode = "100644"
            if entry:
                if entry.get("type") != "blob" or entry.get("mode") not in {"100644", "100755"}:
                    raise ValueError("only regular source files may be changed")
                mode = entry["mode"]
            pieces = edit["path"].split("/")
            for count in range(1, len(pieces)):
                ancestor = entries.get("/".join(pieces[:count]))
                if ancestor and ancestor.get("type") != "tree":
                    raise ValueError("file ancestor is not a directory")
            changes.append(
                {"path": edit["path"], "mode": mode, "type": "blob", "content": edit["content"]}
            )
        check()
        tree_result = call("POST", "/git/trees", {"base_tree": base_tree, "tree": changes})
        new_tree = object_sha(tree_result["sha"])
        if new_tree == base_tree:
            raise ValueError("no publishable changes")
        # Fixed author/date/message makes retrying object creation deterministic.
        date = datetime.fromisoformat(artifact["observed_at"])
        if date.tzinfo is None:
            raise ValueError("commit date requires timezone")
        identity = {
            "name": "SRE Engineering Worker",
            "email": "worker@example.invalid",
            "date": date.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
        message = (
            f"SRE reviewed candidate {execution_id}\n\n"
            f"Manifest: {manifest}\nArtifact: {artifact_digest}\n"
        )
        check()
        created = call(
            "POST",
            "/git/commits",
            {
                "message": message,
                "tree": new_tree,
                "parents": [revision],
                "author": identity,
                "committer": identity,
            },
        )
        head = object_sha(created["sha"])
        if created.get("tree", {}).get("sha") != new_tree or [
            p.get("sha") for p in created.get("parents", [])
        ] != [revision]:
            raise ValueError("created commit differs from tested candidate")
        existing = self._ref(branch, heartbeat)
        if existing is None:
            check()
            try:
                call("POST", "/git/refs", {"ref": "refs/heads/" + branch, "sha": head})
            except RemoteError as error:
                if error.status not in {409, 422}:
                    raise
                # A concurrent creator may already have published the exact ref.
                if self._ref(branch, heartbeat) != head:
                    raise ValueError("branch creation conflict") from None
        elif existing != head:
            raise ValueError("execution branch was changed; refusing to overwrite it")
        check()
        if self._ref(branch, heartbeat) != head:
            raise ValueError("execution branch changed before PR creation")
        owner = self.settings.repository.split("/")[0]
        query = urlencode({"state": "all", "head": owner + ":" + branch, "per_page": 2})
        pulls = call("GET", "/pulls?" + query)
        if not isinstance(pulls, list) or len(pulls) > 1:
            raise ValueError("ambiguous existing pull requests")
        title = "SRE engineering candidate " + execution_id[4:16]
        body = (
            f"Reviewable candidate from execution `{execution_id}`.\n\n"
            f"Source revision: `{revision}`\nManifest: `{manifest}`\n"
            f"Implementation artifact: `{artifact_digest}`\n\n"
            "Implementation, review, configured container tests and QA passed. "
            "These gates are not service certification or deployment authorization. "
            "Raw Jira content and evidence remain in the private evidence system.\n"
        )
        if pulls:
            result = pulls[0]
        else:
            check()
            if self._ref(branch, heartbeat) != head:
                raise ValueError("execution branch changed before PR creation")
            try:
                result = call(
                    "POST",
                    "/pulls",
                    {
                        "title": title,
                        "body": body,
                        "head": branch,
                        "base": base,
                        "draft": True,
                        "maintainer_can_modify": False,
                    },
                )
            except RemoteError as error:
                if error.status != 422:
                    raise
                pulls = call("GET", "/pulls?" + query)
                if not isinstance(pulls, list) or len(pulls) != 1:
                    raise ValueError("PR creation conflict") from None
                result = pulls[0]
        if (
            not isinstance(result, dict)
            or result.get("state") != "open"
            or result.get("draft") is not True
            or result.get("merged_at") is not None
            or result.get("body") != body
            or result.get("title") != title
            or result.get("head", {}).get("sha") != head
            or result.get("head", {}).get("ref") != branch
            or result.get("head", {}).get("repo", {}).get("full_name") != self.settings.repository
            or result.get("base", {}).get("ref") != base
            or result.get("base", {}).get("sha") != revision
            or result.get("base", {}).get("repo", {}).get("full_name") != self.settings.repository
            or type(result.get("number")) is not int
            or result["number"] <= 0
        ):
            raise ValueError("existing PR differs from the authorized draft")
        check()
        if self._ref(branch, heartbeat) != head:
            raise ValueError("execution branch changed after PR creation")
        return {
            "number": result["number"],
            "url": f"https://github.com/{self.settings.repository}/pull/{result['number']}",
            "head": head,
            "branch": branch,
            "base": base,
            "draft": True,
            "source_revision": revision,
            "artifact": artifact_digest,
        }

    def verify(self, published: dict[str, Any], heartbeat: Callable[[], None]) -> None:
        if (
            self._ref(published["branch"], heartbeat) != published["head"]
            or self._ref(published["base"], heartbeat) != published["source_revision"]
        ):
            raise ValueError("published candidate or base changed")
        heartbeat()
        result = self._call("GET", f"/pulls/{published['number']}")
        if (
            not isinstance(result, dict)
            or result.get("number") != published["number"]
            or result.get("state") != "open"
            or result.get("draft") is not True
            or result.get("merged_at") is not None
            or result.get("head", {}).get("sha") != published["head"]
            or result.get("head", {}).get("ref") != published["branch"]
            or result.get("base", {}).get("ref") != published["base"]
            or result.get("base", {}).get("sha") != published["source_revision"]
            or result.get("head", {}).get("repo", {}).get("full_name") != self.settings.repository
            or result.get("base", {}).get("repo", {}).get("full_name") != self.settings.repository
        ):
            raise ValueError("published draft no longer matches the reviewed candidate")

"""Fixture REST transports backed by real Git objects; no external account calls."""

import copy
import json
import os
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import unquote

from sre_suite.execution.config import Settings
from sre_suite.execution.github import GitHubClient
from sre_suite.execution.http import RemoteError
from sre_suite.execution.jira import JiraClient
from sre_suite.execution.runtime import ExecutionRuntime
from tests.execution_fixture import JiraTransport
from tests.test_foundation import NOW

RULES = {
    "in_progress": {"transition_id": "10", "status_id": "200", "from_status_ids": ["100"]},
    "in_review": {"transition_id": "20", "status_id": "300", "from_status_ids": ["200"]},
    "blocked": {"transition_id": "30", "status_id": "400", "from_status_ids": ["200"]},
}


class GitHubTransport:
    def __init__(self, fixture, events):
        self.fixture = fixture
        self.events = events
        self.refs = {"main": fixture.ticket.scope.revision}
        self.pulls = []
        self.writes = []
        self.lose_once = None
        self.after_write = lambda path: None
        self.base = "/repos/" + fixture.settings.repository
        self.entries_override = None

    def git(self, *args, data=None, extra=None):
        env = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            **(extra or {}),
        }
        return subprocess.check_output(["git", *args], cwd=self.fixture.repo, env=env, input=data)

    def commit(self, sha):
        return {
            "sha": sha,
            "tree": {"sha": self.git("rev-parse", sha + "^{tree}").decode().strip()},
            "parents": [
                {"sha": p} for p in self.git("show", "-s", "--format=%P", sha).decode().split()
            ],
        }

    def call(self, method, path, payload=None):
        assert path.startswith(self.base)
        path = path[len(self.base) :]
        if method != "GET":
            self.writes.append((method, path, copy.deepcopy(payload)))
            self.events.append("github:" + path)
        if method == "GET":
            if path.startswith("/git/ref/heads/"):
                branch = unquote(path.removeprefix("/git/ref/heads/"))
                if branch not in self.refs:
                    raise RemoteError(404)
                return {
                    "ref": "refs/heads/" + branch,
                    "object": {"type": "commit", "sha": self.refs[branch]},
                }
            if path.startswith("/git/commits/"):
                return self.commit(path.split("/")[-1])
            if path.startswith("/git/trees/"):
                sha = path.split("/")[-1].split("?")[0]
                entries = []
                for line in self.git("ls-tree", "-r", "-t", sha).decode().splitlines():
                    info, name = line.split("\t")
                    mode, kind, value = info.split()
                    entries.append({"mode": mode, "type": kind, "sha": value, "path": name})
                return {
                    "sha": sha,
                    "truncated": False,
                    "tree": entries if self.entries_override is None else self.entries_override,
                }
            if path.startswith("/pulls/"):
                return copy.deepcopy(self.pulls[int(path.split("/")[-1]) - 1])
            if path.startswith("/pulls?"):
                return copy.deepcopy(self.pulls)
            raise AssertionError(path)
        assert method == "POST", "No ref updates, merges, deletion or deployment allowed"
        if path == "/git/trees":
            with tempfile.TemporaryDirectory() as temporary:
                extra = {"GIT_INDEX_FILE": str(Path(temporary) / "index")}
                self.git("read-tree", payload["base_tree"], extra=extra)
                for change in payload["tree"]:
                    sha = (
                        self.git("hash-object", "-w", "--stdin", data=change["content"].encode())
                        .decode()
                        .strip()
                    )
                    self.git(
                        "update-index",
                        "--add",
                        "--cacheinfo",
                        change["mode"],
                        sha,
                        change["path"],
                        extra=extra,
                    )
                result = {"sha": self.git("write-tree", extra=extra).decode().strip()}
        elif path == "/git/commits":
            extra = {}
            for role in ("author", "committer"):
                for key in ("name", "email", "date"):
                    extra[f"GIT_{role.upper()}_{key.upper()}"] = payload[role][key]
            sha = (
                self.git(
                    "-c",
                    "commit.gpgsign=false",
                    "commit-tree",
                    payload["tree"],
                    "-p",
                    payload["parents"][0],
                    data=payload["message"].encode(),
                    extra=extra,
                )
                .decode()
                .strip()
            )
            result = self.commit(sha)
        elif path == "/git/refs":
            branch = payload["ref"].removeprefix("refs/heads/")
            if branch in self.refs:
                raise RemoteError(422)
            self.refs[branch] = payload["sha"]
            result = {"ref": payload["ref"], "object": {"type": "commit", "sha": payload["sha"]}}
        elif path == "/pulls":
            if self.pulls:
                raise RemoteError(422)
            result = {
                **payload,
                "number": 1,
                "state": "open",
                "merged_at": None,
                "head": {
                    "ref": payload["head"],
                    "sha": self.refs[payload["head"]],
                    "repo": {"full_name": self.fixture.settings.repository},
                },
                "base": {
                    "ref": payload["base"],
                    "sha": self.refs[payload["base"]],
                    "repo": {"full_name": self.fixture.settings.repository},
                },
            }
            self.pulls.append(copy.deepcopy(result))
        else:
            raise AssertionError(path)
        self.after_write(path)
        if self.lose_once == path:
            self.lose_once = None
            raise RemoteError(0)
        return result


class TransitionTransport(JiraTransport):
    def __init__(self, ticket, events):
        super().__init__(ticket)
        self.events = events
        self.status = "100"
        self.category = "new"
        self.transitions = []
        self.lose_transition = False
        self.before_transition = lambda: None
        self.required_fields = False
        self.target_category = "indeterminate"

    def call(self, method, path, payload=None):
        if "/transitions" in path:
            if method == "GET":
                return {
                    "transitions": [
                        {
                            "id": r["transition_id"],
                            "to": {
                                "id": r["status_id"],
                                "statusCategory": {"key": self.target_category},
                            },
                            "fields": {"resolution": {"required": self.required_fields}},
                        }
                        for r in RULES.values()
                        if self.status in r["from_status_ids"]
                    ]
                }
            self.before_transition()
            self.transitions.append(payload["transition"]["id"])
            self.events.append("jira:" + payload["transition"]["id"])
            rule = next(r for r in RULES.values() if r["transition_id"] == self.transitions[-1])
            self.status = rule["status_id"]
            self.category = self.target_category
            for prop in payload["properties"]:
                self.properties[path.split("/transitions")[0] + "/properties/" + prop["key"]] = (
                    prop["value"]
                )
            if self.lose_transition:
                self.lose_transition = False
                raise RemoteError(0)
            return None
        result = super().call(method, path, payload)
        if "/properties/" not in path:
            result["fields"]["status"] = {
                "id": self.status,
                "name": "Fixture status",
                "statusCategory": {"key": self.category},
            }
        return result


def publication_runtime(fixture):
    env = {
        **fixture.env,
        "SRE_GITHUB_PRS_ENABLED": "true",
        "SRE_GITHUB_TOKEN": "synthetic-github-token",
        "SRE_JIRA_TRANSITIONS_ENABLED": "true",
        "SRE_JIRA_TRANSITIONS_JSON": json.dumps(RULES),
    }
    settings = Settings.from_env(env)
    events = []
    github = GitHubTransport(fixture, events)
    jira = TransitionTransport(fixture.ticket, events)
    runtime = ExecutionRuntime(
        settings,
        jira=JiraClient(settings, jira),
        github=GitHubClient(settings, github),
        evidence=fixture.evidence,
        tests=fixture.runtime.tests,
        clock=lambda: NOW,
    )
    return runtime, github, jira, events

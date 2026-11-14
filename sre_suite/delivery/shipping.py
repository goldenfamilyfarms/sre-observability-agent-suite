"""Reviewed CI head and atomic fast-forward with an exact ref lease."""

import os
import re
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import quote

from ..api_wire import fields, text
from ..control.client import ControlClient
from ..control.contracts import Binding, wire
from ..execution.config import https_url
from ..execution.http import JsonHttp
from ..execution.process import minimal_environment, run_process
from ..telemetry.contracts import integer


class ShippingAgent:
    def __init__(
        self,
        binding: Binding,
        control: ControlClient,
        github: JsonHttp,
        token: str,
        guard: Callable[[], None],
        heartbeat: Callable[[], None] = lambda: None,
    ) -> None:
        if binding.action != "merge":
            raise ValueError("exact merge authorization required")
        target = fields(
            binding.target,
            {
                "environment",
                "repository",
                "git_origin",
                "pull_request",
                "head_sha",
                "base_sha",
                "base_branch",
                "required_checks",
                "reviewers",
            },
        )
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", text(target["repository"])):
            raise ValueError("repository identity required")
        for key in ("head_sha", "base_sha"):
            if not re.fullmatch(r"[0-9a-f]{40}", text(target[key])):
                raise ValueError("Git commit pins required")
        branch = text(target["base_branch"], 100)
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_/-]*", branch)
            or "//" in branch
            or branch.endswith("/")
        ):
            raise ValueError("invalid base branch")
        origin = https_url(text(target["git_origin"]))
        from urllib.parse import urlsplit

        if urlsplit(origin).path:
            raise ValueError("Git origin must not contain a path")
        integer(target["pull_request"], 1, 2**31 - 1)
        checks, reviewers = target["required_checks"], target["reviewers"]
        if not isinstance(checks, list) or not 1 <= len(checks) <= 20:
            raise ValueError("required trusted checks must be explicit")
        names = set()
        for check in checks:
            item = fields(check, {"name", "app_id"})
            names.add(text(item["name"], 128))
            integer(item["app_id"], 1, 2**63 - 1)
        if (
            len(names) != len(checks)
            or not isinstance(reviewers, list)
            or not 1 <= len(reviewers) <= 20
        ):
            raise ValueError("unique checks and independent reviewers required")
        for reviewer in reviewers:
            if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", text(reviewer)):
                raise ValueError("invalid reviewer identity")
        if not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError("explicit private Git credential required")
        self.binding, self.control, self.http, self.token = binding, control, github, token
        self.target, self.external_guard, self.heartbeat = wire(target), guard, heartbeat
        self.root = "/repos/" + str(target["repository"])

    def guard(self) -> None:
        self.heartbeat()
        self.external_guard()
        self.control.require_approval(self.binding)

    def call(self, path: str) -> Any:
        self.guard()
        result = self.http.call("GET", self.root + path)
        self.guard()
        return result

    def inspect(self) -> dict[str, Any]:
        target = self.target
        pr = self.call("/pulls/" + str(target["pull_request"]))
        if (
            not isinstance(pr, dict)
            or pr.get("head", {}).get("sha") != target["head_sha"]
            or pr.get("base", {}).get("ref") != target["base_branch"]
            or pr.get("head", {}).get("repo", {}).get("full_name") != target["repository"]
            or pr.get("base", {}).get("repo", {}).get("full_name") != target["repository"]
        ):
            raise ValueError("PR source or destination changed")
        branch = self.call("/git/ref/heads/" + quote(str(target["base_branch"]), safe=""))
        head = branch.get("object", {}).get("sha")
        if pr.get("merged") is True and head == target["head_sha"]:
            return {"merged": True, "head_sha": head, "pull_request": target["pull_request"]}
        if (
            pr.get("state") != "open"
            or pr.get("draft") is not False
            or head not in {target["base_sha"], target["head_sha"]}
        ):
            raise ValueError("PR is not ready or base moved")
        checks = self.call("/commits/" + str(target["head_sha"]) + "/check-runs?per_page=100")
        runs = checks.get("check_runs") if isinstance(checks, dict) else None
        if not isinstance(runs, list) or checks.get("total_count") != len(runs) or len(runs) >= 100:
            raise ValueError("complete CI observation required")
        for required in target["required_checks"]:
            matching = [
                r
                for r in runs
                if r.get("name") == required["name"]
                and r.get("app", {}).get("id") == required["app_id"]
            ]
            if (
                len(matching) != 1
                or matching[0].get("head_sha") != target["head_sha"]
                or matching[0].get("status") != "completed"
                or matching[0].get("conclusion") != "success"
            ):
                raise ValueError("required trusted CI check did not pass for this head")
        reviews = self.call("/pulls/" + str(target["pull_request"]) + "/reviews?per_page=100")
        if not isinstance(reviews, list) or len(reviews) >= 100:
            raise ValueError("complete independent review observation required")
        latest = {}
        for review in sorted(reviews, key=lambda r: r["id"]):
            who = review.get("user", {}).get("login")
            if review.get("state") in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
                latest[who] = review
        if not any(
            who in target["reviewers"]
            and who != pr.get("user", {}).get("login")
            and r.get("state") == "APPROVED"
            and r.get("commit_id") == target["head_sha"]
            for who, r in latest.items()
        ):
            raise ValueError("approved independent review of exact head required")
        if any(r.get("state") == "CHANGES_REQUESTED" for r in latest.values()):
            raise ValueError("unresolved review changes requested")
        return {"merged": False, "base_sha": head, "head_sha": target["head_sha"]}

    def merge(self) -> dict[str, Any]:
        before = self.inspect()
        if before["merged"]:
            return before
        target = self.target
        if before["base_sha"] != target["head_sha"]:
            with TemporaryDirectory(prefix="sre-ship-") as directory:
                root = Path(directory)
                env = minimal_environment()
                # Authorization stays in environment, never argv, output or a Git config file.
                import base64

                auth = base64.b64encode(("x-access-token:" + self.token).encode()).decode()
                env.update(
                    GIT_CONFIG_NOSYSTEM="1",
                    GIT_CONFIG_GLOBAL=os.devnull,
                    GIT_TERMINAL_PROMPT="0",
                    GIT_NO_REPLACE_OBJECTS="1",
                    GIT_CONFIG_COUNT="2",
                    GIT_CONFIG_KEY_0="http.extraHeader",
                    GIT_CONFIG_VALUE_0="Authorization: Basic " + auth,
                    GIT_CONFIG_KEY_1="http.followRedirects",
                    GIT_CONFIG_VALUE_1="false",
                )
                remote = str(target["git_origin"]) + "/" + str(target["repository"]) + ".git"
                commands = [
                    ["init", "--bare", "--quiet"],
                    ["remote", "add", "origin", remote],
                    [
                        "fetch",
                        "--no-tags",
                        "--filter=blob:none",
                        "--depth=128",
                        "origin",
                        str(target["head_sha"]),
                        str(target["base_sha"]),
                    ],
                    [
                        "merge-base",
                        "--is-ancestor",
                        str(target["base_sha"]),
                        str(target["head_sha"]),
                    ],
                    [
                        "push",
                        "--porcelain",
                        "--force-with-lease=refs/heads/"
                        + str(target["base_branch"])
                        + ":"
                        + str(target["base_sha"]),
                        "origin",
                        str(target["head_sha"]) + ":refs/heads/" + str(target["base_branch"]),
                    ],
                ]
                for command in commands:
                    self.guard()
                    if command[0] == "push":
                        current = self.inspect()
                        if current["merged"]:
                            return current
                        if current["base_sha"] != target["base_sha"]:
                            raise ValueError("base changed before atomic update")
                    result = run_process(
                        [
                            "git",
                            "-c",
                            "core.hooksPath=" + os.devnull,
                            "-c",
                            "core.fsmonitor=false",
                            "-c",
                            "protocol.file.allow=never",
                            *command,
                        ],
                        root,
                        env,
                        timeout=120,
                        max_output=262144,
                        heartbeat=self.guard,
                    )
                    if result.code:
                        raise ValueError("shipping Git operation blocked; reconcile remote state")
                    self.guard()
        after = self.inspect()
        return {**after, "outcome": "merged" if after["merged"] else "awaiting_github_confirmation"}

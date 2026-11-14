"""Bounded reads of Git objects only. No checkout, build, hooks, filters or source execution."""

import hashlib
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..execution.process import ExecutionBlocked, minimal_environment, run_process
from ..execution.wire import digest
from .contracts import paths


def snapshot(
    root: Path,
    repository: str,
    revision: str,
    selected: tuple[str, ...],
    heartbeat: Callable[[], None] = lambda: None,
) -> tuple[dict[str, Any], dict[str, str]]:
    paths(list(selected))
    if (
        not root.is_absolute()
        or root.resolve() != root
        or not root.is_dir()
        or not re.fullmatch(r"[0-9a-f]{40}", revision)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
    ):
        raise ValueError("physical repository path and full revision required")
    env = minimal_environment()
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_LITERAL_PATHSPECS="1",
        GIT_TERMINAL_PROMPT="0",
    )

    deadline = time.monotonic() + 120

    def pulse() -> None:
        if time.monotonic() >= deadline:
            raise ExecutionBlocked("source read deadline exceeded")
        heartbeat()

    def git(args: list[str], limit: int) -> bytes:
        pulse()
        result = run_process(
            ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false", *args],
            root,
            env,
            timeout=10,
            max_output=limit,
            heartbeat=pulse,
        )
        if result.code:
            raise ExecutionBlocked("pinned source objects unavailable")
        return result.output

    if git(["rev-parse", "--show-toplevel"], 4096).decode().strip() != str(root):
        raise ValueError("repository root mismatch")
    if git(["cat-file", "-t", revision], 32) != b"commit\n":
        raise ValueError("revision must identify a commit")
    tree = git(["ls-tree", "-rz", "--full-tree", revision, "--", *selected], 100_000)
    entries, contents = [], {}
    total = 0
    for raw in tree.split(b"\0"):
        if not raw:
            continue
        metadata, name = raw.decode("utf-8").split("\t", 1)
        mode, kind, blob = metadata.split(" ")
        if (
            name not in selected
            or name in contents
            or mode not in {"100644", "100755"}
            or kind != "blob"
            or not re.fullmatch(r"[0-9a-f]{40}", blob)
        ):
            raise ValueError("only explicit regular source files are supported")
        size = int(git(["cat-file", "-s", blob], 32).strip())
        total += size
        if not 0 <= size <= 65536 or total > 262144:
            raise ValueError("selected source exceeds analysis budget")
        content = git(["cat-file", "blob", blob], 65536)
        if len(content) != size or b"\0" in content:
            raise ValueError("binary or inconsistent source")
        contents[name] = content.decode("utf-8")
        entries.append(
            {
                "path": name,
                "blob_sha": blob,
                "mode": mode,
                "size": size,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    if set(contents) != set(selected):
        raise ValueError("every selected file must exist at the pinned revision")
    inventory = {
        "kind": "source-snapshot-v1",
        "repository": repository,
        "revision": revision,
        "files": sorted(entries, key=lambda e: str(e["path"])),
    }
    return {**inventory, "artifact_digest": "sha256:" + digest(inventory)}, contents

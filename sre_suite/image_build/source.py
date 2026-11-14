"""Read immutable Git objects, verify the whole candidate, export only reviewed regular files."""

import hashlib
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..api_wire import fields, text
from ..execution.config import safe_path
from ..execution.process import minimal_environment, run_process
from .contracts import BuildRequest


def export(
    root: Path,
    request: BuildRequest,
    implementation: dict[str, Any],
    destination: Path,
    guard: Callable[[], None],
) -> dict[str, str]:
    if not root.is_absolute() or root.resolve() != root or not root.is_dir():
        raise ValueError("physical checkout root required")
    env = minimal_environment() | {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }

    def git(args: list[str], limit: int = 2_000_000) -> bytes:
        guard()
        result = run_process(
            ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false", *args],
            root,
            env,
            timeout=30,
            max_output=limit,
            heartbeat=guard,
        )
        if result.code:
            raise ValueError("pinned Git objects unavailable")
        return result.output

    if git(["rev-parse", "--show-toplevel"]).decode().strip() != str(root):
        raise ValueError("checkout root differs")
    base, head = request.binding.scope.revision, request.binding.target["candidate_revision"]
    commit = git(["cat-file", "commit", head]).decode().split("\n\n", 1)[0].splitlines()
    if [line[7:] for line in commit if line.startswith("parent ")] != [base]:
        raise ValueError("candidate must be one commit on the exact reviewed base")

    def tree(revision: str) -> dict[str, tuple[str, str, str]]:
        result = {}
        for row in git(["ls-tree", "-rz", "--full-tree", revision]).split(b"\0"):
            if row:
                info, path = row.decode().split("\t", 1)
                mode, kind, blob = info.split(" ")
                if path in result:
                    raise ValueError("duplicate Git path")
                result[path] = (mode, kind, blob)
        if len(result) > 10000:
            raise ValueError("source tree exceeds budget")
        return result

    before, actual = tree(base), tree(head)
    expected = dict(before)
    edits = implementation.get("edits")
    if not isinstance(edits, list) or not 1 <= len(edits) <= 50:
        raise ValueError("implementation edits required")
    seen = set()
    for edit in edits:
        item = fields(edit, {"path", "content"})
        path = safe_path(text(item["path"], 240))
        content = text(item["content"], 100000).encode()
        previous = expected.get(path, ("100644", "blob", ""))
        if path in seen or previous[:2] not in {("100644", "blob"), ("100755", "blob")}:
            raise ValueError("only unique regular file edits allowed")
        seen.add(path)
        expected[path] = (
            previous[0],
            "blob",
            hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest(),
        )
    if actual != expected or before == actual:
        raise ValueError("candidate tree differs from reviewed implementation")
    hashes, total = {}, 0
    for path in request.binding.target["recipe"]["paths"]:
        mode, kind, blob = actual[path]
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise ValueError("build context cannot contain links/submodules")
        size = int(git(["cat-file", "-s", blob], 32))
        total += size
        if size > 10_000_000 or total > 50_000_000:
            raise ValueError("build context exceeds budget")
        content = git(["cat-file", "blob", blob], 10_000_000)
        if len(content) != size:
            raise ValueError("blob size changed")
        output = destination / path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)
        output.chmod(0o755 if mode == "100755" else 0o644)
        hashes[path] = hashlib.sha256(content).hexdigest()
    guard()
    return hashes

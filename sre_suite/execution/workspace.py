"""Safe commit export, bounded patch generation and network-disabled container QA."""

import difflib
import hashlib
import io
import os
import tarfile
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from .config import Settings, safe_path
from .process import ExecutionBlocked, minimal_environment, run_process
from .wire import digest


def export_commit(
    settings: Settings, revision: str, target: Path, heartbeat: Callable[[], None]
) -> None:
    import re

    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("full revision required")
    env = minimal_environment()
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    result = run_process(
        [
            "git",
            "-c",
            "core.hooksPath=" + os.devnull,
            "-c",
            "core.fsmonitor=false",
            "archive",
            "--format=tar",
            revision,
        ],
        settings.repository_path,
        env,
        timeout=30,
        max_output=25_000_000,
        heartbeat=heartbeat,
    )
    if result.code:
        raise ExecutionBlocked("configured repository does not contain the requested revision")
    with tarfile.open(fileobj=io.BytesIO(result.output), mode="r:") as archive:
        members = archive.getmembers()
        if len(members) > 2000:
            raise ExecutionBlocked("repository exceeds pilot file limit")
        total = 0
        for member in members:
            if any(
                p in {".git", ".github", ".claude", ".env"} or p.startswith(".env.")
                for p in Path(member.name).parts
            ):
                continue
            name = safe_path(member.name.rstrip("/"))
            if member.isdir():
                (target / name).mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ExecutionBlocked("repository links/special files are not supported")
            total += member.size
            if member.size > 2_000_000 or total > 20_000_000:
                raise ExecutionBlocked("repository exceeds pilot size limit")
            source = archive.extractfile(member)
            if source is None:
                raise ExecutionBlocked("invalid Git archive")
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read())
            destination.chmod(0o755 if member.mode & 0o111 else 0o644)


def selected_source(
    settings: Settings, revision: str, heartbeat: Callable[[], None]
) -> dict[str, str | None]:
    with TemporaryDirectory(prefix="sre-source-") as directory:
        root = Path(directory)
        export_commit(settings, revision, root, heartbeat)
        result = {
            path: (root / path).read_text() if (root / path).exists() else None
            for path in settings.allowed_files
        }
        if sum(len(value.encode()) for value in result.values() if value is not None) > 250000:
            raise ExecutionBlocked("selected source exceeds prompt budget")
        return result


def make_patch(source: dict[str, str | None], edits: list[dict[str, str]]) -> str:
    result = ""
    for edit in edits:
        path = edit["path"]
        original = source[path]
        if edit["content"] == original:
            continue
        lines = difflib.unified_diff(
            (original or "").splitlines(keepends=True),
            edit["content"].splitlines(keepends=True),
            fromfile="/dev/null" if original is None else "a/" + path,
            tofile="b/" + path,
        )
        result += f"diff --git a/{path} b/{path}\n"
        if original is None:
            content = edit["content"].encode()
            blob = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
            result += f"new file mode 100644\nindex {'0' * 40}..{blob}\n"
        for line in lines:
            result += line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
    if not result:
        raise ExecutionBlocked("implementation produced no changes")
    return result


# The timeout runs inside the container too: a dead host worker cannot leave tests
# consuming resources forever. The image must provide Python 3 and the test tools.
CONTAINER_WATCHDOG = """
import os, signal, subprocess, sys
process = subprocess.Popen(sys.argv[2:], start_new_session=True)
try:
    sys.exit(process.wait(timeout=int(sys.argv[1])))
except subprocess.TimeoutExpired:
    os.killpg(process.pid, signal.SIGKILL)
    process.wait()
    sys.exit(124)
"""


class ContainerTests:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def execute(
        self,
        revision: str,
        edits: list[dict[str, str]],
        heartbeat: Callable[[], None] = lambda: None,
    ) -> dict[str, Any]:
        with TemporaryDirectory(prefix="sre-qa-") as directory:
            root = Path(directory)
            export_commit(self.settings, revision, root, heartbeat)
            for edit in edits:
                path = safe_path(edit["path"])
                if path not in self.settings.allowed_files:
                    raise ValueError("unauthorized edit")
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
                target.write_text(edit["content"])
                target.chmod(mode)
            root.chmod(0o755)
            name = "sre-qa-" + uuid4().hex
            argv = [
                "docker",
                "run",
                "--rm",
                "--name",
                name,
                "--pull=never",
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--pids-limit=128",
                "--memory=512m",
                "--cpus=1",
                "--user=65534:65534",
                "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",
                "--mount",
                f"type=bind,source={root},target=/workspace,readonly",
                "--workdir=/workspace",
                "--env=PYTHONDONTWRITEBYTECODE=1",
                "--entrypoint=python",
                self.settings.test_image,
                "-c",
                CONTAINER_WATCHDOG,
                str(self.settings.timeout),
                *self.settings.test_command,
            ]
            try:
                result = run_process(
                    argv,
                    root,
                    minimal_environment(),
                    timeout=self.settings.timeout + 15,
                    max_output=500000,
                    heartbeat=heartbeat,
                )
            finally:
                # Exact task-owned random container only; never prune other containers.
                run_process(
                    ["docker", "rm", "--force", name], root, minimal_environment(), timeout=20
                )
            output = (result.output + result.stderr).decode("utf-8", errors="replace")
            return {
                "passed": result.code == 0,
                "exit_code": result.code,
                "output": output,
                "output_digest": digest(output),
                "command": list(self.settings.test_command),
                "image": self.settings.test_image,
            }

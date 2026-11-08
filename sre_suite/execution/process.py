"""Bounded subprocess I/O and process-group cancellation; never invoke a shell."""

import os
import selectors
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


class ExecutionBlocked(Exception):
    pass


@dataclass(frozen=True)
class ProcessResult:
    code: int
    output: bytes
    stderr: bytes = b""


def run_process(
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    *,
    input_bytes: bytes = b"",
    timeout: int = 300,
    max_output: int = 2_000_000,
    heartbeat: Callable[[], None] = lambda: None,
) -> ProcessResult:
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    output = bytearray()
    errors = bytearray()
    size = 0
    pending = memoryview(input_bytes)
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdin, process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
            if pending:
                selector.register(process.stdin, selectors.EVENT_WRITE)
            else:
                process.stdin.close()
            selector.register(process.stdout, selectors.EVENT_READ)
            selector.register(process.stderr, selectors.EVENT_READ)
            while selector.get_map() or process.poll() is None:
                heartbeat()
                if time.monotonic() >= deadline:
                    raise ExecutionBlocked("worker exceeded its time limit")
                for key, _ in selector.select(timeout=0.2):
                    ready_stream = key.fileobj
                    if ready_stream is process.stdin:
                        try:
                            count = os.write(process.stdin.fileno(), pending[:65536])
                            pending = pending[count:]
                        except BrokenPipeError:
                            pending = memoryview(b"")
                        if not pending:
                            selector.unregister(ready_stream)
                            process.stdin.close()
                    else:
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(ready_stream)
                            continue
                        size += len(chunk)
                        if size > max_output:
                            raise ExecutionBlocked("worker exceeded its output limit")
                        if ready_stream is process.stdout:
                            output.extend(chunk)
                        else:
                            errors.extend(chunk)
            return ProcessResult(
                process.wait(timeout=max(0.1, deadline - time.monotonic())),
                bytes(output),
                bytes(errors),
            )
    finally:
        # Kill descendants too, including those that closed inherited pipes early.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def minimal_environment() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}

"""Private durable stage artifacts. All worker hosts must share this filesystem."""

import fcntl
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from .wire import canonical, digest


class Journal:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.is_symlink() or root.stat().st_mode & 0o077:
            raise ValueError("execution journal requires an owner-only directory")

    def compute(
        self,
        key: str,
        request: object,
        operation: Callable[[], dict[str, Any]],
        heartbeat: Callable[[], None] = lambda: None,
    ) -> dict[str, Any]:
        name = digest(key)
        path = self.root / (name + ".json")
        with (self.root / (name + ".lock")).open("a+b") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    heartbeat()
                    time.sleep(0.2)
            try:
                if path.exists():
                    stored = json.loads(path.read_text())
                    if stored["request"] != digest(request) or stored["checksum"] != digest(
                        stored["result"]
                    ):
                        raise ValueError("stage artifact mismatch; stop and reconcile")
                    result: dict[str, Any] = stored["result"]
                    return result
                result = operation()
                encoded = canonical(
                    {"request": digest(request), "checksum": digest(result), "result": result}
                )
                if len(encoded.encode()) > 1_000_000:
                    raise ValueError("stage artifact too large")
                temporary = self.root / (name + "." + uuid4().hex + ".tmp")
                try:
                    with temporary.open("x", encoding="utf-8") as stream:
                        temporary.chmod(0o600)
                        stream.write(encoded)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, path)
                    descriptor = os.open(self.root, os.O_RDONLY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                finally:
                    temporary.unlink(missing_ok=True)
                return result
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def read(self, key: str, checksum: str) -> dict[str, Any]:
        path = self.root / (digest(key) + ".json")
        if path.stat().st_size > 1_000_000:
            raise ValueError("stage artifact too large")
        stored = json.loads(path.read_text())
        if stored["checksum"] != checksum or digest(stored["result"]) != checksum:
            raise ValueError("stage artifact digest mismatch")
        result: dict[str, Any] = stored["result"]
        return result

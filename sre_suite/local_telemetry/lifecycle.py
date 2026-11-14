"""Owned local containers, persistent private state and foreground gateway supervision."""

import json
import os
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..execution.http import JsonHttp
from ..local_pilot.lifecycle import StartupError, command, require_local_docker, stack_lock
from ..telemetry.schedule_config import private_path
from ..verification.config import private_document
from .config import IMAGES, Config
from .gateway import server


def arguments(root: Path, config: Config, backend: str) -> list[str]:
    bind = "127.0.0.1" if config.mode == "native" else "0.0.0.0"
    argv = [
        "docker",
        "run",
        "--detach",
        "--pull=never",
        "--name",
        config.name + "-" + backend,
        "--label",
        "sre.telemetry.id=" + config.identity,
        "--label",
        "sre.telemetry.backend=" + backend,
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--cap-drop=ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--pids-limit",
        "256",
        "--memory",
        "768m",
        "--cpus",
        "2",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
    ]
    if config.mode == "native":
        argv += ["--network", "host"]
    else:
        for port in (
            [config.ports[backend], config.ports["otlp"]]
            if backend == "tempo"
            else [config.ports[backend]]
        ):
            argv += ["--publish", f"127.0.0.1:{port}:{port}"]
    mounts = [
        (root / (backend + ".json"), "/config/backend.json", True),
        (root / (backend + "-data"), "/data", False),
    ]
    if backend == "prometheus":
        mounts += [
            (root / "target.crt", "/config/target.crt", True),
            (root / "metrics.token", "/config/metrics.token", True),
        ]
    for source, destination, readonly in mounts:
        if "," in str(source):
            raise ValueError("invalid mount path")
        private_path(source, directory=not readonly)
        argv += [
            "--mount",
            f"type=bind,src={source},dst={destination}" + (",readonly" if readonly else ""),
        ]
    argv += [IMAGES[backend]]
    if backend == "prometheus":
        argv += [
            "--config.file=/config/backend.json",
            "--storage.tsdb.path=/data",
            "--storage.tsdb.retention.time=24h",
            "--storage.tsdb.retention.size=256MB",
            f"--web.listen-address={bind}:{config.ports[backend]}",
        ]
    else:
        argv += ["-config.file=/config/backend.json"]
    return argv


def owned(config: Config, backend: str) -> dict[str, Any] | None:
    name = config.name + "-" + backend
    names = command(["docker", "container", "ls", "-a", "--format", "{{.Names}}"])
    if name not in names.splitlines():
        return None
    info: dict[str, Any] = json.loads(command(["docker", "inspect", name]))[0]
    labels = info["Config"].get("Labels", {})
    if (
        labels.get("sre.telemetry.id") != config.identity
        or labels.get("sre.telemetry.backend") != backend
    ):
        raise ValueError("container ownership mismatch")
    return info


def cleanup(config: Config, backends: list[str]) -> None:
    failed = False
    for backend in reversed(backends):
        try:
            info = owned(config, backend)
            if info:
                command(["docker", "stop", "--time", "10", info["Id"]], timeout=20)
                command(["docker", "rm", info["Id"]])
        except Exception:
            failed = True
    if failed:
        raise StartupError("telemetry_cleanup")


@contextmanager
def running(
    root: Path, config: Config, *, authorized: bool, stopping: Callable[[], bool] = lambda: False
) -> Iterator[dict[str, str]]:
    if not authorized:
        raise ValueError("explicit local telemetry startup authorization required")
    if config.mode == "native" and sys.platform != "linux":
        raise ValueError("native mode requires a Linux host; use desktop mode on macOS")
    private_path(root, directory=True)
    if Config.load(root) != config:
        raise ValueError("stack configuration changed")
    threads: list[threading.Thread] = []
    listeners = []
    started: list[str] = []
    with stack_lock(root):
        require_local_docker()
        # Refuse occupied ports or a running old owner; never adopt unrelated containers.
        for backend in IMAGES:
            info = owned(config, backend)
            if info:
                if info["State"]["Running"]:
                    raise ValueError("an owned backend is already running; stop it explicitly")
                command(["docker", "rm", info["Id"]])
        sockets = []
        try:
            for port in config.ports.values():
                s = socket.socket()
                sockets.append(s)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", port))
        finally:
            for s in sockets:
                s.close()
        for image in IMAGES.values():
            command(["docker", "image", "inspect", image])
        try:
            for backend in IMAGES:
                started.append(backend)  # Include a lost create response in owned cleanup.
                command(arguments(root, config, backend))
                listener = server(root, config, backend)
                listeners.append(listener)
                thread = threading.Thread(target=listener.serve_forever, daemon=True)
                threads.append(thread)
                thread.start()
            tokens = private_document(root / "tokens.json")
            assert isinstance(tokens, dict)
            deadline = time.monotonic() + 90
            for backend in IMAGES:
                http = JsonHttp(
                    config.url(backend),
                    tokens[backend + "_read"],
                    local_ca_file=str(root / "server.crt"),
                    timeout=3,
                )
                while True:
                    if stopping():
                        raise InterruptedError("startup stopped")
                    info = owned(config, backend)
                    if info is None or not info["State"]["Running"]:
                        raise StartupError("telemetry_backend")
                    try:
                        if http.call("GET", "/ready") == {"status": "ready"}:
                            break
                    except Exception:
                        pass
                    if time.monotonic() >= deadline:
                        raise StartupError("telemetry_readiness")
                    time.sleep(0.25)
            yield {b: config.url(b) for b in IMAGES}
        finally:
            for listener in listeners:
                listener.shutdown()
                listener.server_close()
            for thread in threads:
                thread.join(timeout=6)
            cleanup(config, started)


def up(
    root: Path,
    config: Config,
    *,
    authorized: bool,
    notify: Callable[[dict[str, Any]], None],
    stopping: Callable[[], bool],
) -> None:
    with running(root, config, authorized=authorized, stopping=stopping) as endpoints:
        notify({"status": "ready", "endpoints": endpoints, "execution_started": False})
        while not stopping():
            for backend in IMAGES:
                info = owned(config, backend)
                if info is None or not info["State"]["Running"]:
                    raise StartupError("telemetry_backend")
            time.sleep(1)


def stop_stack(root: Path, *, authorized: bool) -> None:
    if not authorized:
        raise ValueError("explicit stop authorization required")
    config = Config.load(root)
    with stack_lock(root):
        require_local_docker()
        cleanup(config, list(IMAGES))

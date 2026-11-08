"""Foreground local supervisor with explicit migrations and preserved persistent state."""

import asyncio
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..execution.config import Settings
from ..execution.http import JsonHttp
from ..execution.pilot import _private
from .config import LocalConfig, base_environment, dsn, execution_environment


class StartupError(Exception):
    def __init__(self, component: str) -> None:
        self.component = component
        super().__init__("local pilot failed: " + component)


def command(argv: list[str], *, timeout: int = 30) -> str:
    result = subprocess.run(
        argv, capture_output=True, timeout=timeout, check=True, env=base_environment()
    )
    if len(result.stdout) > 1_000_000:
        raise ValueError("unexpected command output size")
    return result.stdout.decode().strip()


@contextmanager
def stack_lock(root: Path) -> Iterator[None]:
    fd = os.open(root / "stack.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "rb") as file:
        _private(root / "stack.lock", directory=False)
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def check_ports(config: LocalConfig) -> None:
    sockets = []
    try:
        for port in (config.postgres_port, config.temporal_port, config.api_port):
            listener = socket.socket()
            sockets.append(listener)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", port))
    finally:
        for listener in sockets:
            listener.close()


def require_local_docker() -> None:
    """Do not turn a local pilot request into a remote Docker operation."""
    env = base_environment()
    if env.get("DOCKER_CONTEXT"):
        context = json.loads(command(["docker", "context", "inspect", env["DOCKER_CONTEXT"]]))
        endpoint = context[0]["Endpoints"]["docker"]["Host"]
    elif env.get("DOCKER_HOST"):
        endpoint = env["DOCKER_HOST"]
    else:
        context = json.loads(command(["docker", "context", "inspect"]))
        endpoint = context[0]["Endpoints"]["docker"]["Host"]
    if not isinstance(endpoint, str) or not endpoint.startswith("unix:///"):
        raise ValueError("local pilot requires a local Unix-socket Docker endpoint")


def volume(config: LocalConfig) -> str:
    name = config.name + "-postgres"
    # Listing is read-only; never adopt or delete an unrelated Docker volume.
    names = command(["docker", "volume", "ls", "--format", "{{.Name}}"]).splitlines()
    if name in names:
        info = json.loads(command(["docker", "volume", "inspect", name]))
        if info[0].get("Labels", {}).get("sre.pilot.id") != config.identity:
            raise ValueError("volume ownership mismatch")
    else:
        command(["docker", "volume", "create", "--label", "sre.pilot.id=" + config.identity, name])
    return name


def postgres_args(root: Path, config: LocalConfig, volume_name: str) -> list[str]:
    if "," in str(root):
        raise ValueError("Docker bind paths must not contain commas")
    return [
        "docker",
        "run",
        "--detach",
        "--rm",
        "--pull=never",
        "--name",
        config.name,
        "--label",
        "sre.pilot.id=" + config.identity,
        "--publish",
        f"127.0.0.1:{config.postgres_port}:5432",
        "--mount",
        f"type=volume,src={volume_name},dst=/var/lib/postgresql/data",
        "--mount",
        f"type=bind,src={root / 'postgres.password'},dst=/run/secrets/postgres-password,readonly",
        "--env",
        "POSTGRES_USER=sre_pilot",
        "--env",
        "POSTGRES_DB=sre_pilot",
        "--env",
        "POSTGRES_PASSWORD_FILE=/run/secrets/postgres-password",
        config.postgres_image,
    ]


def temporal_args(root: Path, config: LocalConfig) -> list[str]:
    return [
        config.temporal_binary,
        "server",
        "start-dev",
        "--ip",
        "127.0.0.1",
        "--port",
        str(config.temporal_port),
        "--headless",
        "--namespace",
        config.name,
        "--db-filename",
        str(root / "temporal" / "state.db"),
        "--log-level",
        "error",
    ]


def spawn(argv: list[str], env: dict[str, str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        argv,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        umask=0o077,
    )


def stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)


def cleanup(children: list[subprocess.Popen[bytes]], container: str | None) -> None:
    failed = False
    for child in reversed(children):
        try:
            stop_process(child)
        except Exception:
            failed = True
    if container is not None:
        try:
            command(["docker", "stop", "--time", "10", container], timeout=20)
        except Exception:
            failed = True
    if failed:
        raise StartupError("cleanup")


def wait_ready(
    probe: Callable[[], None],
    *,
    processes: list[subprocess.Popen[bytes]],
    timeout: float = 60,
    stopping: Callable[[], bool] = lambda: False,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stopping():
            raise KeyboardInterrupt
        if any(p.poll() is not None for p in processes):
            raise RuntimeError("service exited during startup")
        try:
            probe()
            return
        except Exception:
            time.sleep(0.25)
    raise TimeoutError("service did not become ready")


def postgres_ready(root: Path, config: LocalConfig) -> None:
    import psycopg

    with psycopg.connect(dsn(root, config), connect_timeout=2) as db:
        version = int(db.execute("SHOW server_version_num").fetchone()[0])  # type: ignore[index]
        if not 160000 <= version < 180000:
            raise ValueError("local pilot supports PostgreSQL 16 or 17")
        db.execute("SELECT 1")


async def temporal_ready(config: LocalConfig, *, worker: bool = False) -> None:
    from temporalio.api.enums.v1 import TaskQueueType
    from temporalio.api.taskqueue.v1 import TaskQueue
    from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest, DescribeTaskQueueRequest
    from temporalio.client import Client

    async def probe() -> None:
        client = await Client.connect(f"127.0.0.1:{config.temporal_port}", namespace=config.name)
        await client.workflow_service.describe_namespace(
            DescribeNamespaceRequest(namespace=config.name)
        )
        if worker:
            for kind in (
                TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
                TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY,
            ):
                result = await client.workflow_service.describe_task_queue(
                    DescribeTaskQueueRequest(
                        namespace=config.name,
                        task_queue=TaskQueue(name=config.name),
                        task_queue_type=kind,
                    )
                )
                if not result.pollers:
                    raise ValueError("worker has no registered poller")

    await asyncio.wait_for(probe(), timeout=5)


def api_ready(root: Path, config: LocalConfig) -> None:
    result = JsonHttp(config.url, None, local_ca_file=str(root / "server.crt"), timeout=3).call(
        "GET", "/healthz"
    )
    if result != {"status": "ready"}:
        raise ValueError("API readiness failed")


def health(root: Path, config: LocalConfig, *, worker: bool = False) -> dict[str, str]:
    checks: dict[str, Callable[[], None]] = {
        "postgres": lambda: postgres_ready(root, config),
        "temporal": lambda: asyncio.run(temporal_ready(config, worker=worker)),
        "https_api": lambda: api_ready(root, config),
    }
    result = {}
    for name, probe in checks.items():
        try:
            probe()
            result[name] = "ready"
        except Exception:
            result[name] = "unavailable"
    return result


def up(
    root: Path,
    config: LocalConfig,
    worker_file: Path | None,
    *,
    notify: Callable[[dict[str, Any]], None],
    stopping: Callable[[], bool],
) -> None:
    phase = "configuration"
    children: list[subprocess.Popen[bytes]] = []
    container: str | None = None
    try:
        worker_env = execution_environment(root, config, worker_file) if worker_file else None
        if worker_env is not None:
            Settings.from_env(worker_env)
        with stack_lock(root):
            require_local_docker()
            check_ports(config)
            phase = "postgres_image"
            command(["docker", "image", "inspect", config.postgres_image])
            phase = "postgres"
            storage = volume(config)
            container = command(postgres_args(root, config, storage))
            try:
                wait_ready(
                    lambda: postgres_ready(root, config), processes=children, stopping=stopping
                )
                phase = "migrations"
                from ..postgres import migrate

                migrate(dsn(root, config))
                phase = "temporal"
                children.append(spawn(temporal_args(root, config), base_environment()))
                wait_ready(
                    lambda: asyncio.run(temporal_ready(config)),
                    processes=children,
                    stopping=stopping,
                )
                phase = "https_api"
                children.append(
                    spawn(
                        [
                            sys.executable,
                            "-m",
                            "sre_suite.local_pilot",
                            "serve",
                            "--directory",
                            str(root),
                        ],
                        base_environment(),
                    )
                )
                wait_ready(lambda: api_ready(root, config), processes=children, stopping=stopping)
                if worker_env is not None:
                    phase = "worker"
                    children.append(
                        spawn(
                            [sys.executable, "-m", "sre_suite.execution.cli", "worker"], worker_env
                        )
                    )
                    wait_ready(
                        lambda: asyncio.run(temporal_ready(config, worker=True)),
                        processes=children,
                        stopping=stopping,
                    )
                notify(
                    {
                        "status": "ready",
                        "worker_started": worker_env is not None,
                        "execution_started": False,
                        "services": health(root, config),
                    }
                )
                phase = "supervision"
                while not stopping():
                    if any(p.poll() is not None for p in children):
                        raise RuntimeError("child service exited")
                    if (
                        command(["docker", "inspect", "--format", "{{.State.Running}}", container])
                        != "true"
                    ):
                        raise RuntimeError("database container exited")
                    time.sleep(1)
            finally:
                cleanup(children, container)
    except StartupError:
        raise
    except Exception:
        raise StartupError(phase) from None

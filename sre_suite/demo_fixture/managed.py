"""Private managed target policy, bounded local lifetime and durable export receipt."""

import math
import os
import signal
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..api_wire import envelope, fields, text
from ..execution.http import JsonHttp
from ..execution.journal import Journal
from ..execution.wire import digest
from ..fixtures.contracts import FixturePolicy, FixtureRequest
from ..telemetry.schedule_config import private_path
from ..verification.config import client, environment, private_document
from .controller import Controller, recipe, validate_target
from .export import flush, transports
from .managed_contracts import demo_dispatch, ready_receipt
from .scenarios import reviewed
from .server import server


class ManagedPolicy:
    def __init__(self, raw: object) -> None:
        row = envelope(
            raw,
            {
                "fixture_policy",
                "recipe",
                "task_queue",
                "startup_seconds",
                "settle_seconds",
                "certificate",
                "private_key",
                "export",
            },
        )
        self.fixture = FixturePolicy.parse(row["fixture_policy"])
        reviewed(self.fixture)
        self.recipe = fields(row["recipe"], {"kind", "repository", "base_revision", "edits"})
        if self.recipe != recipe(
            text(self.recipe["repository"]), self.fixture.verification.scope.revision
        ):
            raise ValueError("managed target requires the exact installed demo recipe")
        parsed = urlsplit(self.fixture.base_url)
        if (
            parsed.hostname not in {"localhost", "127.0.0.1"}
            or self.fixture.ca_env is None
            or parsed.port is None
            or not 1024 <= parsed.port <= 65535
        ):
            raise ValueError("managed target requires an explicit loopback HTTPS port and CA")
        self.port = parsed.port
        self.certificate = Path(text(row["certificate"]))
        self.private_key = Path(text(row["private_key"]))
        for path in (self.certificate, self.private_key):
            private_path(path, directory=False)
        self.export = row["export"]
        # Validate transport shapes and origins without loading credentials or connecting.
        placeholders = {
            "SRE_DEMO_TRACES_AUTH": "Bearer validation-only",
            "SRE_DEMO_LOGS_AUTH": "Bearer validation-only",
            "SRE_DEMO_TRACES_TENANT": "validation",
            "SRE_DEMO_LOGS_TENANT": "validation",
            "SRE_DEMO_EXPORT_CA_FILE": str(self.certificate),
        }
        transports(self.export, placeholders)
        self.dispatch = demo_dispatch(
            {
                "policy_sha256": digest(row),
                "task_queue": row["task_queue"],
                "startup_seconds": row["startup_seconds"],
                "settle_seconds": row["settle_seconds"],
            }
        )
        if self.dispatch["task_queue"] in {self.fixture.runner_queue, self.fixture.assessor_queue}:
            raise ValueError("demo lifecycle requires its own queue")

    def authorize(self, raw: object) -> FixtureRequest:
        request = FixtureRequest.parse(raw)
        validate_target(self.fixture, request, self.recipe)
        if request.demo != self.dispatch:
            raise ValueError("lifecycle dispatch differs from private authorization")
        return request

    @classmethod
    def load(cls, path: Path) -> "ManagedPolicy":
        return cls(private_document(path, 1_048_576))


def role_environment(
    policy: ManagedPolicy, operation: str, env: Mapping[str, str]
) -> dict[str, str]:
    result = environment(env)
    keys: set[str] = set()
    if operation == "serve":
        keys = {"SRE_DEMO_FIXTURE_AUTH", "SRE_DEMO_METRICS_AUTH", "SRE_DEMO_LIFECYCLE_AUTH"}
    elif operation == "ready":
        keys = {"SRE_DEMO_LIFECYCLE_AUTH"}
    elif operation == "export":
        keys = {
            "SRE_DEMO_TRACES_AUTH",
            "SRE_DEMO_LOGS_AUTH",
            "SRE_DEMO_TRACES_TENANT",
            "SRE_DEMO_LOGS_TENANT",
            "SRE_DEMO_EXPORT_CA_FILE",
        }
    else:
        raise ValueError("unknown managed operation")
    return {**result, **{k: v for k, v in env.items() if k in keys}}


def readiness(request: FixtureRequest, policy: ManagedPolicy) -> dict[str, Any]:
    return ready_receipt(request.identity, policy.dispatch, request.binding)


class ManagedRuntime:
    def __init__(
        self,
        policy: ManagedPolicy,
        raw: object,
        root: Path,
        env: Mapping[str, str],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.policy, self.raw, self.env, self.clock = policy, raw, env, clock
        self.request = policy.authorize(raw)
        private_path(root, directory=True)
        self.evidence = client(env)
        self.fence()
        self.root = root / self.request.identity
        self.root.mkdir(mode=0o700, exist_ok=True)
        private_path(self.root, directory=True)
        self.journal = Journal(self.root / "managed-journal")
        # Serve and readiness start concurrently in separate processes. Serialize first-open
        # schema/identity initialization before either can use the same controller database.
        with self.journal.locked("initialize", self.fence):
            self.controller = Controller(
                policy.fixture, raw, policy.recipe, self.root, self.evidence, clock=clock
            )

    def fence(self) -> None:
        if (
            not self.request.start - timedelta(seconds=self.policy.dispatch["startup_seconds"])
            <= self.clock()
            <= self.request.end + timedelta(seconds=120)
        ):
            raise ValueError("managed lifecycle is outside its authorized interval")
        self.evidence.context(self.request.scope, self.request.binding["context_version"])

    def ready(self) -> dict[str, Any]:
        self.fence()
        http = JsonHttp(
            self.policy.fixture.base_url,
            self.env["SRE_DEMO_LIFECYCLE_AUTH"],
            local_ca_file=str(self.policy.certificate),
            timeout=3,
            expected_status=200,
        )
        result = http.call("GET", "/ready")
        if result != readiness(self.request, self.policy):
            raise ValueError("listener belongs to another target or policy")
        self.fence()
        return readiness(self.request, self.policy)

    def serve(self, *, parent_pid: int | None = None) -> dict[str, Any]:
        self.fence()
        # A worker crash must not leave an orphan listener alive through its observation window.
        stopped = threading.Event()

        def watch_parent() -> None:
            while not stopped.wait(0.25):
                if parent_pid is not None and os.getppid() != parent_pid:
                    os.kill(os.getpid(), signal.SIGTERM)
                    return

        watcher = threading.Thread(target=watch_parent, daemon=True)
        watcher.start()
        try:
            with self.journal.locked("listener", self.fence):

                def ready() -> dict[str, Any]:
                    self.fence()
                    return readiness(self.request, self.policy)

                with server(
                    self.controller,
                    self.env["SRE_DEMO_FIXTURE_AUTH"],
                    self.env["SRE_DEMO_METRICS_AUTH"],
                    str(self.policy.certificate),
                    str(self.policy.private_key),
                    self.policy.port,
                    lifecycle_token=self.env["SRE_DEMO_LIFECYCLE_AUTH"],
                    ready=ready,
                ) as listener:
                    listener.timeout = 0.25
                    next_check = self.clock()
                    while self.clock() < self.request.end + timedelta(seconds=120):
                        if self.clock() >= next_check:
                            self.fence()
                            next_check = self.clock() + timedelta(seconds=2)
                        listener.handle_request()
            return {"status": "expired", "workflow_id": self.request.identity}
        finally:
            stopped.set()
            watcher.join(timeout=1)

    def export(self) -> dict[str, Any]:
        self.fence()
        cutoff = self.request.end - timedelta(seconds=self.policy.dispatch["settle_seconds"])

        def guard() -> None:
            self.fence()
            if not self.request.start <= self.clock() < cutoff:
                raise ValueError("export deadline leaves no ingestion settlement time")

        def deliver() -> dict[str, Any]:
            guard()
            with self.controller.connect() as conn:
                executions = conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
                rows = conn.execute(
                    "SELECT key,signal,payload FROM outbox ORDER BY key,signal"
                ).fetchall()
            if executions != 3 or len(rows) != 2 or {r[1] for r in rows} != {"logs", "traces"}:
                raise ValueError("export requires the complete reviewed fixture run")
            flush(self.controller, transports(self.policy.export, self.env), self.journal, guard)
            guard()
            with self.controller.connect() as conn:
                if conn.execute("SELECT COUNT(*) FROM outbox WHERE delivered=1").fetchone()[0] != 2:
                    raise ValueError("export is incomplete")
            return {
                "workflow_id": self.request.identity,
                "policy_sha256": self.policy.dispatch["policy_sha256"],
                "outbox_sha256": digest(rows),
                "delivered_batches": 2,
                "completed_at": self.clock().isoformat(),
            }

        receipt = self.journal.compute(
            "export-complete",
            {"run": self.request.identity, "policy": self.policy.dispatch},
            deliver,
            self.fence,
        )
        self.fence()
        if not self.request.start <= datetime.fromisoformat(receipt["completed_at"]) < cutoff:
            raise ValueError("cached export receipt missed the fixed deadline")
        return receipt

    def remaining_seconds(self) -> int:
        return min(
            485,
            max(
                1,
                math.ceil(
                    (self.request.end + timedelta(seconds=125) - self.clock()).total_seconds()
                ),
            ),
        )

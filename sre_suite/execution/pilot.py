"""Explicit preview registration and baseline bootstrap; never starts an execution."""

import fcntl
import hashlib
import os
import stat
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..api_wire import decode, envelope, fields, registration_from_wire, text
from ..auth import token_digest
from ..contracts import Evidence
from ..previews import PreviewRequest
from .config import https_url
from .evidence_client import EvidenceClient
from .http import JsonHttp
from .runtime import execution_key
from .wire import canonical, digest

FILES = {"execution.json", "baseline-evidence.json", "receipt.json"}


@dataclass(frozen=True)
class PilotManifest:
    preview: PreviewRequest
    expected_generation: int
    ticket_key: str
    run_id: str
    baseline: Evidence

    @classmethod
    def parse(cls, value: object, now: datetime) -> "PilotManifest":
        raw = envelope(value, {"registration", "ticket_key", "run_id", "baseline"})
        preview, expected = registration_from_wire(raw["registration"])
        baseline = fields(raw["baseline"], {"sha256", "observed_at", "expires_at"})
        evidence = Evidence(
            "baseline",
            preview.scope,
            "urn:sre:pilot:baseline",
            text(baseline["sha256"], 64),
            datetime.fromisoformat(text(baseline["observed_at"], 64)),
            datetime.fromisoformat(text(baseline["expires_at"], 64)),
            "pending-authentication",
        )
        if not preview.requested_at <= now < preview.expires_at:
            raise ValueError("inactive preview window")
        if not evidence.valid_for(preview.scope, now) or evidence.expires_at > preview.expires_at:
            raise ValueError("baseline must be current and expire within the preview window")
        result = cls(
            preview, expected, text(raw["ticket_key"], 80), text(raw["run_id"], 80), evidence
        )
        execution_key(result.spec())
        return result

    def spec(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "ticket_key": self.ticket_key,
            "run_id": self.run_id,
            "scope": asdict(self.preview.scope),
        }

    def registration(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "preview": asdict(self.preview),
            "expected_generation": self.expected_generation,
        }

    def fingerprint(self) -> str:
        return digest(
            {
                "registration": self.registration(),
                "spec": self.spec(),
                "baseline": asdict(self.baseline),
            }
        )


class PilotSetupError(Exception):
    def __init__(self, phase: str) -> None:
        self.phase = phase
        super().__init__("pilot setup failed during " + phase)


def _read(path: Path, limit: int) -> bytes:
    # Do not follow a final symlink or block on a device/FIFO masquerading as a file.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("expected a regular file")
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError("file too large")
    return data


def _private(path: Path, *, directory: bool) -> None:
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("output paths must be private and owned by this user")
    if not directory and info.st_nlink != 1:
        raise ValueError("output must not be hard-linked")


@contextmanager
def _destination(path: Path, fingerprint: str) -> Iterator[Path]:
    path = path.absolute()
    # A private operator-owned parent is required; never place pilot artifacts in a repo.
    if ".." in path.parts:
        raise ValueError("ambiguous output path")
    for parent in (path, *path.parents):
        if parent.is_symlink() or (parent / ".git").exists():
            raise ValueError("output must be outside Git checkouts and symlinks")
    _private(path.parent, directory=True)
    lock = path.parent / (".pilot-" + digest(path.name) + ".lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "rb") as handle:
        _private(lock, directory=False)
        fcntl.flock(handle, fcntl.LOCK_EX)
        if path.exists():
            _private(path, directory=True)
            if {item.name for item in path.iterdir()} != FILES:
                raise ValueError("output directory is not a complete pilot bundle")
            for name in FILES:
                _private(path / name, directory=False)
            receipt = envelope(
                decode(_read(path / "receipt.json", 32_768)), {"setup_sha256", "workflow_id"}
            )
            if receipt["setup_sha256"] != fingerprint:
                raise ValueError("output belongs to another setup request")
        yield path


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish(path: Path, artifacts: dict[str, object]) -> None:
    contents = {name: (canonical(value) + "\n").encode() for name, value in artifacts.items()}
    if path.exists():
        if any(_read(path / name, 32_768) != content for name, content in contents.items()):
            raise ValueError("existing output differs from verified setup")
        _sync_directory(path)
        _sync_directory(path.parent)
        return
    with tempfile.TemporaryDirectory(prefix=".pilot-pending-", dir=path.parent) as temporary:
        bundle = Path(temporary) / "bundle"
        bundle.mkdir(mode=0o700)
        for name, content in contents.items():
            fd = os.open(bundle / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
        _sync_directory(bundle)
        bundle.rename(path)
        _sync_directory(path.parent)


def setup_pilot(
    env: Mapping[str, str],
    manifest_path: Path,
    baseline_path: Path,
    output: Path,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    controller: JsonHttp | None = None,
    evidence: EvidenceClient | None = None,
) -> dict[str, object]:
    """Retry identical inputs after uncertain writes; never allocate the next generation."""
    phase = "validation"
    try:
        manifest = PilotManifest.parse(decode(_read(manifest_path, 32_768)), clock())
        content = _read(baseline_path, 1_048_576)
        if hashlib.sha256(content).hexdigest() != manifest.baseline.sha256:
            raise ValueError("baseline digest mismatch")
        ca_file = env.get("SRE_EVIDENCE_CA_FILE") or None
        url = https_url(env["SRE_EVIDENCE_API_URL"], allow_loopback=bool(ca_file))
        controller_token, worker_token = env["SRE_PILOT_SETUP_TOKEN"], env["SRE_EVIDENCE_API_TOKEN"]
        if token_digest(controller_token) == token_digest(worker_token):
            raise ValueError("controller and worker credentials must be separate")
        controller = controller or JsonHttp(
            url, "Bearer " + controller_token, local_ca_file=ca_file
        )
        evidence = evidence or EvidenceClient(url, worker_token, local_ca_file=ca_file)
        fingerprint = digest({"manifest": manifest.fingerprint(), "evidence_api_url": url})
        with _destination(output, fingerprint) as destination:
            phase = "registration"
            registration = manifest.registration()
            response = controller.call("POST", "/v1/previews/register", registration)
            if registration_from_wire(response) != (manifest.preview, manifest.expected_generation):
                raise ValueError("registration response mismatch")
            phase = "baseline_upload"
            stored = evidence.upload(
                "pilot-" + fingerprint,
                manifest.preview.scope,
                content,
                manifest.baseline.observed_at,
                manifest.baseline.expires_at,
            )
            phase = "verification"
            evidence.verify(stored)
            evidence.context(manifest.preview.scope)
            now = clock()
            if (
                not stored.valid_for(manifest.preview.scope, now)
                or now >= manifest.preview.expires_at
            ):
                raise ValueError("setup expired during remote operations")
            phase = "output"
            identity = execution_key(manifest.spec())
            _publish(
                destination,
                {
                    "execution.json": manifest.spec(),
                    "baseline-evidence.json": {"schema_version": 2, "evidence": asdict(stored)},
                    "receipt.json": {
                        "schema_version": 2,
                        "setup_sha256": fingerprint,
                        "workflow_id": identity,
                    },
                },
            )
        return {"status": "prepared", "workflow_id": identity, "execution_started": False}
    except Exception:
        # Neither credential values nor response bodies/file contents reach CLI errors.
        raise PilotSetupError(phase) from None

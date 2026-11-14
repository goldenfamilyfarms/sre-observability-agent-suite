"""Real Git and authenticated evidence with a deterministic synthetic OCI exporter."""

import hashlib
import io
import json
import subprocess
import tarfile
from dataclasses import asdict
from datetime import timedelta

from sre_suite.control.contracts import Binding
from sre_suite.execution.journal import Journal
from sre_suite.execution.wire import canonical
from sre_suite.image_build.contracts import BuildRequest
from sre_suite.image_build.oci import inspect
from sre_suite.image_build.providers import labels
from sre_suite.image_build.runtime import BuildRuntime
from tests.delivery_fixture import DeliveryFixture
from tests.test_foundation import NOW


def archive(path, platform, entrypoint, image_labels):
    blobs = {}

    def blob(value, kind):
        data = value if isinstance(value, bytes) else canonical(value).encode()
        sha = hashlib.sha256(data).hexdigest()
        blobs["blobs/sha256/" + sha] = data
        return dict(
            mediaType="application/vnd.oci.image." + kind, digest="sha256:" + sha, size=len(data)
        )

    config = blob(
        {
            "os": "linux",
            "architecture": platform.split("/")[1],
            "config": {
                "User": "65532:65532",
                "WorkingDir": "/app",
                "Entrypoint": entrypoint,
                "Cmd": [],
                "Labels": image_labels,
            },
        },
        "config.v1+json",
    )
    layer = blob(b"synthetic-layer-not-executed", "layer.v1.tar")
    manifest = blob({"schemaVersion": 2, "config": config, "layers": [layer]}, "manifest.v1+json")
    blobs["index.json"] = canonical({"schemaVersion": 2, "manifests": [manifest]}).encode()
    blobs["oci-layout"] = b'{"imageLayoutVersion":"1.0.0"}'
    with tarfile.open(path, "w") as tar:
        for name, data in blobs.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    path.chmod(0o600)
    return manifest["digest"]


class FakeTools:
    def __init__(self):
        self.builds = self.pushes = 0
        self.remote = set()
        self.lose_ack = False
        self.before_push = lambda: None

    def build(self, request, context, hashes, output, guard):
        guard()
        self.builds += 1
        recipe = request.binding.target["recipe"]
        self.files = {
            str(p.relative_to(context)): p.read_bytes() for p in context.rglob("*") if p.is_file()
        }
        archive(output, recipe["platform"], recipe["entrypoint"], labels(request, hashes))
        return inspect(output, recipe["platform"], recipe["entrypoint"], labels(request, hashes))

    def publish(self, request, archive, image, guard):
        self.before_push()
        guard()
        self.pushes += 1
        self.remote.add(image)
        if self.lose_ack:
            self.lose_ack = False
            raise ValueError("synthetic lost publication acknowledgement")

    def verify(self, request, image, guard):
        guard()
        if image not in self.remote:
            raise ValueError("image unavailable")


class BuildFixture:
    def __init__(self, root, reference=False):
        self.delivery = DeliveryFixture(root)
        f = self.delivery.f
        content = "def answer():\n    return 2\n"
        (f.repo / "app.py").write_text(content)
        edits = [{"path": "app.py", "content": content}]
        if reference:
            from pathlib import Path

            from sre_suite.target_service import reference as service

            source = Path(service.__file__).read_text()
            (f.repo / "reference_service.py").write_text(source)
            edits.append({"path": "reference_service.py", "content": source})
            subprocess.run(
                ["git", "add", "reference_service.py"], cwd=f.repo, check=True, capture_output=True
            )
        subprocess.run(["git", "add", "app.py"], cwd=f.repo, check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "reviewed candidate",
            ],
            cwd=f.repo,
            check=True,
            capture_output=True,
        )
        candidate = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=f.repo, text=True
        ).strip()
        stages = {}
        for role, subject in self.delivery.roles.items():
            value = {
                "role": role,
                "verdict": "passed",
                "manifest": "b" * 64,
                "prior_evidence_sha256": [e.sha256 for e in stages.values()],
            }
            if role == "implement":
                value["edits"] = edits
            stages[role] = self.delivery.client(subject).upload(
                "image-" + role,
                f.ticket.scope,
                canonical(value).encode(),
                NOW,
                NOW + timedelta(minutes=40),
            )
        target = {
            "environment": "development",
            "kind": "image-build",
            "source_repository": "example/demo",
            "candidate_revision": candidate,
            "chart_revision": "c" * 40,
            "chart_digest": self.delivery.raw["binding"]["target"]["chart_digest"],
            "registry_repository": "registry.example.invalid/team/service",
            "recipe": {
                "base_image": "python@sha256:" + "1" * 64,
                "platform": "linux/amd64",
                "paths": ["app.py", "test_app.py"],
                "steps": [],
                "entrypoint": ["python", "/app/app.py"],
            },
        }
        self.raw = {
            "schema_version": 2,
            "binding": {
                "scope": asdict(f.ticket.scope),
                "context_version": 0,
                "artifact_digest": "sha256:" + stages["implement"].sha256,
                "plan_digest": "sha256:" + "b" * 64,
                "action": "execute",
                "target": target,
            },
            "stages": {r: asdict(e) for r, e in stages.items()},
            "expires_at": (NOW + timedelta(minutes=30)).isoformat(),
        }
        self.raw = json.loads(canonical(self.raw))
        if reference:
            recipe = self.raw["binding"]["target"]["recipe"]
            recipe["paths"] = ["reference_service.py"]
            recipe["entrypoint"] = ["python", "/app/reference_service.py"]
        self.tools = FakeTools()
        self.runtime = BuildRuntime(
            BuildRequest.parse(self.raw),
            self.delivery.roles,
            "builder",
            f.repo,
            self.delivery.client("builder"),
            self.delivery.control,
            Journal(root / "images"),
            self.tools,
            clock=lambda: NOW,
        )
        self.approve()

    def approve(self):
        self.delivery.human.call(
            "approve",
            self.delivery.scope,
            {
                "binding": Binding.parse(self.raw["binding"]).wire(),
                "expires_at": self.raw["expires_at"],
            },
        )

    def close(self):
        self.delivery.close()

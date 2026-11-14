"""Explicit BuildKit export and digest-preserving registry publication, with no shell."""

import json
import os
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from ..execution.process import minimal_environment, run_process
from ..execution.wire import canonical, digest
from ..telemetry.schedule_config import private_path
from .contracts import BuildRequest
from .oci import file_sha, inspect


def labels(request: BuildRequest, context: dict[str, str]) -> dict[str, str]:
    return {
        "org.opencontainers.image.revision": request.binding.target["candidate_revision"],
        "sre.source.revision": request.binding.scope.revision,
        "sre.plan.digest": request.binding.plan_digest,
        "sre.source.artifact": request.binding.artifact_digest,
        "sre.build.identity": request.identity,
        "sre.build.context": digest(context),
    }


def dockerfile(request: BuildRequest, context: dict[str, str]) -> str:
    recipe = request.binding.target["recipe"]
    lines = [
        "FROM " + recipe["base_image"],
        "WORKDIR /app",
        "COPY --chown=65532:65532 . /app/",
        "USER 65532:65532",
        "HEALTHCHECK NONE",
    ]
    lines.extend("RUN " + json.dumps(step) for step in recipe["steps"])
    lines.extend("LABEL " + k + "=" + json.dumps(v) for k, v in labels(request, context).items())
    return "\n".join([*lines, "ENTRYPOINT " + json.dumps(recipe["entrypoint"]), "CMD []", ""])


class Tools:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def binary(self, name: str) -> str:
        item = self.config[name]
        path = Path(item["path"])
        if (
            not path.is_absolute()
            or not os.access(path, os.X_OK)
            or file_sha(path) != item["sha256"]
        ):
            raise ValueError("build tool differs from reviewed binary")
        return str(path)

    def registry_cert(self, destination: bool) -> list[str]:
        config = self.config.get("registry_ca")
        if config is None:
            return []
        from ..api_wire import fields

        fields(config, {"path", "sha256"})
        path = Path(config["path"])
        private_path(path, directory=False)
        if (
            path.name != "ca.crt"
            or {p.name for p in path.parent.iterdir()} != {"ca.crt"}
            or file_sha(path) != config["sha256"]
        ):
            raise ValueError("registry trust directory must contain only the pinned CA")
        return ["--dest-cert-dir" if destination else "--cert-dir", str(path.parent)]

    def registry_environment(self, request: BuildRequest) -> dict[str, str]:
        auth = Path(self.config["registry_auth_file"])
        private_path(auth, directory=False)
        if auth.stat().st_size > 16384:
            raise ValueError("registry auth file exceeds budget")
        credentials = json.loads(auth.read_text())
        host = request.binding.target["registry_repository"].split("/", 1)[0]
        if (
            not isinstance(credentials, dict)
            or set(credentials) != {"auths"}
            or not isinstance(credentials["auths"], dict)
            or set(credentials["auths"]) - {host}
        ):
            raise ValueError("authfile must be scoped to the exact registry host")
        for credential in credentials["auths"].values():
            import base64

            if not isinstance(credential, dict) or set(credential) != {"auth"}:
                raise ValueError(
                    "explicit registry auth required; credential helpers are not allowed"
                )
            if b":" not in base64.b64decode(credential["auth"], validate=True):
                raise ValueError("invalid registry auth")
        # An empty private config directory prevents fallback to unrelated host credentials.
        isolated = auth.parent / "sre-registry-empty-config"
        isolated.mkdir(mode=0o700, exist_ok=True)
        private_path(isolated, directory=True)
        if any(isolated.iterdir()):
            raise ValueError("registry fallback directory must remain empty")
        return minimal_environment() | {
            "DOCKER_CONFIG": str(isolated),
            "XDG_CONFIG_HOME": str(isolated),
            "XDG_RUNTIME_DIR": str(isolated),
            "REGISTRY_AUTH_FILE": str(auth),
        }

    def build(
        self,
        request: BuildRequest,
        context: Path,
        hashes: dict[str, str],
        archive: Path,
        guard: Callable[[], None],
    ) -> dict[str, Any]:
        private_path(Path(self.config["docker_config"]), directory=True)
        # Dockerfile is outside the context. Build code receives neither auth files nor host mounts.
        with TemporaryDirectory(prefix="recipe-", dir=archive.parent) as directory:
            recipe_file = Path(directory) / "Dockerfile"
            recipe_file.write_text(dockerfile(request, hashes))
            env = minimal_environment() | {
                "DOCKER_CONFIG": self.config["docker_config"],
                "DOCKER_CONTEXT": self.config["docker_context"],
            }
            guard()
            result = run_process(
                [
                    self.binary("buildx"),
                    "build",
                    "--builder",
                    self.config["builder"],
                    "--network=none",
                    "--platform",
                    request.binding.target["recipe"]["platform"],
                    "--provenance=false",
                    "--sbom=false",
                    "--no-cache",
                    "--tag",
                    "sre-build:" + request.identity,
                    "--file",
                    str(recipe_file),
                    "--output",
                    "type=oci,dest=" + str(archive),
                    str(context),
                ],
                Path(directory),
                env,
                timeout=480,
                max_output=500000,
                heartbeat=guard,
            )
            if result.code:
                raise ValueError("image build failed; inspect isolated builder privately")
        archive.chmod(0o600)
        guard()
        recipe = request.binding.target["recipe"]
        return inspect(archive, recipe["platform"], recipe["entrypoint"], labels(request, hashes))

    def publish(
        self, request: BuildRequest, archive: Path, image_digest: str, guard: Callable[[], None]
    ) -> None:
        auth = Path(self.config["registry_auth_file"])
        private_path(auth, directory=False)
        # Only a private authfile path is passed, never credentials in argv or build context.
        with TemporaryDirectory(prefix="push-", dir=archive.parent) as directory:
            root = Path(directory)
            policy = root / "policy.json"
            # This operation only imports the locally verified, exact-digest archive.
            policy.write_text(canonical({"default": [{"type": "insecureAcceptAnything"}]}))
            target = request.binding.target["registry_repository"]
            env = self.registry_environment(request)
            guard()
            result = run_process(
                [
                    self.binary("skopeo"),
                    "--policy",
                    str(policy),
                    "copy",
                    "--preserve-digests",
                    "--dest-tls-verify=true",
                    *self.registry_cert(True),
                    "--dest-authfile",
                    str(auth),
                    "--digestfile",
                    str(root / "digest"),
                    "oci-archive:" + str(archive),
                    "docker://" + target + ":sre-" + image_digest.replace(":", "-"),
                ],
                root,
                env,
                timeout=480,
                max_output=500000,
                heartbeat=guard,
            )
            if result.code:
                raise ValueError("registry publication uncertain; retry the same immutable archive")
            if (root / "digest").read_text().strip() != image_digest:
                raise ValueError("registry changed the manifest digest")
            self.verify(request, image_digest, guard)

    def verify(self, request: BuildRequest, image_digest: str, guard: Callable[[], None]) -> None:
        import hashlib

        auth = Path(self.config["registry_auth_file"])
        private_path(auth, directory=False)
        guard()
        result = run_process(
            [
                self.binary("skopeo"),
                "inspect",
                "--raw",
                "--tls-verify=true",
                *self.registry_cert(False),
                "--authfile",
                str(auth),
                "docker://" + request.binding.target["registry_repository"] + "@" + image_digest,
            ],
            auth.parent,
            self.registry_environment(request),
            timeout=30,
            max_output=2_000_000,
            heartbeat=guard,
        )
        if result.code or "sha256:" + hashlib.sha256(result.output).hexdigest() != image_digest:
            raise ValueError("published immutable manifest could not be verified")
        guard()

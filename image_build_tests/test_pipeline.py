"""Disposable real BuildKit/OCI/registry test. No deployment or external registry writes."""

import json
import os
import platform
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4

from sre_suite.image_build.contracts import BuildRequest
from sre_suite.image_build.oci import file_sha
from sre_suite.image_build.providers import Tools
from tests.image_build_fixture import BuildFixture
from tests.test_target_service import identity

BASE = "python@sha256:65bd3d9f1d7fa268dd1909973ba7eb4bdf41f6dcf2cfbe76ba9528b170f3fd79"
BUILDKIT = "moby/buildkit@sha256:c5f8862f52499773af85bc07695b2379ef08e8002352b0df2c4864c91d78dabe"
REGISTRY = "registry@sha256:82f6ff4a7f982960c8aa51617fea3aabc485df6e55cce80d22deb1de36f1bc3c"


class ImagePipelineTests(unittest.TestCase):
    def test_real_oci_build_registry_attestation_and_reference_container(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = temporary.name
        root = Path(directory).resolve()
        f = BuildFixture(root, reference=True)
        self.addCleanup(f.close)
        docker = shutil.which("docker")
        self.assertIsNotNone(docker)
        config = root / "docker-config"
        config.mkdir(mode=0o700)

        def command(*args):
            result = subprocess.run(
                [docker, "--config", str(config), *args],
                capture_output=True,
                timeout=180,
            )
            if result.returncode:
                raise AssertionError(result.stderr.decode())
            return result.stdout

        host = os.environ.get("SRE_TEST_DOCKER_HOST", "unix:///var/run/docker.sock")
        command("context", "create", "test", "--docker", "host=" + host)

        def docker_call(*args):
            return command("--context", "test", *args)

        plugins = json.loads(
            subprocess.check_output(
                [docker, "info", "--format", "{{json .ClientInfo.Plugins}}"], text=True
            )
        )
        buildx = next(p["Path"] for p in plugins if p["Name"] == "buildx")

        def buildx_call(*args):
            result = subprocess.run(
                [buildx, *args],
                env=dict(os.environ, DOCKER_CONFIG=str(config), DOCKER_CONTEXT="test"),
                check=True,
                capture_output=True,
                timeout=180,
            )
            return result.stdout

        name = "sre-build-test-" + uuid4().hex[:12]
        buildx_call(
            "create",
            "--name",
            name,
            "--driver",
            "docker-container",
            "--driver-opt",
            "image=" + BUILDKIT,
        )
        self.addCleanup(lambda: buildx_call("rm", "--force", name))
        recipe = f.raw["binding"]["target"]["recipe"]
        recipe["base_image"] = BASE
        recipe["platform"] = "linux/" + (
            "arm64" if platform.machine() in {"arm64", "aarch64"} else "amd64"
        )
        auth = root / "auth.json"
        auth.write_text('{"auths":{}}')
        auth.chmod(0o600)
        skopeo = shutil.which("skopeo")
        # Build-only mode is explicit for local development; CI requires full registry proof.
        build_only = os.environ.get("SRE_TEST_BUILD_ONLY") == "true"
        if not build_only:
            self.assertIsNotNone(skopeo, "Install skopeo for registry integration tests")
        cert_dir = root / "registry-ca"
        cert_dir.mkdir(mode=0o700)
        cert, key = cert_dir / "ca.crt", root / "key.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost,IP:127.0.0.1",
                "-keyout",
                str(key),
                "-out",
                str(cert),
            ],
            check=True,
            capture_output=True,
        )
        cert.chmod(0o600)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        registry = "sre-registry-" + uuid4().hex[:12]
        if not build_only:
            docker_call(
                "run",
                "--detach",
                "--rm",
                "--name",
                registry,
                "--publish",
                f"127.0.0.1:{port}:5000",
                "--mount",
                f"type=bind,source={cert},target=/cert.pem,readonly",
                "--mount",
                f"type=bind,source={key},target=/key.pem,readonly",
                "--env",
                "REGISTRY_HTTP_TLS_CERTIFICATE=/cert.pem",
                "--env",
                "REGISTRY_HTTP_TLS_KEY=/key.pem",
                REGISTRY,
            )
            self.addCleanup(lambda: docker_call("rm", "--force", registry))
            f.raw["binding"]["target"]["registry_repository"] = f"localhost:{port}/reference"
        tools = {
            "buildx": {"path": buildx, "sha256": file_sha(Path(buildx))},
            "skopeo": {"path": skopeo or docker, "sha256": file_sha(Path(skopeo or docker))},
            "docker_config": str(config),
            "docker_context": "test",
            "builder": name,
            "registry_auth_file": str(auth),
            "registry_ca": {"path": str(cert), "sha256": file_sha(cert)},
        }
        f.runtime.request = BuildRequest.parse(f.raw)
        f.runtime.tools = Tools(tools)
        f.approve()
        built = f.runtime.build()
        self.assertEqual(built["outcome"], "built")
        if not build_only:
            receipt = f.runtime.publish()
            self.assertTrue(receipt["image"].endswith(built["image_digest"]))
            self.assertEqual(receipt, f.runtime.publish())
        # Run the exact built image locally, never a configured remote target.
        docker_call("load", "--input", str(f.runtime.archive))
        config_id = "sre-build:" + f.runtime.request.identity
        self.addCleanup(lambda: docker_call("image", "rm", "--force", config_id))
        container = "sre-reference-" + uuid4().hex[:12]
        env = identity() | {"sre.artifact.digest": built["image_digest"]}
        docker_call(
            "run",
            "--detach",
            "--rm",
            "--name",
            container,
            "--publish",
            "127.0.0.1::8080",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--tmpfs",
            "/tmp:rw,nosuid,size=32m",
            "--env",
            "OTEL_RESOURCE_ATTRIBUTES=" + ",".join(k + "=" + v for k, v in env.items()),
            "--env",
            "SRE_TARGET_TOKEN=" + "s" * 40,
            "--env",
            "SRE_TARGET_METRICS_TOKEN=" + "m" * 40,
            config_id,
        )
        self.addCleanup(lambda: docker_call("rm", "--force", container))
        address = docker_call("port", container, "8080").decode().strip()
        deadline = time.monotonic() + 30
        while True:
            try:
                with urlopen("http://" + address + "/ready", timeout=1) as reply:
                    self.assertEqual(json.load(reply), {"ready": True})
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)
        with urlopen(
            Request(
                "http://" + address + "/sre/identity",
                headers={"Authorization": "Bearer " + "s" * 40},
            ),
            timeout=5,
        ) as reply:
            self.assertEqual(json.load(reply), {"resource": env})

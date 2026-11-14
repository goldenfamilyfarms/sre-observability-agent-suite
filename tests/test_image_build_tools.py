import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sre_suite.execution.process import ProcessResult
from sre_suite.image_build.oci import file_sha
from sre_suite.image_build.providers import Tools, labels
from tests.image_build_fixture import BuildFixture, archive


class ImageToolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.f = BuildFixture(self.root)
        self.addCleanup(self.f.close)
        self.config_dir = self.root / "docker-config"
        self.config_dir.mkdir(mode=0o700)
        auth = self.root / "auth.json"
        auth.write_text('{"auths":{}}')
        auth.chmod(0o600)
        binary = {"path": sys.executable, "sha256": file_sha(Path(sys.executable))}
        self.tools = Tools(
            {
                "buildx": binary,
                "skopeo": binary,
                "docker_config": str(self.config_dir),
                "docker_context": "isolated",
                "builder": "isolated",
                "registry_auth_file": str(auth),
                "registry_ca": None,
            }
        )

    def test_build_process_receives_no_registry_or_agent_credentials_and_no_network_entitlement(
        self,
    ):
        context = self.root / "context"
        context.mkdir()
        output = self.root / "image.tar"
        request = self.f.runtime.request
        hashes = {"app.py": "a" * 64}
        calls = []

        def process(argv, cwd, env, **kwargs):
            calls.append((argv, env))
            self.assertNotIn("registry-private", json.dumps(env))
            self.assertNotIn("agent-private", json.dumps(env))
            self.assertIn("--network=none", argv)
            self.assertNotIn("--allow", argv)
            self.assertNotIn("--secret", argv)
            recipe = request.binding.target["recipe"]
            archive(output, recipe["platform"], recipe["entrypoint"], labels(request, hashes))
            return ProcessResult(0, b"")

        with (
            patch.dict(
                "os.environ",
                {"SRE_CONTROL_TOKEN": "agent-private", "SRE_REGISTRY_TOKEN": "registry-private"},
            ),
            patch("sre_suite.image_build.providers.run_process", side_effect=process),
        ):
            self.tools.build(request, context, hashes, output, lambda: None)
        self.assertEqual(len(calls), 1)

    def test_registry_copy_preserves_digest_and_verifies_actual_remote_manifest(self):
        manifest = b'{"schemaVersion":2}'
        digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
        calls = []

        def process(argv, cwd, env, **kwargs):
            calls.append(argv)
            if "copy" in argv:
                self.assertIn("--preserve-digests", argv)
                self.assertIn("--dest-tls-verify=true", argv)
                Path(argv[argv.index("--digestfile") + 1]).write_text(digest)
                return ProcessResult(0, b"")
            self.assertTrue(argv[-1].endswith("@" + digest))
            return ProcessResult(0, manifest)

        with patch("sre_suite.image_build.providers.run_process", side_effect=process):
            self.tools.publish(
                self.f.runtime.request, self.root / "image.tar", digest, lambda: None
            )
        self.assertEqual(len(calls), 2)
        with patch(
            "sre_suite.image_build.providers.run_process", return_value=ProcessResult(0, b"altered")
        ):
            with self.assertRaises(ValueError):
                self.tools.verify(self.f.runtime.request, digest, lambda: None)

    def test_altered_tool_or_broad_authfile_is_rejected(self):
        self.tools.config["buildx"] = {"path": sys.executable, "sha256": "0" * 64}
        with self.assertRaises(ValueError):
            self.tools.binary("buildx")
        path = Path(self.tools.config["registry_auth_file"])
        for value in (
            {"auths": {}, "credsStore": "ambient"},
            {"auths": {"other.example.invalid": {"auth": "dTpw"}}},
        ):
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                self.tools.registry_environment(self.f.runtime.request)

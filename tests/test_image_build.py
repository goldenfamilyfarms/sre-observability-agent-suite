import copy
import json
import tarfile
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from sre_suite.api_wire import evidence_from_wire
from sre_suite.delivery.contracts import DeliveryRequest
from sre_suite.execution.http import RemoteError
from sre_suite.image_build.contracts import BuildRequest
from sre_suite.image_build.providers import dockerfile
from tests.image_build_fixture import BuildFixture
from tests.test_foundation import NOW


class ImageBuildTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.f = BuildFixture(Path(temp.name).resolve())
        self.addCleanup(self.f.close)
        self.runtime = self.f.runtime

    def test_build_publish_retry_and_delivery_consume_real_authenticated_attestation(self):
        result = self.runtime.build()
        self.assertEqual(result, self.runtime.build())
        self.assertEqual(self.f.tools.builds, 1)
        self.assertEqual(set(self.f.tools.files), {"app.py", "test_app.py"})
        receipt = self.runtime.publish()
        self.assertEqual(receipt, self.runtime.publish())
        self.assertEqual(self.f.tools.pushes, 1)
        proof = json.loads(self.runtime.evidence.read(evidence_from_wire(receipt["build"])))
        self.assertEqual(
            proof["candidate_revision"], self.f.raw["binding"]["target"]["candidate_revision"]
        )
        delivery = self.f.delivery
        raw = copy.deepcopy(delivery.raw)
        raw["build"], raw["stages"] = receipt["build"], receipt["stages"]
        raw["binding"]["artifact_digest"] = result["image_digest"]
        raw["binding"]["target"]["image"] = receipt["image"]
        raw["binding"]["target"]["candidate_revision"] = proof["candidate_revision"]
        delivery.runtime.request = DeliveryRequest.parse(raw)
        delivery.approve()
        delivery.runtime.provenance()

    def test_publish_requires_completed_build_and_checks_saved_bytes(self):
        with self.assertRaises(FileNotFoundError):
            self.runtime.publish()
        self.runtime.build()
        self.runtime.archive.write_bytes(b"tampered")
        with self.assertRaises(tarfile.ReadError):
            self.runtime.publish()
        self.assertEqual(self.f.tools.pushes, 0)

    def test_lost_registry_ack_retries_only_same_immutable_image(self):
        self.runtime.build()
        self.f.tools.lose_ack = True
        with self.assertRaises(ValueError):
            self.runtime.publish()
        receipt = self.runtime.publish()
        self.assertEqual(self.f.tools.pushes, 2)
        self.assertEqual(self.f.tools.remote, {receipt["image"].split("@")[1]})

    def test_changed_generation_and_revocation_block_even_cached_build(self):
        self.runtime.build()
        self.f.delivery.f.registry.start(
            replace(self.f.delivery.f.preview, scope=replace(self.f.delivery.scope, generation=2)),
            1,
            NOW,
        )
        with self.assertRaises(RemoteError):
            self.runtime.publish()
        self.assertEqual(self.f.tools.pushes, 0)

    def test_unreviewed_extra_file_in_commit_blocks_before_build(self):
        import subprocess

        root = self.f.delivery.f.repo
        (root / "extra.txt").write_text("unreviewed")
        subprocess.run(["git", "add", "extra.txt"], cwd=root, check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "--amend",
                "--no-edit",
            ],
            cwd=root,
            check=True,
            capture_output=True,
        )
        self.f.raw["binding"]["target"]["candidate_revision"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()
        self.runtime.request = BuildRequest.parse(self.f.raw)
        self.f.approve()
        with self.assertRaisesRegex(ValueError, "candidate tree differs"):
            self.runtime.build()
        self.assertEqual(self.f.tools.builds, 0)

    def test_wrong_stage_identity_plan_chain_and_expiry_are_denied(self):
        for mutation in ("producer", "plan", "chain"):
            with self.subTest(mutation=mutation):
                if mutation == "producer":
                    with patch.dict(self.runtime.roles, {"review": "builder"}):
                        with self.assertRaises(ValueError):
                            self.runtime.build()
                else:
                    original = self.runtime.evidence.read

                    def altered(ref, original=original, mutation=mutation):
                        raw = json.loads(original(ref))
                        raw["manifest" if mutation == "plan" else "prior_evidence_sha256"] = (
                            "changed"
                        )
                        return json.dumps(raw).encode()

                    with patch.object(self.runtime.evidence, "read", side_effect=altered):
                        with self.assertRaises(ValueError):
                            self.runtime.build()
        self.assertEqual(self.f.tools.builds, 0)

    def test_request_rejects_unpinned_images_hidden_context_and_production(self):
        for key, value in [
            ("base_image", "python:latest"),
            ("paths", [".env"]),
            ("paths", ["Dockerfile"]),
            ("platform", "all"),
            ("entrypoint", ["x\nRUN bad"]),
        ]:
            raw = copy.deepcopy(self.f.raw)
            raw["binding"]["target"]["recipe"][key] = value
            with self.assertRaises(ValueError):
                BuildRequest.parse(raw)
        raw = copy.deepcopy(self.f.raw)
        raw["binding"]["scope"]["environment"] = "production"
        raw["binding"]["target"]["environment"] = "production"
        with self.assertRaises(ValueError):
            BuildRequest.parse(raw)

    def test_recipe_is_generated_and_does_not_accept_source_dockerfile(self):
        source = dockerfile(self.runtime.request, {"app.py": "a" * 64})
        self.assertIn("USER 65532:65532", source)
        self.assertIn("HEALTHCHECK NONE", source)
        self.assertNotIn("SECRET", source)
        self.assertIn("CMD []", source)

    def test_revoked_approval_during_push_blocks_publication(self):
        self.runtime.build()
        self.f.tools.before_push = lambda: self.f.delivery.human.call(
            "revoke", self.f.delivery.scope, {"binding": self.runtime.request.binding.wire()}
        )
        with self.assertRaises(ValueError):
            self.runtime.publish()
        self.assertEqual(self.f.tools.pushes, 0)

    def test_missing_remote_image_does_not_reuse_cached_attestation(self):
        self.runtime.build()
        self.runtime.publish()
        self.f.tools.remote.clear()
        with self.assertRaises(ValueError):
            self.runtime.publish()

    def test_oci_symlinks_and_mismatched_runtime_labels_are_rejected(self):
        from sre_suite.image_build.oci import inspect
        from sre_suite.image_build.providers import labels

        result = self.runtime.build()
        recipe = self.runtime.request.binding.target["recipe"]
        with self.assertRaises(ValueError):
            inspect(
                self.runtime.archive,
                recipe["platform"],
                ["unreviewed"],
                labels(self.runtime.request, result["context"]),
            )
        with tarfile.open(self.runtime.archive, "a") as archive:
            link = tarfile.TarInfo("outside")
            link.type = tarfile.SYMTYPE
            link.linkname = "/outside"
            archive.addfile(link)
        with self.assertRaises(ValueError):
            self.runtime.publish()

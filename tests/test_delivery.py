"""Guarded provider writes, real owned resource state and fail-closed readiness."""

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from sre_suite.delivery.contracts import DeliveryRequest
from sre_suite.delivery.helm import HelmRenderer, resources
from sre_suite.delivery.providers import KubernetesArgo, recipe_matches
from sre_suite.execution.http import RemoteError
from sre_suite.execution.wire import digest
from tests.delivery_fixture import DeliveryFixture
from tests.test_foundation import NOW


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.d = DeliveryFixture(self.root)
        self.addCleanup(self.d.close)

    def test_authorization_required_before_any_provider_access(self):
        with self.assertRaises(ValueError):
            self.d.runtime.deploy()
        self.assertEqual(self.d.cluster.calls, [])
        self.d.approve()
        self.assertFalse(self.d.runtime.prepare()["writes"])
        self.assertEqual(self.d.cluster.calls, [])

    def test_deploy_readiness_uses_actual_digest_and_is_not_final_certification(self):
        self.d.approve()
        self.assertEqual(self.d.runtime.deploy()["outcome"], "sync_requested")
        self.assertFalse(self.d.runtime.readiness()["ready"])
        self.d.cluster.healthy(self.d.request)
        receipt = self.d.runtime.readiness()
        self.assertTrue(receipt["ready"])
        self.assertFalse(receipt["accepted"])
        doc = json.loads(
            self.d.evidence.read(
                __import__(
                    "sre_suite.api_wire", fromlist=["evidence_from_wire"]
                ).evidence_from_wire(receipt["evidence"])
            )
        )
        self.assertEqual(doc["binding"], self.d.request.binding.wire())
        # A saved receipt cannot hide a live image substitution.
        pods = next(v for k, v in self.d.cluster.objects.items() if "/pods?" in k)
        pods["items"][0]["status"]["containerStatuses"][0]["imageID"] = "image@sha256:" + "d" * 64
        self.assertFalse(self.d.runtime.readiness()["ready"])

    def test_retries_reconcile_lost_create_acknowledgement(self):
        self.d.approve()
        self.d.cluster.drop_after = True
        with self.assertRaises(RemoteError):
            self.d.runtime.deploy()
        self.d.runtime.deploy()
        self.assertEqual(
            sum(m == "POST" and p == "/api/v1/namespaces" for m, p, _ in self.d.cluster.calls), 1
        )

    def test_generation_change_mid_write_blocks_followup_effects(self):
        self.d.approve()

        def changed(method, path, value):
            if method == "POST":
                self.d.f.registry.start(
                    replace(self.d.f.preview, scope=replace(self.d.scope, generation=2)), 1, NOW
                )

        self.d.cluster.before = changed
        with self.assertRaises(RemoteError):
            self.d.runtime.deploy()
        self.assertEqual(sum(m == "POST" for m, _, _ in self.d.cluster.calls), 1)

    def test_foreign_namespace_and_changed_application_are_not_adopted(self):
        self.d.approve()
        self.d.runtime.deploy()
        app = self.d.cluster.objects[KubernetesArgo.application_path(self.d.request.target)]
        app["spec"]["project"] = "foreign"
        with self.assertRaises(ValueError):
            self.d.runtime.deploy()
        app["metadata"]["annotations"]["sre-suite/owner"] = "foreign"
        with self.assertRaises(ValueError):
            self.d.runtime.readiness()

    def test_cleanup_requires_separate_authority_and_uses_uid_preconditions(self):
        self.d.approve()
        self.d.runtime.deploy()
        self.d.cluster.healthy(self.d.request)
        raw = copy.deepcopy(self.d.raw)
        raw["binding"]["action"] = "cleanup"
        raw["build"], raw["stages"] = None, {}
        self.d.runtime.request = DeliveryRequest.parse(raw)
        with self.assertRaises(ValueError):
            self.d.runtime.cleanup()
        self.d.approve()
        self.assertFalse(self.d.runtime.cleanup()["complete"])
        self.assertFalse(self.d.runtime.cleanup()["complete"])
        self.assertTrue(self.d.runtime.cleanup()["complete"])
        deletes = [v for m, _, v in self.d.cluster.calls if m == "DELETE"]
        self.assertTrue(all(set(v["preconditions"]) == {"uid", "resourceVersion"} for v in deletes))

    def test_stage_identity_or_build_digest_substitution_blocks(self):
        self.d.approve()
        self.d.runtime.roles["review"] = "implement-actor"
        with self.assertRaises(ValueError):
            self.d.runtime.deploy()
        self.assertEqual(self.d.cluster.calls, [])
        raw = copy.deepcopy(self.d.raw)
        raw["binding"]["artifact_digest"] = "sha256:" + "f" * 64
        with self.assertRaises(ValueError):
            DeliveryRequest.parse(raw)

    def test_production_requires_certification_and_namespaces_are_generation_specific(self):
        for mutate in (
            lambda r: r["binding"]["target"].update(namespace="default"),
            lambda r: r["binding"]["target"].update(phase="release"),
            lambda r: r["binding"]["target"].update(namespace="preview-other-g1"),
        ):
            raw = copy.deepcopy(self.d.raw)
            mutate(raw)
            with self.assertRaises(ValueError):
                DeliveryRequest.parse(raw)

    def test_template_defaults_allowed_but_commands_secrets_and_sidecars_rejected(self):
        spec = resources(self.d.request)[0]["spec"]["template"]["spec"]
        actual = copy.deepcopy(spec)
        actual["containers"][0]["terminationMessagePolicy"] = "File"
        self.assertTrue(recipe_matches(actual, spec))
        actual["containers"][0]["command"] = ["unexpected"]
        self.assertFalse(recipe_matches(actual, spec))
        actual = copy.deepcopy(spec)
        actual["volumes"].append({"name": "secret", "secret": {"secretName": "unexpected"}})
        self.assertFalse(recipe_matches(actual, spec))

    def test_helm_subprocess_pins_resources_and_rejects_changed_output(self):
        from unittest.mock import patch

        from sre_suite.execution.process import ProcessResult
        from sre_suite.execution.wire import canonical

        rows = resources(self.d.request)
        with patch(
            "sre_suite.delivery.helm.run_process",
            return_value=ProcessResult(
                0, ("---\n" + "\n---\n".join(canonical(r) for r in rows)).encode()
            ),
        ) as process:
            result = HelmRenderer("/configured/helm").render(self.d.request, lambda: None)
        self.assertEqual(result["resources_sha256"], digest(rows))
        self.assertNotIn("SRE_KUBERNETES_TOKEN", process.call_args.args[2])
        with patch(
            "sre_suite.delivery.helm.run_process", return_value=ProcessResult(0, b"---\n{}")
        ):
            with self.assertRaises(ValueError):
                HelmRenderer("/configured/helm").render(self.d.request, lambda: None)

    def test_rollback_requires_exact_previous_uid_and_spec_before_resource_replacement(self):
        self.d.approve()
        self.d.runtime.deploy()
        target = self.d.request.target
        app_path = KubernetesArgo.application_path(target)
        app = self.d.cluster.objects[app_path]
        app.pop("operation", None)
        previous = {
            "owner": KubernetesArgo.ownership(target),
            "namespace_uid": self.d.cluster.objects[
                "/api/v1/namespaces/" + target.raw["namespace"]
            ]["metadata"]["uid"],
            "application_uid": app["metadata"]["uid"],
            "spec_sha256": digest(app["spec"]),
        }
        raw = copy.deepcopy(self.d.raw)
        raw["binding"]["action"] = "rollback"
        raw["binding"]["target"]["replicas"] = 2
        raw["binding"]["target"]["expected_previous"] = previous
        request = DeliveryRequest.parse(raw)
        self.d.runtime.request = request
        with self.assertRaises(ValueError):
            self.d.runtime.deploy()
        self.d.approve(request)
        self.d.runtime.deploy()
        self.assertEqual(
            self.d.cluster.objects[app_path]["spec"]["source"]["helm"]["valuesObject"]["resources"][
                0
            ]["spec"]["replicas"],
            2,
        )
        changed = copy.deepcopy(raw)
        changed["binding"]["target"]["replicas"] = 3
        changed["binding"]["target"]["expected_previous"]["application_uid"] = "foreign-uid"
        self.d.runtime.request = DeliveryRequest.parse(changed)
        self.d.approve()
        self.d.cluster.objects[app_path].pop("operation", None)
        with self.assertRaises(ValueError):
            self.d.runtime.deploy()

    def test_image_certification_cannot_reuse_a_source_candidate_certificate(self):
        raw = copy.deepcopy(self.d.raw)
        raw["binding"]["target"]["phase"] = "release"
        raw["verification"] = {"input": {"artifact_kind": "source-candidate-v1"}, "receipt": {}}
        self.d.runtime.request = DeliveryRequest.parse(raw)
        self.d.approve()
        with self.assertRaisesRegex(ValueError, "image certification"):
            self.d.runtime.deploy()
        self.assertEqual(self.d.cluster.calls, [])

    def test_mutated_security_context_and_wrong_stage_chain_are_rejected(self):
        template = resources(self.d.request)[0]["spec"]["template"]["spec"]
        altered = copy.deepcopy(template)
        altered["containers"][0]["securityContext"]["privileged"] = True
        self.assertFalse(recipe_matches(altered, template))
        self.d.approve()
        reader = self.d.evidence.read

        def read(ref):
            document = json.loads(reader(ref))
            if document.get("role") == "qa":
                document["prior_evidence_sha256"] = ["f" * 64]
            return json.dumps(document).encode()

        self.d.evidence.read = read
        with self.assertRaisesRegex(ValueError, "artifact chain"):
            self.d.runtime.deploy()

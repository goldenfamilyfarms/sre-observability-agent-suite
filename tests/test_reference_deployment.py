import copy
import tempfile
import unittest
from pathlib import Path

from sre_suite.delivery.contracts import DeliveryRequest
from sre_suite.delivery.helm import resources
from sre_suite.target_service.deployment import environment, package
from tests.delivery_fixture import DeliveryFixture


def reference_request(raw):
    raw = copy.deepcopy(raw)
    target = raw["binding"]["target"]
    profile = dict(
        storage_class="private-csi",
        storage_gib=1,
        ingress_class="private-ingress",
        hostname="reference.example.invalid",
        tls_secret="reference-tls",
        credentials_secret="reference-credentials",
        export_ca_secret="export-ca",
        monitor_labels={"release": "private-prometheus"},
    )
    target.update(reference_service=profile, health_path="/ready", port=8080, replicas=1)
    target["runtime_environment"] = environment(profile) + [
        {"name": "SRE_TARGET_TRACES_URL", "value": "https://tempo.example.invalid/v1/traces"},
        {"name": "SRE_TARGET_LOGS_URL", "value": "https://loki.example.invalid/loki/api/v1/push"},
    ]
    return raw


class ReferenceDeploymentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.f = DeliveryFixture(Path(temp.name).resolve())
        self.addCleanup(self.f.close)
        self.raw = reference_request(self.f.raw)

    def test_package_binds_storage_tls_scrape_and_preserves_approval(self):
        result = package(self.raw)
        self.assertFalse(result["execution_started"])
        self.assertEqual(result["binding"], self.raw["binding"])
        objects = result["resources"]
        self.assertEqual(
            [o["kind"] for o in objects],
            ["Deployment", "Service", "PersistentVolumeClaim", "Ingress", "ServiceMonitor"],
        )
        pod = objects[0]["spec"]["template"]["spec"]
        self.assertEqual(pod["securityContext"]["fsGroup"], 65532)
        self.assertEqual(objects[2]["spec"]["accessModes"], ["ReadWriteOncePod"])
        self.assertEqual(objects[3]["spec"]["tls"][0]["secretName"], "reference-tls")
        self.assertEqual(
            objects[4]["spec"]["endpoints"][0]["authorization"]["credentials"]["key"],
            "metrics-token",
        )
        with self.assertRaises(ValueError):
            self.f.runtime.request = DeliveryRequest.parse(self.raw)
            self.f.runtime.prepare()  # profile needs its own exact approval

    def test_state_claim_changes_with_each_release_binding(self):
        original = package(self.raw)["resources"][2]["metadata"]["name"]
        for field in ("artifact_digest", "plan_digest"):
            changed = copy.deepcopy(self.raw)
            changed["binding"][field] = "sha256:" + "e" * 64
            if field == "artifact_digest":
                changed["binding"]["target"]["image"] = "example/demo@sha256:" + "e" * 64
            self.assertNotEqual(package(changed)["resources"][2]["metadata"]["name"], original)
        changed = copy.deepcopy(self.raw)
        changed["binding"]["target"]["scope"]["generation"] += 1
        changed["binding"]["target"]["namespace"] = changed["binding"]["target"][
            "namespace"
        ].replace("-g1", "-g2")
        self.assertNotEqual(package(changed)["resources"][2]["metadata"]["name"], original)

    def test_rejects_unsafe_reference_settings_and_plaintext_secrets(self):
        for mutate in (
            lambda t: t.update(replicas=2),
            lambda t: t.update(health_path="/health"),
            lambda t: t["reference_service"].update(hostname="*.example.invalid"),
            lambda t: t["reference_service"].update(storage_gib=0),
            lambda t: t["runtime_environment"].__setitem__(
                2, {"name": "SRE_TARGET_TOKEN", "value": "plaintext"}
            ),
            lambda t: t["runtime_environment"].__setitem__(
                -1, {"name": "SRE_TARGET_LOGS_URL", "value": "http://loki.invalid/loki/api/v1/push"}
            ),
        ):
            value = copy.deepcopy(self.raw)
            mutate(value["binding"]["target"])
            with self.assertRaises(ValueError):
                package(value)

    def test_readiness_requires_bound_owned_resources_and_exact_monitor(self):
        f = self.f
        f.runtime.request = request = DeliveryRequest.parse(self.raw)
        f.approve(request)
        f.runtime.deploy()
        f.cluster.healthy(request)
        self.assertFalse(f.runtime.readiness()["ready"])
        rendered = resources(request)
        routes = {
            "Service": "/api/v1/namespaces/{}/services/",
            "PersistentVolumeClaim": "/api/v1/namespaces/{}/persistentvolumeclaims/",
            "Ingress": "/apis/networking.k8s.io/v1/namespaces/{}/ingresses/",
            "ServiceMonitor": "/apis/monitoring.coreos.com/v1/namespaces/{}/servicemonitors/",
        }
        paths = []
        for obj in rendered[1:]:
            obj = copy.deepcopy(obj)
            obj["metadata"].update(uid="resource-uid", resourceVersion="1")
            obj["status"] = {"phase": "Bound"}
            path = (
                routes[obj["kind"]].format(request.target.raw["namespace"])
                + obj["metadata"]["name"]
            )
            f.cluster.objects[path] = obj
            paths.append(path)
        self.assertTrue(f.runtime.readiness()["ready"])
        f.cluster.objects[paths[1]]["status"]["phase"] = "Pending"
        self.assertFalse(f.runtime.readiness()["ready"])
        f.cluster.objects[paths[1]]["status"]["phase"] = "Bound"
        f.cluster.objects[paths[-1]]["spec"]["endpoints"][0]["authorization"]["credentials"][
            "key"
        ] = "wrong-token"
        with self.assertRaises(ValueError):
            f.runtime.readiness()

    def test_full_reference_oracle_covers_every_scenario_without_demo_exclusions(self):
        from dataclasses import asdict
        from datetime import timedelta

        from image_build_tests.reviewed_policy import reviewed
        from sre_suite.target_service.prepare import prepare
        from sre_suite.verification.contracts import VerificationPolicy
        from tests.test_foundation import NOW

        class Config:
            def url(self, backend):
                return "https://localhost:9443"

        v, checks, measured = reviewed(self.f.scope, "d" * 40, "sha256:" + "a" * 64, Config())
        policy = VerificationPolicy.parse(v)
        ready = copy.deepcopy(self.raw["build"])
        ready["producer"] = "delivery"
        result = prepare(
            dict(
                plan=dict(
                    release=dict(
                        scope=asdict(self.f.scope),
                        artifact_digest="sha256:" + "a" * 64,
                        plan_digest="sha256:" + "b" * 64,
                        required_gates=["certification"],
                    ),
                    artifact_kind="container-image-v1",
                    context_version=0,
                    window_start=NOW.isoformat(),
                    window_end=(NOW + timedelta(seconds=15)).isoformat(),
                    policy_sha256=policy.fingerprint,
                    catalog_sha256=policy.catalog_sha256,
                ),
                verification_policy=v,
                checks=checks,
                runner="runner",
                runner_queue="runner-queue",
                assessor_queue="assessor-queue",
                origin=dict(
                    base_url="https://service.example.invalid",
                    authorization_env="SRE_JOURNEY_SERVICE_AUTH",
                    ca_env=None,
                ),
                target_service=dict(
                    build=self.raw["build"],
                    readiness=ready,
                    builder="builder",
                    delivery="delivery",
                    maximum_age_seconds=300,
                ),
            )
        )
        self.assertEqual(len(result["fixture_policy"]["checks"]), 120)
        self.assertEqual(
            set(c["scenario"] for c in checks.values()), {"health", "unicode", "error"}
        )
        self.assertTrue(all(r.applicable for r in policy.rules))
        self.assertIn("LOGS-010", measured)
        self.assertNotIn("PROMQL-009", measured)  # HTTP counts cannot prove scrape availability.

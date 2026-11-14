"""Synthetic cluster state and independently authenticated build/stage evidence."""

import copy
import json
from dataclasses import asdict
from datetime import timedelta

from sre_suite.auth import Action, TokenAuthenticator
from sre_suite.control.api import ControlApi, ControlPolicy
from sre_suite.control.client import ControlClient
from sre_suite.control.store import prepare_sqlite
from sre_suite.delivery.contracts import DeliveryRequest, preview_namespace
from sre_suite.delivery.helm import CHART_DIGEST, resources
from sre_suite.delivery.providers import GkeInventory, KubernetesArgo
from sre_suite.delivery.runtime import DeliveryRuntime
from sre_suite.execution.evidence_client import EvidenceClient
from sre_suite.execution.http import RemoteError
from sre_suite.execution.journal import Journal
from sre_suite.execution.wire import canonical
from tests.execution_fixture import Fixture
from tests.test_api import AUDIENCE, credential
from tests.test_foundation import NOW
from tests.verification_fixture import TokenTransport


class FakeCluster:
    base = "https://cluster.example.invalid"

    def __init__(self):
        self.objects = {}
        self.calls = []
        self.count = 0
        self.before = lambda method, path, value: None
        self.drop_after = False

    def call(self, method, path, value=None):
        self.calls.append((method, path, copy.deepcopy(value)))
        self.before(method, path, value)
        if method == "GET":
            if path not in self.objects:
                raise RemoteError(404)
            return copy.deepcopy(self.objects[path])
        if method in {"POST", "PUT"}:
            obj = copy.deepcopy(value)
            if method == "POST":
                path += "/" + obj["metadata"]["name"]
                if path in self.objects:
                    raise RemoteError(409)
                self.count += 1
                obj["metadata"]["uid"] = "uid-" + str(self.count)
                obj["metadata"]["resourceVersion"] = "1"
            else:
                old = self.objects[path]
                if old["metadata"]["resourceVersion"] != obj["metadata"]["resourceVersion"]:
                    raise RemoteError(409)
                obj["metadata"]["resourceVersion"] = str(
                    int(obj["metadata"]["resourceVersion"]) + 1
                )
            self.objects[path] = obj
            if self.drop_after:
                self.drop_after = False
                raise RemoteError(0)
            return copy.deepcopy(obj)
        if method == "DELETE":
            old = self.objects[path]
            if value["preconditions"] != {
                k: old["metadata"][k] for k in ("uid", "resourceVersion")
            }:
                raise RemoteError(409)
            del self.objects[path]
            return {}
        raise AssertionError(method)

    def healthy(self, request):
        target = request.target
        application = self.objects[KubernetesArgo.application_path(target)]
        application.pop("operation", None)
        application["status"] = {
            "health": {"status": "Healthy"},
            "sync": {"status": "Synced", "revision": target.raw["git_revision"]},
            "operationState": {
                "phase": "Succeeded",
                "syncResult": {"revision": target.raw["git_revision"]},
            },
        }
        deployment = copy.deepcopy(resources(request)[0])
        deployment["metadata"].update(uid="deployment-uid", resourceVersion="1", generation=1)
        replicas = target.raw["replicas"]
        deployment["status"] = dict(
            observedGeneration=1,
            replicas=replicas,
            readyReplicas=replicas,
            updatedReplicas=replicas,
            availableReplicas=replicas,
        )
        # Admission defaults must not turn an otherwise exact template into a false negative.
        deployment["spec"]["template"]["spec"]["containers"][0]["terminationMessagePolicy"] = "File"
        pod = {
            "metadata": copy.deepcopy(deployment["spec"]["template"]["metadata"]),
            "spec": copy.deepcopy(deployment["spec"]["template"]["spec"]),
            "status": {
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "True"}],
                "containerStatuses": [
                    {
                        "ready": True,
                        "state": {"running": {}},
                        "imageID": "docker-pullable://" + target.raw["image"],
                    }
                ],
            },
        }
        pod["metadata"].update(uid="pod-uid", resourceVersion="1")
        from urllib.parse import urlencode

        query = urlencode(
            {"labelSelector": "sre-suite/owner=" + KubernetesArgo.ownership(target)[:63]}
        )
        ns = target.raw["namespace"]
        self.objects["/apis/apps/v1/namespaces/" + ns + "/deployments?" + query] = {
            "items": [deployment]
        }
        self.objects["/api/v1/namespaces/" + ns + "/pods?" + query] = {"items": [pod]}


class FakeGke:
    def call(self, method, path, value=None):
        assert (
            method == "GET" and path == "/v1/projects/synthetic/locations/us-central1/clusters/test"
        )
        return dict(
            name="test",
            location="us-central1",
            id="cluster-uid",
            status="RUNNING",
            endpoint="cluster.example.invalid",
        )


class FakeHelm:
    def render(self, request, heartbeat):
        heartbeat()
        return {"chart_digest": CHART_DIGEST}


class DeliveryFixture:
    def __init__(self, root):
        self.f = Fixture(root)
        self.scope = self.f.ticket.scope
        self.roles = {r: r + "-actor" for r in ("implement", "review", "test", "qa")}
        subjects = [*self.roles.values(), "builder", "human", "delivery"]
        self.tokens = {s: "synthetic-" + s + "x" * 43 for s in subjects}
        auth = TokenAuthenticator(
            AUDIENCE,
            tuple(
                credential(self.scope, tuple(Action), token=t, subject=s)
                for s, t in self.tokens.items()
            ),
        )
        self.app = ControlApi(
            auth,
            prepare_sqlite(str(root / "evidence.db")),
            clock=lambda: NOW,
            policy=ControlPolicy(frozenset({"human"}), frozenset()),
        )
        self.evidence = self.client("delivery")
        self.control = ControlClient(TokenTransport(self.app, self.tokens["delivery"]))
        self.human = ControlClient(TokenTransport(self.app, self.tokens["human"]))
        stages = {}
        for role, subject in self.roles.items():
            stages[role] = self.client(subject).upload(
                role,
                self.scope,
                canonical(
                    {
                        "role": role,
                        "verdict": "passed",
                        "manifest": "b" * 64,
                        "prior_evidence_sha256": [r.sha256 for r in stages.values()],
                    }
                ).encode(),
                NOW,
                NOW + timedelta(minutes=40),
            )
        build = self.client("builder").upload(
            "build",
            self.scope,
            canonical(
                {
                    "schema_version": 2,
                    "kind": "image-build",
                    "source_repository": "example/demo",
                    "source_revision": self.scope.revision,
                    "candidate_revision": "d" * 40,
                    "source_artifact_digest": "sha256:" + stages["implement"].sha256,
                    "plan_digest": "sha256:" + "b" * 64,
                    "image_digest": "sha256:" + "a" * 64,
                    "stage_artifacts": {r: e.sha256 for r, e in stages.items()},
                    "chart_digest": CHART_DIGEST,
                    "chart_revision": "c" * 40,
                }
            ).encode(),
            NOW,
            NOW + timedelta(minutes=40),
        )
        target = {
            "environment": "development",
            "scope": asdict(self.scope),
            "context_version": 0,
            "project": "synthetic",
            "location": "us-central1",
            "cluster": "test",
            "cluster_uid": "cluster-uid",
            "namespace": preview_namespace(self.scope),
            "application": "demo",
            "argo_namespace": "argocd",
            "argo_project": "synthetic",
            "git_url": "https://github.com/example/gitops",
            "git_revision": "c" * 40,
            "chart_path": "charts/service",
            "chart_digest": CHART_DIGEST,
            "image": "example/demo@sha256:" + "a" * 64,
            "port": 8080,
            "replicas": 1,
            "health_path": "/health",
            "phase": "preview",
            "source_repository": "example/demo",
            "candidate_revision": "d" * 40,
            "expected_previous": None,
            "runtime_environment": [],
        }
        self.raw = json.loads(
            canonical(
                {
                    "schema_version": 2,
                    "binding": {
                        "scope": asdict(self.scope),
                        "context_version": 0,
                        "artifact_digest": "sha256:" + "a" * 64,
                        "plan_digest": "sha256:" + "b" * 64,
                        "action": "promote",
                        "target": target,
                    },
                    "build": asdict(build),
                    "stages": {r: asdict(e) for r, e in stages.items()},
                    "verification": None,
                    "expires_at": (NOW + timedelta(minutes=30)).isoformat(),
                }
            )
        )
        self.request = DeliveryRequest.parse(self.raw)
        self.cluster = FakeCluster()
        self.runtime = DeliveryRuntime(
            self.request,
            self.control,
            self.evidence,
            Journal(root / "delivery"),
            GkeInventory(FakeGke()),
            KubernetesArgo(self.cluster, lambda: None),
            FakeHelm(),
            "builder",
            self.roles,
            clock=lambda: NOW,
        )

    def client(self, subject):
        return EvidenceClient(
            "https://evidence.invalid",
            self.tokens[subject],
            TokenTransport(self.app, self.tokens[subject]),
        )

    def approve(self, request=None):
        request = request or self.runtime.request
        return self.human.call(
            "approve",
            self.scope,
            {
                "binding": request.binding.wire(),
                "expires_at": (NOW + timedelta(minutes=30)).isoformat(),
            },
        )

    def close(self):
        self.f.close()

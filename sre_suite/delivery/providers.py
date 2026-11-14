"""GKE inventory and Kubernetes/Argo CD CRD adapters with ownership and resourceVersion fencing."""

from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode, urlsplit

from ..api_wire import text
from ..execution.http import JsonHttp, RemoteError
from ..execution.wire import digest
from .contracts import Target


def contains_expected(actual: object, expected: object) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and contains_expected(actual[k], v) for k, v in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(contains_expected(a, e) for a, e in zip(actual, expected, strict=True))
        )
    return type(actual) is type(expected) and actual == expected


def recipe_matches(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    # Kubernetes adds defaults, but must not add execution commands, credentials or sidecars.
    containers = actual.get("containers", [])
    if not contains_expected(actual, expected) or not isinstance(containers, list):
        return False
    allowed_defaults = {"terminationMessagePath", "terminationMessagePolicy"}
    for container, planned in zip(containers, expected["containers"], strict=True):
        if container.get("securityContext") != planned.get("securityContext"):
            return False
        if set(container) - set(planned) - allowed_defaults:
            return False
    if actual.get("securityContext") != expected.get("securityContext"):
        return False
    if (
        actual.get("initContainers")
        or actual.get("ephemeralContainers")
        or any(actual.get(k) for k in ("hostNetwork", "hostPID", "hostIPC"))
        or actual.get("automountServiceAccountToken") is not False
    ):
        return False
    return True


class GkeInventory:
    def __init__(self, http: JsonHttp) -> None:
        self.http = http

    def verify(self, target: Target, kubernetes_url: str) -> dict[str, Any]:
        value = target.raw
        path = "/v1/projects/{project}/locations/{location}/clusters/{cluster}".format(**value)
        result = self.http.call("GET", path)
        if not isinstance(result, dict) or result.get("status") != "RUNNING":
            raise ValueError("GKE cluster is not running")
        if (
            result.get("name") != value["cluster"]
            or result.get("location") != value["location"]
            or result.get("id") != value["cluster_uid"]
        ):
            raise ValueError("GKE cluster identity differs")
        private = result.get("privateClusterConfig", {})
        endpoints = result.get("controlPlaneEndpointsConfig", {})
        dns = endpoints.get("dnsEndpointConfig", {}) if isinstance(endpoints, dict) else {}
        allowed = {
            result.get("endpoint"),
            private.get("privateEndpoint"),
            private.get("publicEndpoint"),
            dns.get("endpoint"),
        }
        if (
            not urlsplit(kubernetes_url).hostname
            or urlsplit(kubernetes_url).hostname not in allowed
        ):
            raise ValueError("Kubernetes endpoint differs from selected GKE cluster")
        return {"cluster_uid": result["id"], "name": result["name"], "status": result["status"]}


class KubernetesArgo:
    def __init__(self, http: JsonHttp, guard: Callable[[], None]) -> None:
        self.http, self.guard = http, guard

    def call(self, method: str, path: str, payload: object = None) -> Any:
        self.guard()
        result = self.http.call(method, path, payload)
        self.guard()
        return result

    def read(self, path: str) -> dict[str, Any] | None:
        try:
            result = self.call("GET", path)
        except RemoteError as error:
            if error.status == 404:
                return None
            raise
        if not isinstance(result, dict):
            raise ValueError("Kubernetes object required")
        return result

    @staticmethod
    def labels(target: Target) -> dict[str, str]:
        scope = target.scope
        return {
            "sre-suite/service": scope.service,
            "sre-suite/environment-id": scope.environment_id,
            "sre-suite/generation": str(scope.generation),
        }

    @staticmethod
    def ownership(target: Target) -> str:
        # Resource ownership survives a separately approved cleanup/rollback action.
        return digest(
            {k: target.raw[k] for k in ("scope", "cluster_uid", "namespace", "application")}
        )

    def own(self, target: Target, obj: dict[str, Any]) -> None:
        metadata = obj.get("metadata", {})
        if metadata.get("annotations", {}).get("sre-suite/owner") != self.ownership(target) or any(
            metadata.get("labels", {}).get(k) != v for k, v in self.labels(target).items()
        ):
            raise ValueError("resource ownership or preview generation differs")
        if not metadata.get("uid") or not metadata.get("resourceVersion"):
            raise ValueError("resource UID/version missing")

    def namespace(self, target: Target) -> dict[str, Any]:
        name = target.raw["namespace"]
        path = "/api/v1/namespaces/" + name
        previous = self.read(path)
        if previous is None:
            self.call(
                "POST",
                "/api/v1/namespaces",
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {
                        "name": name,
                        "labels": self.labels(target),
                        "annotations": {"sre-suite/owner": self.ownership(target)},
                    },
                },
            )
            previous = self.read(path)
        if previous is None:
            raise ValueError("namespace create was not observed")
        expected = target.raw["expected_previous"]
        if (
            previous.get("metadata", {}).get("annotations", {}).get("sre-suite/owner")
            != self.ownership(target)
            and expected is not None
        ):
            metadata = previous["metadata"]
            if (
                metadata.get("uid") != expected["namespace_uid"]
                or metadata.get("annotations", {}).get("sre-suite/owner") != expected["owner"]
            ):
                raise ValueError("namespace is not the approved previous target")
            updated = {
                **previous,
                "metadata": {
                    **metadata,
                    "labels": {**metadata.get("labels", {}), **self.labels(target)},
                    "annotations": {
                        **metadata.get("annotations", {}),
                        "sre-suite/owner": self.ownership(target),
                    },
                },
            }
            updated.pop("status", None)
            self.call("PUT", path, updated)
            previous = self.read(path)
            if previous is None:
                raise ValueError("namespace disappeared")
        self.own(target, previous)
        if previous.get("metadata", {}).get("deletionTimestamp"):
            raise ValueError("namespace is terminating")
        return previous

    @staticmethod
    def application_path(target: Target) -> str:
        return (
            "/apis/argoproj.io/v1alpha1/namespaces/"
            + str(target.raw["argo_namespace"])
            + "/applications/"
            + str(target.raw["application"])
        )

    def apply(self, target: Target, app: dict[str, Any]) -> dict[str, Any]:
        path = self.application_path(target)
        previous = self.read(path)
        if previous:
            if previous.get("spec") != app["spec"]:
                expected = target.raw["expected_previous"]
                metadata = previous.get("metadata", {})
                if (
                    expected is None
                    or metadata.get("uid") != expected["application_uid"]
                    or metadata.get("annotations", {}).get("sre-suite/owner") != expected["owner"]
                    or digest(previous.get("spec")) != expected["spec_sha256"]
                    or previous.get("operation") is not None
                ):
                    raise ValueError("application is not the approved previous release")
                updated = {
                    **app,
                    "metadata": {
                        **app["metadata"],
                        "uid": metadata["uid"],
                        "resourceVersion": metadata["resourceVersion"],
                    },
                }
                self.call("PUT", path, updated)
                previous = self.read(path)
                if previous is None:
                    raise ValueError("application disappeared during replacement")
            self.own(target, previous)
        else:
            self.call("POST", path.rsplit("/", 1)[0], app)
            previous = self.read(path)
        if previous is None:
            raise ValueError("application was not observed")
        self.own(target, previous)
        return previous

    def sync(self, target: Target, app: dict[str, Any]) -> dict[str, Any]:
        self.own(target, app)
        revision = target.raw["git_revision"]
        existing = app.get("operation")
        operation = {"sync": {"revision": revision, "prune": False}}
        if existing is not None:
            if existing != operation:
                raise ValueError("another Argo operation is active")
            return app
        status = app.get("status", {})
        completed = status.get("operationState", {})
        if (
            completed.get("phase") == "Succeeded"
            and completed.get("syncResult", {}).get("revision") == revision
        ):
            return app
        # PUT retains UID/resourceVersion; Kubernetes rejects concurrent object replacement.
        updated = {**app, "operation": operation}
        updated.pop("status", None)
        self.call("PUT", self.application_path(target), updated)
        result = self.read(self.application_path(target))
        if result is None:
            raise ValueError("application disappeared during sync")
        self.own(target, result)
        return result

    def readiness(self, target: Target, expected_template: dict[str, Any]) -> dict[str, Any]:
        app = self.read(self.application_path(target))
        if app is None:
            return {"ready": False, "reason": "application_missing"}
        self.own(target, app)
        status = app.get("status", {})
        if (
            status.get("sync", {}).get("status") != "Synced"
            or status.get("sync", {}).get("revision") != target.raw["git_revision"]
            or status.get("health", {}).get("status") != "Healthy"
            or app.get("operation") is not None
        ):
            return {"ready": False, "reason": "argo_not_healthy"}
        ns = target.raw["namespace"]
        selector = urlencode({"labelSelector": "sre-suite/owner=" + self.ownership(target)[:63]})
        deployments = self.read("/apis/apps/v1/namespaces/" + ns + "/deployments?" + selector)
        pods = self.read("/api/v1/namespaces/" + ns + "/pods?" + selector)
        if (
            not deployments
            or not pods
            or deployments.get("metadata", {}).get("continue")
            or pods.get("metadata", {}).get("continue")
        ):
            raise ValueError("incomplete readiness inventory")
        ds, ps = deployments.get("items"), pods.get("items")
        if (
            not isinstance(ds, list)
            or len(ds) != 1
            or not isinstance(ps, list)
            or not ps
            or len(ps) > 100
        ):
            return {"ready": False, "reason": "workloads_missing"}
        deployment = ds[0]
        self.own(target, deployment)
        state = deployment.get("status", {})
        replicas = target.raw["replicas"]
        template = deployment.get("spec", {}).get("template", {})
        expected_image = target.raw["image"]
        template_spec = template.get("spec", {})
        expected_spec = expected_template["spec"]
        if not contains_expected(
            template.get("metadata", {}), expected_template["metadata"]
        ) or not recipe_matches(template_spec, expected_spec):
            raise ValueError("deployed template differs from approved recipe")
        if (
            deployment.get("spec", {}).get("replicas") != replicas
            or state.get("observedGeneration") != deployment["metadata"].get("generation")
            or any(
                state.get(k, 0) != replicas
                for k in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")
            )
            or state.get("unavailableReplicas", 0) != 0
            or [c.get("image") for c in template.get("spec", {}).get("containers", [])]
            != [expected_image]
        ):
            return {"ready": False, "reason": "rollout_incomplete"}
        running = []
        for pod in ps:
            self.own(target, pod)
            if pod.get("metadata", {}).get("deletionTimestamp"):
                continue
            pod_spec = pod.get("spec", {})
            if (
                not recipe_matches(pod_spec, expected_spec)
                or pod_spec.get("initContainers")
                or pod_spec.get("ephemeralContainers")
                or any(pod_spec.get(k) for k in ("hostNetwork", "hostPID", "hostIPC"))
                or pod.get("status", {}).get("phase") != "Running"
            ):
                return {"ready": False, "reason": "pod_execution_mismatch"}
            states = pod.get("status", {}).get("containerStatuses", [])
            images = [c.get("image") for c in pod.get("spec", {}).get("containers", [])]
            if (
                images != [expected_image]
                or len(states) != 1
                or states[0].get("ready") is not True
                or "running" not in states[0].get("state", {})
                or not any(
                    c.get("type") == "Ready" and c.get("status") == "True"
                    for c in pod.get("status", {}).get("conditions", [])
                )
                or not str(states[0].get("imageID", "")).endswith(
                    "@" + expected_image.split("@", 1)[1]
                )
            ):
                return {"ready": False, "reason": "pod_image_or_health_mismatch"}
            running.append({"uid": pod["metadata"]["uid"], "image_id": states[0]["imageID"]})
        if len(running) != replicas:
            return {"ready": False, "reason": "pod_count_mismatch"}
        return {
            "ready": True,
            "namespace": ns,
            "application_uid": app["metadata"]["uid"],
            "deployment_uid": deployment["metadata"]["uid"],
            "pods": running,
            "image": expected_image,
            "git_revision": target.raw["git_revision"],
        }

    def cleanup(self, target: Target) -> dict[str, Any]:
        path = self.application_path(target)
        app = self.read(path)
        if app:
            self.own(target, app)
            if app.get("operation") is not None:
                raise ValueError("wait for the active Argo operation before cleanup")
            self.delete(path, app)
            return {"complete": False, "waiting": "application_deletion"}
        path = "/api/v1/namespaces/" + target.raw["namespace"]
        namespace = self.read(path)
        if namespace:
            self.own(target, namespace)
            self.delete(path, namespace)
            return {"complete": False, "waiting": "namespace_deletion"}
        return {"complete": True}

    def delete(self, path: str, obj: dict[str, Any]) -> None:
        metadata = obj["metadata"]
        self.call(
            "DELETE",
            path,
            {
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "preconditions": {
                    "uid": text(metadata["uid"]),
                    "resourceVersion": text(metadata["resourceVersion"]),
                },
                "propagationPolicy": "Foreground",
            },
        )

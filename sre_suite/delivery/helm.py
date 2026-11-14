"""Packaged Helm renderer with exact JSON resources and no plugin/network configuration."""

import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from ..execution.process import minimal_environment, run_process
from ..execution.wire import canonical, digest
from .contracts import DeliveryRequest
from .providers import KubernetesArgo

CHART = "apiVersion: v2\nname: sre-service\nversion: 1.0.0\ntype: application\n"
TEMPLATE = "{{- range .Values.resources }}\n---\n{{ . | toJson }}\n{{- end }}\n"
CHART_DIGEST = "sha256:" + digest({"Chart.yaml": CHART, "templates/resources.yaml": TEMPLATE})


def resources(request: DeliveryRequest) -> list[dict[str, Any]]:
    target = request.target
    raw = target.raw
    owner = KubernetesArgo.ownership(target)
    labels = {
        **KubernetesArgo.labels(target),
        "sre-suite/owner": owner[:63],
        "app.kubernetes.io/name": raw["application"],
    }
    annotations = {
        "sre-suite/owner": owner,
        "sre-suite/artifact": request.binding.artifact_digest,
        "sre-suite/plan": request.binding.plan_digest,
    }
    metadata = {
        "name": raw["application"],
        "namespace": raw["namespace"],
        "labels": labels,
        "annotations": annotations,
    }
    attrs = {
        "service.name": target.scope.service,
        "deployment.environment.name": str(target.scope.environment),
        "service.version": raw["candidate_revision"],
        "sre.source.revision": target.scope.revision,
        "sre.environment.id": target.scope.environment_id,
        "sre.environment.generation": str(target.scope.generation),
        "sre.artifact.digest": request.binding.artifact_digest,
        "sre.plan.digest": request.binding.plan_digest,
    }
    if any("," in v or "=" in v for v in attrs.values()):
        raise ValueError("resource attributes cannot contain separators")
    probe = {
        "httpGet": {"path": raw["health_path"], "port": raw["port"]},
        "periodSeconds": 5,
        "timeoutSeconds": 2,
        "failureThreshold": 6,
    }
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": metadata,
        "spec": {
            "replicas": raw["replicas"],
            "selector": {"matchLabels": {"app.kubernetes.io/name": raw["application"]}},
            "template": {
                "metadata": {"labels": labels, "annotations": annotations},
                "spec": {
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "service",
                            "image": raw["image"],
                            "imagePullPolicy": "IfNotPresent",
                            "ports": [{"name": "http", "containerPort": raw["port"]}],
                            "env": [
                                *raw["runtime_environment"],
                                {"name": "OTEL_SERVICE_NAME", "value": target.scope.service},
                                {
                                    "name": "OTEL_RESOURCE_ATTRIBUTES",
                                    "value": ",".join(k + "=" + v for k, v in attrs.items()),
                                },
                            ],
                            "readinessProbe": probe,
                            "livenessProbe": {**probe, "initialDelaySeconds": 30},
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "128Mi"},
                                "limits": {"cpu": "1", "memory": "512Mi"},
                            },
                            "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
                        }
                    ],
                    "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "128Mi"}}],
                },
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": metadata,
        "spec": {
            "type": "ClusterIP",
            "selector": {"sre-suite/owner": owner[:63]},
            "ports": [{"name": "http", "port": raw["port"], "targetPort": raw["port"]}],
        },
    }
    extra = []
    if "reference_service" in raw:
        from ..target_service.deployment import resources as reference_resources

        extra = reference_resources(request, deployment, metadata)
    return [deployment, service, *extra]


def application(request: DeliveryRequest) -> dict[str, Any]:
    target = request.target
    value = target.raw
    if value["chart_digest"] != CHART_DIGEST:
        raise ValueError("unsupported chart recipe; review and pin a new policy")
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {
            "name": value["application"],
            "namespace": value["argo_namespace"],
            "labels": KubernetesArgo.labels(target),
            "annotations": {"sre-suite/owner": KubernetesArgo.ownership(target)},
            "finalizers": ["resources-finalizer.argocd.argoproj.io"],
        },
        "spec": {
            "project": value["argo_project"],
            "destination": {
                "server": "https://kubernetes.default.svc",
                "namespace": value["namespace"],
            },
            "source": {
                "repoURL": value["git_url"],
                "targetRevision": value["git_revision"],
                "path": value["chart_path"],
                "helm": {"valuesObject": {"resources": resources(request)}},
            },
        },
    }


class HelmRenderer:
    def __init__(self, binary: str) -> None:
        self.binary = binary

    def render(self, request: DeliveryRequest, heartbeat: Callable[[], None]) -> dict[str, Any]:
        expected = resources(request)
        with TemporaryDirectory(prefix="sre-helm-") as directory:
            root = Path(directory)
            chart = root / "chart"
            (chart / "templates").mkdir(parents=True)
            (chart / "Chart.yaml").write_text(CHART)
            (chart / "templates/resources.yaml").write_text(TEMPLATE)
            values = root / "values.json"
            values.write_text(canonical({"resources": expected}))
            env = minimal_environment()
            env.update(
                HELM_CONFIG_HOME=str(root / "config"),
                HELM_CACHE_HOME=str(root / "cache"),
                HELM_DATA_HOME=str(root / "data"),
                HELM_PLUGINS=str(root / "plugins"),
            )
            result = run_process(
                [
                    self.binary,
                    "template",
                    request.target.raw["application"],
                    str(chart),
                    "--namespace",
                    request.target.raw["namespace"],
                    "--values",
                    str(values),
                    "--skip-tests",
                ],
                root,
                env,
                timeout=30,
                max_output=262144,
                heartbeat=heartbeat,
            )
            if result.code:
                raise ValueError("Helm render failed")
            clean = "\n".join(
                line for line in result.output.decode().splitlines() if not line.startswith("#")
            )
            rendered = [json.loads(part) for part in clean.split("---") if part.strip()]
            if sorted(map(canonical, rendered)) != sorted(map(canonical, expected)):
                raise ValueError("Helm output differs from reviewed resources")
            return {
                "chart_digest": CHART_DIGEST,
                "resources_sha256": digest(rendered),
                "scope": asdict(request.target.scope),
            }

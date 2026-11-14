"""Reviewed nonproduction reference profile; no credentials or infrastructure creation."""

import copy
import re
from typing import Any

from ..api_wire import fields, text
from ..contracts import Environment
from ..execution.wire import digest


def validate(value: object, target: dict[str, Any]) -> dict[str, Any]:
    from ..delivery.contracts import name

    raw = dict(
        fields(
            value,
            {
                "storage_class",
                "storage_gib",
                "ingress_class",
                "hostname",
                "tls_secret",
                "credentials_secret",
                "export_ca_secret",
                "monitor_labels",
            },
        )
    )
    for key in (
        "storage_class",
        "ingress_class",
        "tls_secret",
        "credentials_secret",
        "export_ca_secret",
    ):
        name(raw[key])
    host = text(raw["hostname"], 253)
    if not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)+", host):
        raise ValueError("exact private DNS hostname required")
    if type(raw["storage_gib"]) is not int or not 1 <= raw["storage_gib"] <= 100:
        raise ValueError("bounded persistent storage required")
    labels = raw["monitor_labels"]
    if not isinstance(labels, dict) or len(labels) > 10:
        raise ValueError("bounded monitor selection labels required")
    for key, val in labels.items():
        name(key)
        name(val)
    if (
        target["environment"] == Environment.PRODUCTION
        or target["replicas"] != 1
        or target["health_path"] != "/ready"
        or target["port"] != 8080
    ):
        raise ValueError("reference requires nonproduction, one replica and port 8080 /ready")
    env = {e["name"]: e for e in target["runtime_environment"]}
    expected = environment(raw)
    for item in expected:
        if env.get(item["name"]) != item:
            raise ValueError("reference requires exact state, CA and private secret references")
    for signal, path in (("TRACES", "/v1/traces"), ("LOGS", "/loki/api/v1/push")):
        from urllib.parse import urlsplit

        from ..execution.config import https_url

        entry = env.get("SRE_TARGET_" + signal + "_URL", {})
        url = https_url(text(entry.get("value")))
        if urlsplit(url).path != path:
            raise ValueError("exact HTTPS export route required")
    return copy.deepcopy(raw)


def environment(profile: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"name": "SRE_TARGET_STATE", "value": "/state/reference.db"},
        {"name": "SRE_TARGET_EXPORT_CA_FILE", "value": "/trust/ca.crt"},
        *[
            {
                "name": "SRE_TARGET_" + variable,
                "valueFrom": {"secretKeyRef": {"name": profile["credentials_secret"], "key": key}},
            }
            for variable, key in (
                ("TOKEN", "workload-token"),
                ("METRICS_TOKEN", "metrics-token"),
                ("TRACES_AUTH", "traces-authorization"),
                ("LOGS_AUTH", "logs-authorization"),
            )
        ],
    ]


def resources(
    request: Any, deployment: dict[str, Any], metadata: dict[str, Any]
) -> list[dict[str, Any]]:
    profile = request.target.raw["reference_service"]
    # A different image/plan must never attach the previous release's SQLite binding.
    claim = (
        "reference-"
        + digest(
            {
                "scope": request.target.raw["scope"],
                "artifact": request.binding.artifact_digest,
                "plan": request.binding.plan_digest,
            }
        )[:40]
    )
    pod = deployment["spec"]["template"]["spec"]
    deployment["spec"]["strategy"] = {"type": "Recreate"}
    pod["securityContext"].update(
        runAsUser=65532, runAsGroup=65532, fsGroup=65532, fsGroupChangePolicy="OnRootMismatch"
    )
    pod["containers"][0]["volumeMounts"].extend(
        [
            {"name": "state", "mountPath": "/state"},
            {"name": "export-ca", "mountPath": "/trust", "readOnly": True},
        ]
    )
    pod["volumes"].extend(
        [
            {"name": "state", "persistentVolumeClaim": {"claimName": claim}},
            {
                "name": "export-ca",
                "secret": {
                    "secretName": profile["export_ca_secret"],
                    "defaultMode": 292,
                    "items": [{"key": "ca.crt", "path": "ca.crt"}],
                },
            },
        ]
    )
    pvc = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {**copy.deepcopy(metadata), "name": claim},
        "spec": {
            "accessModes": ["ReadWriteOncePod"],
            "storageClassName": profile["storage_class"],
            "resources": {"requests": {"storage": str(profile["storage_gib"]) + "Gi"}},
        },
    }
    ingress = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": copy.deepcopy(metadata),
        "spec": {
            "ingressClassName": profile["ingress_class"],
            "tls": [{"hosts": [profile["hostname"]], "secretName": profile["tls_secret"]}],
            "rules": [
                {
                    "host": profile["hostname"],
                    "http": {
                        "paths": [
                            {
                                "path": "/",
                                "pathType": "Prefix",
                                "backend": {
                                    "service": {"name": metadata["name"], "port": {"number": 8080}}
                                },
                            }
                        ]
                    },
                }
            ],
        },
    }
    monitor = {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "ServiceMonitor",
        "metadata": {
            **copy.deepcopy(metadata),
            "labels": {**metadata["labels"], **profile["monitor_labels"]},
        },
        "spec": {
            "selector": {"matchLabels": {"sre-suite/owner": metadata["labels"]["sre-suite/owner"]}},
            "namespaceSelector": {"matchNames": [metadata["namespace"]]},
            "endpoints": [
                {
                    "port": "http",
                    "path": "/metrics",
                    "scheme": "http",
                    "interval": "5s",
                    "scrapeTimeout": "4s",
                    "followRedirects": False,
                    "authorization": {
                        "type": "Bearer",
                        "credentials": {
                            "name": profile["credentials_secret"],
                            "key": "metrics-token",
                        },
                    },
                }
            ],
        },
    }
    return [pvc, ingress, monitor]


def package(value: object) -> dict[str, Any]:
    from ..control.contracts import wire
    from ..delivery.contracts import DeliveryRequest
    from ..delivery.helm import application
    from ..delivery.helm import resources as render

    request = DeliveryRequest.parse(value)
    if "reference_service" not in request.target.raw or request.binding.action != "promote":
        raise ValueError("reference promotion request required")
    rendered = render(request)
    return {
        "binding": request.binding.wire(),
        "resources": rendered,
        "application": application(request),
        "resources_sha256": digest(rendered),
        "execution_started": False,
        "request": wire(value),
    }

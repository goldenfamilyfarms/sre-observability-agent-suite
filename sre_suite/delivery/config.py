"""Private delivery policy and scoped provider credentials; no default cloud account discovery."""

import hashlib
import os
import ssl
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, build_opener

from ..api_wire import fields, text
from ..control.client import ControlClient
from ..execution.config import https_url
from ..execution.http import JsonHttp, NoRedirect
from ..execution.journal import Journal
from ..execution.wire import digest
from ..fixtures.contracts import queue
from ..telemetry.schedule_config import private_path
from ..verification.config import client, load_policy, private_document
from ..verification.runtime import CertificationEngine
from .contracts import DeliveryRequest
from .helm import HelmRenderer
from .providers import GkeInventory, KubernetesArgo
from .runtime import DeliveryRuntime


def load(path: Path) -> dict[str, Any]:
    value = fields(
        private_document(path),
        {
            "request",
            "journal",
            "task_queue",
            "builder",
            "roles",
            "verification_policy",
            "kubernetes_url",
            "kubernetes_ca",
            "kubernetes_ca_sha256",
            "helm_binary",
            "helm_sha256",
        },
    )
    request = DeliveryRequest.parse(private_document(Path(text(value["request"]))))
    roles = fields(value["roles"], {"implement", "review", "test", "qa"})
    if len({text(s, 128) for s in [value["builder"], *roles.values()]}) != 5:
        raise ValueError("independent provider subjects required")
    queue(value["task_queue"])
    root = Path(text(value["journal"]))
    private_path(root, directory=True)
    https_url(text(value["kubernetes_url"]), allow_loopback=bool(value["kubernetes_ca"]))
    helm = Path(text(value["helm_binary"]))
    if not helm.is_absolute() or not helm.is_file() or not os.access(helm, os.X_OK):
        raise ValueError("explicit executable Helm path required")
    if hashlib.sha256(helm.read_bytes()).hexdigest() != value["helm_sha256"]:
        raise ValueError("Helm binary differs from approved configuration")
    if value["kubernetes_ca"] is not None:
        ca = Path(text(value["kubernetes_ca"]))
        private_path(ca, directory=False)
        if hashlib.sha256(ca.read_bytes()).hexdigest() != value["kubernetes_ca_sha256"]:
            raise ValueError("Kubernetes CA differs from approved configuration")
    elif value["kubernetes_ca_sha256"] is not None:
        raise ValueError("CA configuration incomplete")
    if request.verification is not None and value["verification_policy"] is None:
        raise ValueError("release certification policy required")
    if value["verification_policy"] is not None:
        load_policy(Path(text(value["verification_policy"])))
    return dict(value)


def runtime(config: dict[str, Any]) -> DeliveryRuntime:
    request = DeliveryRequest.parse(private_document(Path(config["request"])))
    evidence = client(os.environ)
    control = ControlClient(
        JsonHttp(
            os.environ["SRE_CONTROL_URL"],
            "Bearer " + os.environ["SRE_CONTROL_TOKEN"],
            local_ca_file=os.environ.get("SRE_CONTROL_CA_FILE"),
        )
    )
    base = config["kubernetes_url"]
    ca = config["kubernetes_ca"]
    loopback = urlsplit(base).hostname in {"localhost", "127.0.0.1"}
    kubernetes = JsonHttp(
        base, "Bearer " + os.environ["SRE_KUBERNETES_TOKEN"], local_ca_file=ca if loopback else None
    )
    if ca and not loopback:
        # Explicit CA pin is checked by load(); redirects remain disabled.
        kubernetes._opener = build_opener(
            NoRedirect(), HTTPSHandler(context=ssl.create_default_context(cafile=ca))
        )
    journal = Journal(Path(config["journal"]))
    verifier = (
        CertificationEngine(load_policy(Path(config["verification_policy"])), evidence, journal)
        if config["verification_policy"]
        else None
    )
    return DeliveryRuntime(
        request,
        control,
        evidence,
        journal,
        GkeInventory(
            JsonHttp(
                "https://container.googleapis.com", "Bearer " + os.environ["SRE_GCP_ACCESS_TOKEN"]
            )
        ),
        KubernetesArgo(kubernetes, lambda: None),
        HelmRenderer(config["helm_binary"]),
        config["builder"],
        config["roles"],
        verifier,
    )


def fingerprint(config: dict[str, Any]) -> str:
    return digest(
        {
            "config": config,
            "request": private_document(Path(config["request"])),
            "verification_policy": load_policy(Path(config["verification_policy"])).fingerprint
            if config["verification_policy"]
            else None,
        }
    )

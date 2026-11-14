"""Strict immutable GKE/Argo release targets; credentials live only in worker environment."""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from ..api_wire import envelope, evidence_from_wire, fields, text
from ..contracts import Environment, Evidence, Scope
from ..control.contracts import Binding, sha, timestamp, wire
from ..execution.config import https_url, safe_path
from ..execution.wire import digest
from ..serialization import scope_from_dict
from ..telemetry.contracts import integer


def name(value: object) -> str:
    result = text(value, 63)
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", result):
        raise ValueError("DNS label required")
    return result


def preview_namespace(scope: Scope) -> str:
    return (
        scope.environment_id[:40] + "-g" + str(scope.generation)
        if scope.generation < 10**12
        else ("preview-" + digest(wire(scope.__dict__))[:40])
    )


@dataclass(frozen=True)
class Target:
    raw: dict[str, Any]
    scope: Scope

    @classmethod
    def parse(cls, raw: object) -> "Target":
        optional = (
            {"reference_service"} if isinstance(raw, dict) and "reference_service" in raw else set()
        )
        value = fields(
            raw,
            {
                "environment",
                "scope",
                "context_version",
                "project",
                "location",
                "cluster",
                "cluster_uid",
                "namespace",
                "application",
                "argo_namespace",
                "argo_project",
                "git_url",
                "git_revision",
                "chart_path",
                "chart_digest",
                "image",
                "port",
                "replicas",
                "health_path",
                "phase",
                "source_repository",
                "candidate_revision",
                "expected_previous",
                "runtime_environment",
            }
            | optional,
        )
        runtime_env = value["runtime_environment"]
        if not isinstance(runtime_env, list) or len(runtime_env) > 30:
            raise ValueError("bounded reviewed runtime environment required")
        names = set()
        for entry in runtime_env:
            if not isinstance(entry, dict) or set(entry) not in (
                {"name", "value"},
                {"name", "valueFrom"},
            ):
                raise ValueError("explicit value or secret reference required")
            env_name = text(entry["name"], 128)
            if (
                not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", env_name)
                or env_name in names
                or env_name in {"OTEL_SERVICE_NAME", "OTEL_RESOURCE_ATTRIBUTES"}
            ):
                raise ValueError("runtime environment cannot override release identity")
            names.add(env_name)
            if "value" in entry:
                if (
                    not isinstance(entry["value"], str)
                    or len(entry["value"]) > 2048
                    or "\0" in entry["value"]
                ):
                    raise ValueError("bounded non-secret configuration required")
            else:
                secret = fields(
                    fields(entry["valueFrom"], {"secretKeyRef"})["secretKeyRef"], {"name", "key"}
                )
                name(secret["name"])
                if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", text(secret["key"])):
                    raise ValueError("invalid private secret key reference")
        previous = value["expected_previous"]
        if previous is not None:
            previous = fields(
                previous, {"owner", "namespace_uid", "application_uid", "spec_sha256"}
            )
            for key in ("owner", "spec_sha256"):
                if not re.fullmatch(r"[0-9a-f]{64}", text(previous[key])):
                    raise ValueError("previous resource fingerprints required")
            for key in ("namespace_uid", "application_uid"):
                if not re.fullmatch(r"[a-zA-Z0-9-]{1,128}", text(previous[key])):
                    raise ValueError("previous resource UIDs required")
        scope = scope_from_dict(value["scope"])
        if value["environment"] != str(scope.environment):
            raise ValueError("target environment differs from scope")
        for key in (
            "project",
            "location",
            "cluster",
            "namespace",
            "application",
            "argo_namespace",
            "argo_project",
        ):
            name(value[key])
        text(value["cluster_uid"], 128)
        integer(value["context_version"], 0, 2**63 - 1)
        integer(value["port"], 1024, 65535)
        integer(value["replicas"], 1, 20)
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+", text(value["source_repository"])):
            raise ValueError("source repository required")
        url = https_url(text(value["git_url"]))
        if not urlsplit(url).path or value["phase"] not in {"preview", "release"}:
            raise ValueError("exact chart repository and delivery phase required")
        if not re.fullmatch(r"[0-9a-f]{40}", text(value["git_revision"])):
            raise ValueError("chart Git revision must be pinned")
        if not re.fullmatch(r"[0-9a-f]{40}", text(value["candidate_revision"])):
            raise ValueError("candidate commit required")
        safe_path(text(value["chart_path"]))
        sha(value["chart_digest"])
        if not re.fullmatch(r"[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64}", text(value["image"], 512)):
            raise ValueError("registry image digest required")
        if not re.fullmatch(r"/[A-Za-z0-9/_-]{0,127}", text(value["health_path"])):
            raise ValueError("bounded health route required")
        if scope.environment != Environment.PRODUCTION:
            if value["namespace"] != preview_namespace(scope):
                raise ValueError("nonproduction namespace must identify preview generation")
        elif value["phase"] != "release":
            raise ValueError("production requires release certification")
        if value["namespace"] == value["argo_namespace"] or value["namespace"] in {
            "default",
            "kube-system",
            "kube-public",
            "kube-node-lease",
        }:
            raise ValueError("dedicated application namespace required")
        if optional:
            from ..target_service.deployment import validate

            validate(value["reference_service"], dict(value))
        return cls(wire(value), scope)


@dataclass(frozen=True)
class DeliveryRequest:
    binding: Binding
    target: Target
    build: Evidence | None
    stages: dict[str, Evidence]
    verification: dict[str, Any] | None
    expires_at: datetime

    @classmethod
    def parse(cls, value: object) -> "DeliveryRequest":
        raw = envelope(value, {"binding", "build", "stages", "verification", "expires_at"})
        binding = Binding.parse(raw["binding"])
        if binding.action not in {"promote", "rollback", "cleanup"}:
            raise ValueError("delivery action required")
        target = Target.parse(binding.target)
        if (
            binding.action != "cleanup" and target.scope.revision != binding.scope.revision
        ) or not target.raw["image"].endswith("@" + binding.artifact_digest):
            raise ValueError("image or source revision substitution")
        stages = fields(
            raw["stages"],
            set() if binding.action == "cleanup" else {"implement", "review", "test", "qa"},
        )
        decoded = {r: evidence_from_wire(v) for r, v in stages.items()}
        build = evidence_from_wire(raw["build"]) if raw["build"] is not None else None
        if build is None and binding.action != "cleanup":
            raise ValueError("build provenance required")
        if any(
            e.scope != binding.scope for e in ([build] if build else []) + list(decoded.values())
        ):
            raise ValueError("build/stage evidence scope differs")
        verification = raw["verification"]
        if verification is not None:
            verification = fields(verification, {"input", "receipt"})
        elif target.raw["phase"] == "release" and binding.action != "cleanup":
            raise ValueError("release delivery requires final certification")
        return cls(
            binding,
            target,
            build,
            decoded,
            wire(verification) if verification is not None else None,
            timestamp(raw["expires_at"]),
        )

    @property
    def identity(self) -> str:
        return "sre-delivery-" + self.binding.identity

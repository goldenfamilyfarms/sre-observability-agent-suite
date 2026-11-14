"""Frozen build recipe, exact engineering chain and explicit registry destination."""

import re
from dataclasses import dataclass
from datetime import datetime

from ..api_wire import envelope, evidence_from_wire, fields, text
from ..contracts import Environment, Evidence
from ..control.contracts import Binding, sha, timestamp, wire
from ..delivery.helm import CHART_DIGEST
from ..execution.config import safe_path
from ..execution.wire import digest


def argv(value: object) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        raise ValueError("bounded argv required")
    for item in value:
        if any(ord(c) < 32 for c in text(item, 1024)):
            raise ValueError("control characters in argv")
    return list(value)


@dataclass(frozen=True)
class BuildRequest:
    binding: Binding
    stages: dict[str, Evidence]
    expires: datetime

    @classmethod
    def parse(cls, value: object) -> "BuildRequest":
        raw = envelope(value, {"binding", "stages", "expires_at"})
        binding = Binding.parse(raw["binding"])
        target = fields(
            binding.target,
            {
                "environment",
                "kind",
                "source_repository",
                "candidate_revision",
                "chart_revision",
                "chart_digest",
                "registry_repository",
                "recipe",
            },
        )
        if (
            binding.action != "execute"
            or target["kind"] != "image-build"
            or target["environment"] != str(binding.scope.environment)
            or binding.scope.environment == Environment.PRODUCTION
        ):
            raise ValueError("explicit nonproduction image-build authorization required")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", text(target["source_repository"])):
            raise ValueError("source repository required")
        for key in ("candidate_revision", "chart_revision"):
            if not re.fullmatch(r"[0-9a-f]{40}", text(target[key])):
                raise ValueError("full Git revision required")
        if target["chart_digest"] != CHART_DIGEST:
            raise ValueError("reviewed chart recipe required")
        # An explicit host avoids Docker Hub short-name and implicit namespace resolution.
        if not re.fullmatch(
            r"[a-z0-9][a-z0-9.-]+(?::[0-9]{1,5})?/[a-z0-9]+(?:[._/-][a-z0-9]+)*",
            text(target["registry_repository"], 240),
        ):
            raise ValueError("explicit registry host/repository required")
        recipe = fields(
            target["recipe"], {"base_image", "platform", "paths", "steps", "entrypoint"}
        )
        base = text(recipe["base_image"], 300)
        if "@" not in base or not re.fullmatch(r"[a-zA-Z0-9._/:-]+", base.split("@")[0]):
            raise ValueError("pinned base image required")
        sha(base.split("@", 1)[1])
        if recipe["platform"] not in {"linux/amd64", "linux/arm64"}:
            raise ValueError("one supported Linux platform required")
        paths = recipe["paths"]
        if (
            not isinstance(paths, list)
            or not 1 <= len(paths) <= 1000
            or len(set(paths)) != len(paths)
        ):
            raise ValueError("explicit unique context file list required")
        for path in paths:
            safe_path(text(path, 240))
            if path in {"Dockerfile", ".dockerignore"} or any(
                p.startswith(".") for p in path.split("/")
            ):
                raise ValueError("hidden files and Docker build overrides cannot enter context")
        if not isinstance(recipe["steps"], list) or len(recipe["steps"]) > 20:
            raise ValueError("bounded build steps required")
        for step in recipe["steps"]:
            argv(step)
        argv(recipe["entrypoint"])
        stages = {
            r: evidence_from_wire(e)
            for r, e in fields(raw["stages"], {"implement", "review", "test", "qa"}).items()
        }
        if any(e.scope != binding.scope for e in stages.values()):
            raise ValueError("stage scope differs")
        if binding.artifact_digest != "sha256:" + stages["implement"].sha256:
            raise ValueError("source artifact differs")
        return cls(binding, stages, timestamp(raw["expires_at"]))

    @property
    def identity(self) -> str:
        return "sre-image-" + digest(
            wire(
                {
                    "binding": self.binding.wire(),
                    "stages": {r: e.sha256 for r, e in self.stages.items()},
                    "expires": self.expires.isoformat(),
                }
            )
        )

"""Exact artifact/plan/source-scope binding for every governed external operation."""

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from ..api_wire import fields, text
from ..contracts import Scope, aware
from ..execution.wire import canonical, digest
from ..serialization import scope_from_dict
from ..telemetry.contracts import integer


def identifier(value: object) -> str:
    result = text(value, 128)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", result):
        raise ValueError("invalid identifier")
    return result


def sha(value: object) -> str:
    result = text(value, 71)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", result):
        raise ValueError("full SHA256 required")
    return result


def timestamp(value: object) -> datetime:
    result = datetime.fromisoformat(text(value, 64))
    aware(result)
    return result


def wire(value: object) -> dict[str, Any]:
    result = json.loads(canonical(value))
    if not isinstance(result, dict):
        raise ValueError("object required")
    return dict(result)


@dataclass(frozen=True)
class Binding:
    scope: Scope
    context_version: int
    artifact_digest: str
    plan_digest: str
    action: str
    target: dict[str, Any]

    @classmethod
    def parse(cls, value: object) -> "Binding":
        raw = fields(
            value,
            {"scope", "context_version", "artifact_digest", "plan_digest", "action", "target"},
        )
        action = text(raw["action"], 32)
        if action not in {
            "plan",
            "execute",
            "merge",
            "promote",
            "rollback",
            "cleanup",
            "remediate",
        }:
            raise ValueError("unsupported governed action")
        target = raw["target"]
        if not isinstance(target, dict) or not target or len(canonical(target).encode()) > 16384:
            raise ValueError("bounded explicit target required")
        if target.get("environment") not in {"development", "staging", "production"}:
            raise ValueError("explicit target environment required")
        return cls(
            scope_from_dict(raw["scope"]),
            integer(raw["context_version"], 0, 2**63 - 1),
            sha(raw["artifact_digest"]),
            sha(raw["plan_digest"]),
            action,
            wire(target),
        )

    def wire(self) -> dict[str, Any]:
        return wire(asdict(self))

    @property
    def identity(self) -> str:
        return digest(self.wire())

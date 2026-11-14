"""Explicit engineering role dispatch and independent authenticated evidence subjects."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..api_wire import envelope, fields, text
from ..contracts import Evidence
from ..execution.wire import digest
from ..fixtures.contracts import queue

ROLES = ("controller", "implement", "review", "test", "qa")


@dataclass(frozen=True)
class RolePolicy:
    path: Path
    bindings: dict[str, dict[str, str]]

    @classmethod
    def load(cls, path: Path) -> "RolePolicy":
        from ..verification.config import private_document

        raw = envelope(private_document(path), {"roles"})
        roles = fields(raw["roles"], set(ROLES))
        bindings = {}
        for role, value in roles.items():
            row = fields(value, {"subject", "queue"})
            subject = text(row["subject"], 128)
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", subject):
                raise ValueError("invalid role subject")
            bindings[role] = {"subject": subject, "queue": queue(row["queue"])}
        for name in ("subject", "queue"):
            if len({r[name] for r in bindings.values()}) != len(ROLES):
                raise ValueError("engineering roles require distinct subjects and queues")
        return cls(path, bindings)

    @property
    def fingerprint(self) -> str:
        return digest(self.bindings)

    def current(self) -> None:
        if self.load(self.path).fingerprint != self.fingerprint:
            raise ValueError("engineering role authorization changed")

    def dispatch(self) -> dict[str, Any]:
        return {
            "policy": self.fingerprint,
            "queues": {r: v["queue"] for r, v in self.bindings.items()},
        }

    def require(self, configured_role: str | None, operation: str) -> None:
        self.current()
        if configured_role != operation:
            raise ValueError("operation is not authorized for this worker role")

    def attest(self, role: str, evidence: Evidence) -> None:
        if role not in self.bindings or evidence.producer != self.bindings[role]["subject"]:
            raise ValueError("stage evidence producer is not the assigned independent role")

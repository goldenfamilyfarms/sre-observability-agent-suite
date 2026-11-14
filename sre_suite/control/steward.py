"""Evidence-driven steward: reconcile only explicit assertions from configured producers."""

import json
from dataclasses import asdict, dataclass
from typing import Any

from ..api_wire import evidence_from_wire, fields, text
from ..contracts import Scope
from ..execution.evidence_client import EvidenceClient
from ..execution.wire import digest
from .client import ControlClient
from .contracts import identifier


@dataclass(frozen=True)
class StewardRules:
    producers: frozenset[str]
    predicates: frozenset[str]
    maximum: int = 50

    def __post_init__(self) -> None:
        if not self.producers or not self.predicates or not 1 <= self.maximum <= 100:
            raise ValueError("bounded explicit steward rules required")
        for value in self.producers | self.predicates:
            identifier(value)


class KnowledgeSteward:
    def __init__(
        self, control: ControlClient, evidence: EvidenceClient, rules: StewardRules
    ) -> None:
        self.control, self.evidence, self.rules = control, evidence, rules

    def run(self, scope: Scope) -> dict[str, Any]:
        pending = self.control.call("pending", scope, {})
        rows = pending["proposals"]
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError("invalid pending response")
        results = []
        for entry in rows[: self.rules.maximum]:
            record = fields(entry, {"finding", "proposer"})
            finding = fields(
                record["finding"],
                {"id", "scope", "kind", "subject", "predicate", "value", "evidence"},
            )
            ref = evidence_from_wire(finding["evidence"])
            if ref.scope != scope:
                raise ValueError("cross-scope proposal")
            identity = identifier(finding["id"])
            if (
                ref.producer not in self.rules.producers
                or finding["predicate"] not in self.rules.predicates
            ):
                results.append({"finding_id": identity, "outcome": "needs_review"})
                continue
            document = json.loads(self.evidence.read(ref))
            assertions = document.get("assertions") if isinstance(document, dict) else None
            expected = {k: text(finding[k], 4000) for k in ("subject", "predicate", "value")}
            if (
                not isinstance(assertions, list)
                or len(assertions) > 200
                or expected not in assertions
            ):
                results.append({"finding_id": identity, "outcome": "unsubstantiated"})
                continue
            # A CAS conflict stops this run; fresh retries reconstruct the scoped snapshot.
            version = self.evidence.context(scope)["version"]
            result = self.control.call(
                "reconcile",
                scope,
                {
                    "finding_id": identity,
                    "decision": "verify",
                    "expected_version": version,
                    "supersedes": None,
                },
                digest([identity, ref.sha256, version, "verified"]),
            )
            results.append(result)
        return {
            "scope": asdict(scope),
            "results": results,
            "remaining": max(0, len(rows) - len(results)),
        }

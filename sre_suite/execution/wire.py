"""Strict runtime ticket decoding and canonical fingerprints."""

import hashlib
import json
from dataclasses import asdict
from dataclasses import fields as dataclass_fields
from typing import Any

from ..api_wire import envelope, evidence_from_wire, fields, text
from ..contracts import JiraExecutionTicket
from ..serialization import scope_from_dict


def canonical(value: object) -> str:
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def ticket_from_wire(value: object) -> JiraExecutionTicket:
    raw = fields(value, {f.name for f in dataclass_fields(JiraExecutionTicket)})
    values: dict[str, Any] = {}
    arrays = {
        "implementation_scope",
        "dependencies",
        "acceptance_criteria",
        "qa_requirements",
        "telemetry_requirements",
        "delivery_requirements",
    }
    for key, item in raw.items():
        if key == "scope":
            values[key] = scope_from_dict(item)
        elif key == "evidence":
            if not isinstance(item, list) or not 1 <= len(item) <= 16:
                raise ValueError("ticket requires 1..16 evidence records")
            values[key] = tuple(evidence_from_wire(e) for e in item)
        elif key in arrays:
            if not isinstance(item, list) or len(item) > 30:
                raise ValueError("invalid ticket array")
            values[key] = tuple(text(v, 2000) for v in item)
        else:
            values[key] = text(item, 4000)
    return JiraExecutionTicket(**values)


def ticket_envelope(value: object) -> JiraExecutionTicket:
    raw = envelope(value, {"ticket"})
    return ticket_from_wire(raw["ticket"])


def ticket_json(ticket: JiraExecutionTicket) -> dict[str, Any]:
    # JSON wire arrays, not dataclass tuples, are persisted in the execution manifest.
    result: dict[str, Any] = json.loads(canonical(asdict(ticket)))
    return result

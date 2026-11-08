"""Versioned identity codecs. Legacy scopes are never assigned an instance implicitly."""

import json
from dataclasses import asdict
from typing import cast

from .contracts import Environment, Scope


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def scope_from_dict(value: object) -> Scope:
    if not isinstance(value, dict) or set(value) != {
        "service",
        "environment",
        "revision",
        "environment_id",
        "generation",
    }:
        raise ValueError("scope requires exactly the v2 instance-scoped fields")
    fields = cast(dict[str, object], value)
    for field in ("service", "environment", "revision", "environment_id"):
        if not isinstance(fields[field], str):
            raise ValueError(f"{field} must be a string")
    if type(fields["generation"]) is not int:
        raise ValueError("generation must be an integer, not a boolean or coerced string")
    return Scope(
        cast(str, fields["service"]),
        Environment(cast(str, fields["environment"])),
        cast(str, fields["revision"]),
        cast(str, fields["environment_id"]),
        fields["generation"],
    )


def encode_scope(scope: Scope) -> str:
    return json.dumps({"schema_version": 2, "scope": asdict(scope)}, sort_keys=True)


def decode_scope(payload: str) -> Scope:
    raw = json.loads(payload, object_pairs_hook=_unique_fields)
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "scope"}:
        raise ValueError("invalid scope envelope")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 2:
        raise ValueError("unsupported scope schema version")
    return scope_from_dict(raw["scope"])

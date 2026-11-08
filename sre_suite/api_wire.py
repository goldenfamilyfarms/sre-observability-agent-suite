"""Strict, bounded JSON decoding at the untrusted API boundary."""

import base64
import binascii
import json
import re
from datetime import datetime
from typing import cast

from .contracts import Evidence
from .serialization import _unique_fields, scope_from_dict

MAX_BODY_BYTES = 1_500_000
MAX_RESPONSE_BYTES = 2_000_000


def fields(value: object, expected: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("unexpected or missing fields")
    return cast(dict[str, object], value)


def text(value: object, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("expected a bounded nonempty string")
    return value


def _constant(value: str) -> object:
    raise ValueError("non-finite JSON number")


def decode(body: bytes) -> object:
    if len(body) > MAX_BODY_BYTES:
        raise ValueError("body too large")
    raw: object = json.loads(
        body.decode("utf-8"), object_pairs_hook=_unique_fields, parse_constant=_constant
    )
    stack = [(raw, 0)]
    count = 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if depth > 16 or count > 10000:
            raise ValueError("JSON complexity exceeded")
        if isinstance(item, dict):
            stack.extend((value, depth + 1) for value in item.values())
        elif isinstance(item, list):
            stack.extend((value, depth + 1) for value in item)
    return raw


def envelope(value: object, names: set[str]) -> dict[str, object]:
    raw = fields(value, names | {"schema_version"})
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 2:
        raise ValueError("unsupported schema version")
    return raw


def evidence_from_wire(value: object, *, producer: str | None = None) -> Evidence:
    names = {"id", "scope", "uri", "sha256", "observed_at", "expires_at"}
    raw = fields(value, names if producer is not None else names | {"producer"})
    identity = text(raw["id"], 128)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", identity):
        raise ValueError("invalid evidence identity")
    scope = scope_from_dict(raw["scope"])
    text(scope.service, 128)
    return Evidence(
        identity,
        scope,
        text(raw["uri"], 2048),
        text(raw["sha256"], 64),
        datetime.fromisoformat(text(raw["observed_at"], 64)),
        datetime.fromisoformat(text(raw["expires_at"], 64)),
        producer if producer is not None else text(raw["producer"], 128),
    )


def content_from_wire(value: object) -> bytes:
    if not isinstance(value, str) or len(value) > 1_398_104:
        raise ValueError("invalid content size")
    try:
        content = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("invalid base64") from error
    if len(content) > 1_048_576 or base64.b64encode(content).decode("ascii") != value:
        raise ValueError("invalid content size or noncanonical encoding")
    return content

"""Authenticated existing API client; scoped grants are required for every operation."""

import base64
from dataclasses import asdict
from datetime import datetime
from typing import Any

from ..api_wire import evidence_from_wire
from ..contracts import Evidence, Scope
from .http import JsonHttp


class EvidenceClient:
    def __init__(
        self,
        url: str,
        token: str,
        http: JsonHttp | None = None,
        *,
        local_ca_file: str | None = None,
    ) -> None:
        self.http = http or JsonHttp(url, "Bearer " + token, local_ca_file=local_ca_file)

    def context(self, scope: Scope, expected_version: int | None = None) -> dict[str, Any]:
        result = self.http.call(
            "POST",
            "/v1/context",
            {
                "schema_version": 2,
                "scope": asdict(scope),
                "expected_version": expected_version,
            },
        )
        if (
            not isinstance(result, dict)
            or set(result) != {"schema_version", "scope", "version", "findings"}
            or result["schema_version"] != 2
            or type(result["version"]) is not int
            or not isinstance(result["findings"], list)
        ):
            raise ValueError("invalid context response")
        from ..serialization import scope_from_dict

        if scope_from_dict(result["scope"]) != scope:
            raise ValueError("context scope mismatch")
        return result

    def verify(self, evidence: Evidence) -> None:
        self.read(evidence)

    def read(self, evidence: Evidence) -> bytes:
        result = self.http.call(
            "POST", "/v1/evidence/read", {"schema_version": 2, "evidence": asdict(evidence)}
        )
        if not isinstance(result, dict) or evidence_from_wire(result.get("evidence")) != evidence:
            raise ValueError("evidence response mismatch")
        import hashlib

        from ..api_wire import content_from_wire

        content = content_from_wire(result.get("content_base64"))
        if hashlib.sha256(content).hexdigest() != evidence.sha256:
            raise ValueError("evidence response digest mismatch")
        return content

    def upload(
        self, key: str, scope: Scope, content: bytes, observed: datetime, expires: datetime
    ) -> Evidence:
        import hashlib

        metadata = {
            "id": key,
            "scope": asdict(scope),
            "uri": "urn:sre:execution:" + key,
            "sha256": hashlib.sha256(content).hexdigest(),
            "observed_at": observed.isoformat(),
            "expires_at": expires.isoformat(),
        }
        result = self.http.call(
            "POST",
            "/v1/evidence",
            {
                "schema_version": 2,
                "evidence": metadata,
                "content_base64": base64.b64encode(content).decode(),
            },
        )
        if not isinstance(result, dict):
            raise ValueError("invalid upload response")
        evidence = evidence_from_wire(result.get("evidence"))
        if (
            evidence.scope != scope
            or evidence.sha256 != metadata["sha256"]
            or evidence.observed_at != observed
            or evidence.expires_at != expires
        ):
            raise ValueError("upload response mismatch")
        return evidence

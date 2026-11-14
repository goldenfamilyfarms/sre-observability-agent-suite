"""No cached authority: every governed external effect must re-read the control service."""

from dataclasses import asdict
from typing import Any

from ..api_wire import envelope, fields
from ..contracts import Scope
from ..execution.http import JsonHttp
from ..execution.wire import digest
from ..serialization import scope_from_dict
from .contracts import Binding


class ControlClient:
    def __init__(self, http: JsonHttp) -> None:
        self.http = http

    def call(
        self, operation: str, scope: Scope, payload: dict[str, Any], operation_id: str | None = None
    ) -> dict[str, Any]:
        value = self.http.call(
            "POST",
            "/v1/control/" + operation,
            {
                "schema_version": 2,
                "scope": asdict(scope),
                "payload": payload,
                "operation_id": operation_id or digest([operation, asdict(scope), payload]),
            },
        )
        raw = envelope(value, {"scope", "result"})
        if scope_from_dict(raw["scope"]) != scope or not isinstance(raw["result"], dict):
            raise ValueError("control response scope mismatch")
        return dict(raw["result"])

    def require_approval(self, binding: Binding) -> dict[str, Any]:
        result = self.call("approval", binding.scope, {"binding": binding.wire()})
        if (
            result.get("active") is not True
            or result.get("binding") != binding.wire()
            or binding.target["environment"] == "production"
            and result.get("human") is not True
        ):
            raise ValueError("current exact-action approval required")
        return result

    def artifact(self, scope: Scope, key: str) -> dict[str, Any]:
        return dict(
            fields(self.call("artifact-get", scope, {"key": key}), {"evidence", "content_sha256"})
        )

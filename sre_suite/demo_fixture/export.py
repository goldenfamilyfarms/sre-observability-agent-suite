"""Explicit, bounded outbox delivery. OTLP/Loki delivery is at least once, never fabricated."""

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from ..api_wire import fields, text
from ..execution.http import JsonHttp
from ..execution.journal import Journal
from .controller import Controller


def transports(config: object, env: Mapping[str, str]) -> dict[str, JsonHttp]:
    result = {}
    for signal, raw in fields(config, {"traces", "logs"}).items():
        row = fields(raw, {"base_url", "authorization_env", "tenant_env", "ca_env"})
        name = text(row["authorization_env"], 80)
        expected = "SRE_DEMO_" + signal.upper() + "_AUTH"
        if name != expected:
            raise ValueError("use signal-specific demo export credentials")
        token = env[name]
        if not token or any(ord(c) < 32 or ord(c) > 126 for c in token):
            raise ValueError("invalid export credential")
        tenant = row["tenant_env"]
        if tenant not in (None, "SRE_DEMO_" + signal.upper() + "_TENANT"):
            raise ValueError("invalid export tenant variable")
        headers = {} if tenant is None else {"X-Scope-OrgID": text(env[str(tenant)], 256)}
        if any(ord(c) < 33 or ord(c) > 126 for v in headers.values() for c in v):
            raise ValueError("invalid export tenant")
        ca = row["ca_env"]
        if ca not in (None, "SRE_DEMO_EXPORT_CA_FILE"):
            raise ValueError("invalid export CA variable")
        base = text(row["base_url"])
        if urlsplit(base).path:
            raise ValueError("export requires an exact HTTPS origin")
        result[signal] = JsonHttp(
            base,
            token,
            headers=headers,
            timeout=10,
            local_ca_file=env[str(ca)] if ca else None,
            expected_status=200 if signal == "traces" else 204,
        )
    return result


def acknowledge(signal: str, value: object) -> None:
    if signal == "logs":
        if value is not None:
            raise ValueError("Loki push did not return an empty acknowledgement")
    elif value != {}:
        row = fields(value, {"partialSuccess"})
        partial = row["partialSuccess"]
        if not isinstance(partial, dict) or set(partial) - {"rejectedSpans", "errorMessage"}:
            raise ValueError("invalid OTLP acknowledgement")
        rejected = partial.get("rejectedSpans", "0")
        if (
            type(rejected) not in (int, str)
            or str(rejected) != "0"
            or partial.get("errorMessage", "") != ""
        ):
            raise ValueError("OTLP rejected or warned about telemetry")


def flush(controller: Controller, clients: dict[str, Any], journal: Journal) -> int:
    count = 0
    with journal.locked("demo-export", lambda: None):
        controller.guard()
        with controller.connect() as conn:
            pending = conn.execute(
                "SELECT key,signal,payload FROM outbox WHERE delivered=0 "
                "ORDER BY key,signal LIMIT 2"
            ).fetchall()
        for key, signal, payload in pending:
            from ..api_wire import decode

            controller.guard()
            path = "/v1/traces" if signal == "traces" else "/loki/api/v1/push"
            result = clients[signal].call("POST", path, decode(payload.encode()))
            acknowledge(signal, result)
            controller.guard()
            with controller.connect() as conn:
                conn.execute(
                    "UPDATE outbox SET delivered=1 WHERE key=? AND signal=? AND payload=?",
                    (key, signal, payload),
                )
            count += 1
    return count

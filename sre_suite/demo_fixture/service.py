"""Reviewed pure demo service. No IO, credentials, imports from a candidate, or plugins.

This completed target is separate from the intentionally incomplete pilot-log demo.
Its exact source bytes must occur in the authorized source-candidate recipe.
"""

import re

CAPABILITIES = {
    "http": True,
    "exceptions": True,
    "unicode": True,
    "grpc": False,
    "outbound_http": False,
    "database": False,
    "cache": False,
    "messaging": False,
    "async": False,
    "retries": False,
    "timeouts": False,
    "cancellation": False,
    "jvm": False,
}


def handle(method: str, path: str, trace_id: object) -> tuple[int, str, dict[str, object]]:
    """Execute the bounded HTTP semantics; never retain an unrecognized path/context."""
    event: dict[str, object] = {"event": "request_completed"}
    if method != "GET":
        status, route, body = 405, "unsupported", "method not allowed"
    elif path == "/health":
        status, route, body = 200, "/health", "ok"
    elif path == "/unicode":
        status, route, body = 200, "/unicode", "Hello, 世界 🌾"
    elif path == "/error":
        try:
            raise RuntimeError("controlled demo failure")
        except RuntimeError as error:
            status, route, body = 500, "/error", "controlled error"
            # Fixed safe exception details; no stack locals, request content or credentials.
            event.update(exception_type=type(error).__name__, exception_message=str(error))
    else:
        status, route, body = 404, "unknown", "not found"
    event.update(route=route, status=status)
    if (
        isinstance(trace_id, str)
        and re.fullmatch(r"[0-9a-f]{32}", trace_id)
        and int(trace_id, 16) != 0
    ):
        event["trace_id"] = trace_id
    return status, body, event

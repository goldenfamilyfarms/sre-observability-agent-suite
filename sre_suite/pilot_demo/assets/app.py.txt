"""Synthetic request handler. No listener, network calls, credentials or real traffic."""

import json


def handle_request(method: str, path: str, trace_id: object = None) -> tuple[int, str, str]:
    """Return status, response body and one JSON request-log event.

    trace_id is a synthetic request-context value, not a raw HTTP header.
    The pilot task is to add validated correlation without changing responses.
    """
    if method != "GET":
        status, route, body = 405, "unsupported", "method not allowed"
    elif path == "/health":
        status, route, body = 200, "/health", "ok"
    else:
        status, route, body = 404, "unknown", "not found"
    event: dict[str, object] = {"event": "request_completed", "route": route, "status": status}
    # PILOT GAP: validated trace_id correlation is not implemented yet.
    return status, body, json.dumps(event, sort_keys=True, separators=(",", ":"))

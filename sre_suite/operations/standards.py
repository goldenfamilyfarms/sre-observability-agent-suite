"""Versioned source indicators and Python AST citations, never a runtime guarantee."""

import ast
from typing import Any

from ..execution.wire import digest

PATTERNS = {
    "tracing": ("opentelemetry", "OpenTelemetry", "TracerProvider"),
    "metrics": ("prometheus_client", "MeterRegistry", "get_meter", "Micrometer"),
    "correlation": ("trace_id", "traceId", "LoggingInstrumentor"),
    "propagation": (
        "propagate.inject",
        "propagate.extract",
        "TextMapPropagator",
        "W3CTraceContextPropagator",
    ),
    "redaction": ("redact", "sanitize", "SensitiveDataFilter"),
    "tests": ("unittest", "pytest", "@Test", "describe("),
}
POLICY = digest({"version": "standards-ast-v1", "patterns": PATTERNS})


def inspect(contents: dict[str, str], inventory: dict[str, Any]) -> dict[str, Any]:
    entries = {e["path"]: e for e in inventory["files"]}
    controls: dict[str, list[dict[str, Any]]] = {name: [] for name in PATTERNS}
    calls = []
    parse_errors = []
    for path, content in contents.items():
        for line, text in enumerate(content.splitlines(), 1):
            for name, tokens in PATTERNS.items():
                if any(token in text for token in tokens):
                    controls[name].append(
                        {"path": path, "line": line, "sha256": entries[path]["sha256"]}
                    )
        if not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(content, filename=path)
        except (SyntaxError, ValueError, RecursionError):
            parse_errors.append(path)
            continue
        aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                aliases.update({a.asname or a.name: a.name for a in node.names})
            if isinstance(node, ast.ImportFrom) and node.module:
                aliases.update({a.asname or a.name: node.module + "." + a.name for a in node.names})
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                pieces = []
                expr = node.func
                while isinstance(expr, ast.Attribute):
                    pieces.append(expr.attr)
                    expr = expr.value
                if isinstance(expr, ast.Name):
                    pieces.append(aliases.get(expr.id, expr.id))
                    calls.append(
                        {
                            "path": path,
                            "line": node.lineno,
                            "sha256": entries[path]["sha256"],
                            "symbol": ".".join(reversed(pieces)),
                        }
                    )
        if len(calls) > 5000:
            raise ValueError("AST exceeds analysis budget")
    return {
        "policy_sha256": POLICY,
        "controls": {
            k: {"status": "observed_static" if v else "not_observed", "citations": v[:100]}
            for k, v in controls.items()
        },
        "calls": calls,
        "parse_errors": parse_errors,
        "runtime_verified": False,
    }

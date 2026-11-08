"""Pure dependency validation suitable for a dry-run Jira plan."""

from collections.abc import Sequence

from .contracts import JiraExecutionTicket


def execution_order(tickets: Sequence[JiraExecutionTicket]) -> tuple[str, ...]:
    by_key = {ticket.key: ticket for ticket in tickets}
    if len(by_key) != len(tickets):
        raise ValueError("duplicate ticket keys")
    remaining = {key: set(ticket.dependencies) for key, ticket in by_key.items()}
    if any(dep not in by_key for deps in remaining.values() for dep in deps):
        raise ValueError("unresolved dependency; include its ticket in the plan")
    ordered: list[str] = []
    while remaining:
        ready = sorted(key for key, deps in remaining.items() if not deps)
        if not ready:
            raise ValueError("dependency cycle")
        ordered.extend(ready)
        for key in ready:
            del remaining[key]
        for deps in remaining.values():
            deps.difference_update(ready)
    return tuple(ordered)

"""Immutable planned IDs resolve to Jira keys only after issue creation."""

import re
from dataclasses import asdict, dataclass, replace
from typing import Any

from ..api_wire import envelope, fields, text
from ..contracts import Environment, JiraExecutionTicket, Scope
from ..execution.wire import canonical, digest, ticket_from_wire, ticket_json
from ..planning import execution_order
from ..serialization import scope_from_dict


@dataclass(frozen=True)
class Node:
    id: str
    summary: str
    depends_on: tuple[str, ...]
    template: JiraExecutionTicket


@dataclass(frozen=True)
class Plan:
    id: str
    project: str
    repository: str
    scope: Scope
    nodes: tuple[Node, ...]

    @classmethod
    def parse(cls, value: object) -> "Plan":
        raw = envelope(value, {"id", "project", "repository", "scope", "nodes"})
        identity, project, repository = (text(raw[k], 128) for k in ("id", "project", "repository"))
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", identity) or not re.fullmatch(
            r"[A-Z][A-Z0-9]{1,30}", project
        ):
            raise ValueError("invalid plan or project ID")
        scope = scope_from_dict(raw["scope"])
        if scope.environment is Environment.PRODUCTION or not re.fullmatch(
            r"preview-r[1-9][0-9]*-pr[1-9][0-9]*", scope.environment_id
        ):
            raise ValueError("planning requires an authorized non-production preview")
        values = raw["nodes"]
        if not isinstance(values, list) or not 1 <= len(values) <= 20:
            raise ValueError("plan requires 1..20 nodes")
        nodes = []
        for index, value in enumerate(values, 1):
            item = fields(value, {"id", "summary", "depends_on", "requirements"})
            name, summary = text(item["id"], 64), text(item["summary"], 200)
            deps = item["depends_on"]
            if (
                not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name)
                or any(ord(c) < 32 for c in summary)
                or not isinstance(deps, list)
                or len(deps) > 19
                or any(not isinstance(dep, str) for dep in deps)
                or len(set(deps)) != len(deps)
                or name in deps
            ):
                raise ValueError("invalid planned task")
            requirements = item["requirements"]
            if not isinstance(requirements, dict) or set(requirements) & {
                "key",
                "scope",
                "repository",
                "dependencies",
            }:
                raise ValueError("requirements cannot supply assigned identity")
            ticket = ticket_from_wire(
                {
                    **requirements,
                    "key": f"{project}-{index}",
                    "scope": asdict(scope),
                    "repository": repository,
                    "dependencies": [],
                }
            )
            nodes.append(Node(name, summary, tuple(deps), ticket))
        plan = cls(identity, project, repository, scope, tuple(nodes))
        plan.order()
        if len(canonical(plan.json()).encode()) > 100_000:
            raise ValueError("plan too large")
        return plan

    def order(self) -> tuple[Node, ...]:
        by_id = {n.id: n for n in self.nodes}
        if len(by_id) != len(self.nodes) or any(
            d not in by_id for n in self.nodes for d in n.depends_on
        ):
            raise ValueError("duplicate or unresolved planned ID")
        tickets = tuple(
            replace(n.template, dependencies=tuple(by_id[d].template.key for d in n.depends_on))
            for n in self.nodes
        )
        by_key = {n.template.key: n for n in self.nodes}
        return tuple(by_key[key] for key in execution_order(tickets))

    def ticket(self, node: Node, keys: dict[str, str]) -> JiraExecutionTicket:
        for key in keys.values():
            if not re.fullmatch(re.escape(self.project) + r"-[1-9][0-9]*", key):
                raise ValueError("assigned key outside project")
        if len(set(keys.values())) != len(keys):
            raise ValueError("assigned issue keys must be unique")
        return replace(
            node.template, key=keys[node.id], dependencies=tuple(keys[d] for d in node.depends_on)
        )

    def json(self) -> dict[str, Any]:
        nodes = []
        for node in self.nodes:
            requirements = ticket_json(node.template)
            for name in ("key", "scope", "repository", "dependencies"):
                del requirements[name]
            nodes.append(
                {
                    "id": node.id,
                    "summary": node.summary,
                    "depends_on": list(node.depends_on),
                    "requirements": requirements,
                }
            )
        return {
            "schema_version": 2,
            "id": self.id,
            "project": self.project,
            "repository": self.repository,
            "scope": asdict(self.scope),
            "nodes": nodes,
        }

    @property
    def fingerprint(self) -> str:
        return digest(self.json())

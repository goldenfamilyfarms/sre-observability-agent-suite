"""Retry-safe service epic creation under an optional initiative; no execution ticket contract."""

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from ..api_wire import decode, fields, text
from ..control.contracts import Binding
from ..execution.pilot import _read
from ..execution.wire import digest
from .client import Planner, obj, save, state_file


class EpicAgent:
    def __init__(self, planner: Planner, binding: Binding) -> None:
        self.planner, self.binding = planner, binding
        target = fields(
            binding.target,
            {
                "environment",
                "url",
                "project",
                "repository",
                "issue_type",
                "summary",
                "parent_issue",
                "epic_name_field",
            },
        )
        if (
            binding.action != "plan"
            or target["url"] != planner.config.url
            or target["project"] != planner.config.project
            or target["repository"] != planner.config.repository
            or target["environment"] != str(binding.scope.environment)
            or planner.control is None
        ):
            raise ValueError("exact authenticated epic authorization required")
        if not re.fullmatch(r"[1-9][0-9]{0,19}", text(target["issue_type"])):
            raise ValueError("explicit epic issue type required")
        summary = text(target["summary"], 200)
        if any(ord(c) < 32 for c in summary):
            raise ValueError("single-line epic summary required")
        if target["parent_issue"] is not None:
            planner.key(target["parent_issue"])
        field = target["epic_name_field"]
        if field is not None and not re.fullmatch(r"customfield_[1-9][0-9]*", text(field)):
            raise ValueError("invalid optional legacy epic name field")
        self.target = dict(binding.target)
        self.label = "sre-epic-" + binding.identity

    def guard(self, require_approval: bool = True) -> None:
        if self.planner.evidence is None or self.planner.control is None:
            raise ValueError("authenticated control/evidence clients required")
        self.planner.evidence.context(self.binding.scope, self.binding.context_version)
        if require_approval:
            self.planner.control.require_approval(self.binding)
        parent = self.target["parent_issue"]
        if parent:
            issue = obj(
                self.planner.http.call(
                    "GET", f"/rest/api/3/issue/{parent}?fields=project,issuetype"
                )
            )
            if (
                issue.get("key") != parent
                or obj(obj(issue.get("fields")).get("project")).get("key")
                != self.planner.config.project
                or obj(obj(issue.get("fields")).get("issuetype")).get("hierarchyLevel") != 2
            ):
                raise ValueError("initiative parent identity or hierarchy differs")

    def document(self) -> dict[str, Any]:
        target = self.target
        values: dict[str, Any] = {
            "project": {"key": target["project"]},
            "issuetype": {"id": target["issue_type"]},
            "summary": target["summary"],
            "labels": [self.label],
        }
        if target["parent_issue"]:
            values["parent"] = {"key": target["parent_issue"]}
        if target["epic_name_field"]:
            values[target["epic_name_field"]] = target["summary"]
        return values

    def inspect(self) -> dict[str, Any]:
        self.guard(False)
        base = f"/rest/api/3/issue/createmeta/{self.target['project']}/issuetypes"
        types = self.planner.pages(base, "issueTypes")
        selected = [t for t in types if t.get("id") == self.target["issue_type"]]
        if len(selected) != 1 or selected[0].get("subtask") is not False:
            raise ValueError("configured epic type is unavailable")
        details = obj(
            self.planner.http.call("GET", "/rest/api/3/issuetype/" + self.target["issue_type"])
        )
        if details.get("id") != self.target["issue_type"] or details.get("hierarchyLevel") != 1:
            raise ValueError("configured type must be an epic at hierarchy level one")
        rows = self.planner.pages(base + "/" + self.target["issue_type"], "fields")
        metadata = {text(r.get("fieldId")): r for r in rows}
        document = self.document()
        if len(metadata) != len(rows) or not set(document) <= metadata.keys():
            raise ValueError("required epic fields unavailable")
        for name, row in metadata.items():
            if row.get("required") is True and name not in document:
                raise ValueError("unsupported required epic field")
            if (
                name in document
                and name not in {"project", "issuetype"}
                and "set" not in row.get("operations", [])
            ):
                raise ValueError("epic field cannot be set")
        self.guard(False)
        return {"binding": self.binding.wire(), "fields": document, "writes": False}

    def verify(self, key: str) -> None:
        self.guard()
        key = self.planner.key(key)
        issue = obj(
            self.planner.http.call(
                "GET", "/rest/api/3/issue/" + key + "?fields=" + ",".join(self.document())
            )
        )
        current = obj(issue.get("fields"))
        for name, expected in self.document().items():
            actual = current.get(name)
            if isinstance(expected, dict):
                if any(obj(actual).get(k) != v for k, v in expected.items()):
                    raise ValueError("epic identity differs")
            elif actual != expected:
                raise ValueError("epic identity differs")
        marker = obj(
            self.planner.http.call("GET", "/rest/api/3/issue/" + key + "/properties/sre.epic.v1")
        )
        if issue.get("key") != key or marker.get("value") != {
            "binding_sha256": self.binding.identity
        }:
            raise ValueError("epic provenance differs")
        self.guard()

    def publish(self, journal: Path) -> dict[str, Any]:
        if not self.planner.config.enabled:
            raise ValueError("planning writes disabled")
        self.inspect()
        with state_file(journal, "epic-" + self.binding.identity) as path:
            state: dict[str, Any] | None = None
            if path.exists():
                stored = obj(decode(_read(path, 100000)))
                state = obj(stored.get("state"))
                if (
                    stored.get("checksum") != digest(state)
                    or set(state) != {"binding", "key"}
                    or state["binding"] != self.binding.identity
                ):
                    raise ValueError("epic journal changed")
            key = state["key"] if state else None
            if key is None:
                self.guard()
                query = urlencode(
                    {
                        "jql": f'project = "{self.target["project"]}" AND labels = "{self.label}"',
                        "fields": "key",
                        "maxResults": 2,
                    }
                )
                found = obj(self.planner.http.call("GET", "/rest/api/3/search/jql?" + query))
                rows = found.get("issues")
                if (
                    not isinstance(rows, list)
                    or len(rows) > 1
                    or found.get("isLast") is not True
                    or found.get("nextPageToken")
                ):
                    raise ValueError("ambiguous epic reconciliation")
                if rows:
                    key = self.planner.key(obj(rows[0]).get("key"))
                elif state is not None:
                    raise ValueError("uncertain epic create; reconcile without repeating POST")
                else:
                    save(path, {"binding": self.binding.identity, "key": None})
                    self.guard()
                    created = obj(
                        self.planner.http.call(
                            "POST",
                            "/rest/api/3/issue",
                            {
                                "fields": self.document(),
                                "properties": [
                                    {
                                        "key": "sre.epic.v1",
                                        "value": {"binding_sha256": self.binding.identity},
                                    }
                                ],
                            },
                        )
                    )
                    key = self.planner.key(created.get("key"))
                save(path, {"binding": self.binding.identity, "key": key})
            self.verify(key)
            return {
                "schema_version": 2,
                "outcome": "epic_prepared",
                "key": key,
                "binding_sha256": self.binding.identity,
                "execution_started": False,
            }

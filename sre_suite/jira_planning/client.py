"""Project-scoped reads and guarded writes; uncertain creates are never repeated."""

import base64
import fcntl
import os
import re
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from ..api_wire import decode
from ..execution.config import flag, https_url, required
from ..execution.evidence_client import EvidenceClient
from ..execution.http import JsonHttp
from ..execution.jira_text import document, plain
from ..execution.pilot import _private, _read, _sync_directory
from ..execution.wire import canonical, digest, ticket_json
from .contracts import Node, Plan

MARKER = "sre.plan.v1"
TEXTAREA = "com.atlassian.jira.plugin.system.customfieldtypes:textarea"


def obj(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("invalid Jira response")
    return value


@dataclass(frozen=True)
class Config:
    url: str
    project: str
    repository: str
    issue_type: str
    contract_field: str
    enabled: bool

    @classmethod
    def load(cls, env: Mapping[str, str]) -> "Config":
        result = cls(
            https_url(required(env, "SRE_JIRA_BASE_URL")),
            required(env, "SRE_JIRA_PROJECT"),
            required(env, "SRE_REPOSITORY"),
            required(env, "SRE_JIRA_ISSUE_TYPE_ID"),
            required(env, "SRE_JIRA_CONTRACT_FIELD"),
            flag(env, "SRE_JIRA_PLANNING_WRITES_ENABLED"),
        )
        if (
            not re.fullmatch(r"https://[A-Za-z0-9.-]+", result.url)
            or not re.fullmatch(r"[A-Z][A-Z0-9]{1,30}", result.project)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", result.repository)
            or not re.fullmatch(r"[1-9][0-9]{0,19}", result.issue_type)
            or not re.fullmatch(r"customfield_[1-9][0-9]{0,19}", result.contract_field)
        ):
            raise ValueError("invalid planning configuration")
        return result

    def identity(self) -> dict[str, str]:
        return {
            "url": self.url,
            "project": self.project,
            "repository": self.repository,
            "issue_type": self.issue_type,
            "contract_field": self.contract_field,
        }


@contextmanager
def state_file(root: Path, identity: str) -> Iterator[Path]:
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("journal must use a physical absolute path")
    for path in (root, *root.parents):
        if path.is_symlink() or (path / ".git").exists():
            raise ValueError("journal must be private and outside Git")
    _private(root, directory=True)
    lock = root / (identity + ".lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "rb") as stream:
        _private(lock, directory=False)
        deadline = time.monotonic() + 30
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError("another publisher holds this plan") from None
                time.sleep(0.1)
        try:
            yield root / (identity + ".json")
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def save(path: Path, value: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".plan-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(canonical({"state": value, "checksum": digest(value)}).encode())
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
            _sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)


class Planner:
    def __init__(
        self,
        config: Config,
        http: JsonHttp,
        evidence: EvidenceClient | None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.config, self.http, self.evidence, self.clock = config, http, evidence, clock

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, discovery_only: bool = False) -> "Planner":
        config = Config.load(env)
        auth = base64.b64encode(
            (required(env, "SRE_JIRA_EMAIL") + ":" + required(env, "SRE_JIRA_API_TOKEN")).encode()
        ).decode()
        return cls(
            config,
            JsonHttp(config.url, "Basic " + auth),
            None
            if discovery_only
            else EvidenceClient(
                required(env, "SRE_EVIDENCE_API_URL"),
                required(env, "SRE_EVIDENCE_API_TOKEN"),
                local_ca_file=env.get("SRE_EVIDENCE_CA_FILE"),
            ),
        )

    def pages(self, path: str, field: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        start = 0
        for _ in range(20):
            response = obj(self.http.call("GET", path + f"?startAt={start}&maxResults=50"))
            values = response.get(field)
            total = response.get("total")
            if (
                not isinstance(values, list)
                or len(values) > 50
                or type(total) is not int
                or not 0 <= total <= 1000
                or response.get("startAt") != start
            ):
                raise ValueError("invalid metadata pagination")
            result.extend(obj(item) for item in values)
            start += len(values)
            if start == total:
                return result
            if not values or start > total:
                break
        raise ValueError("incomplete metadata pagination")

    def discover(self) -> dict[str, object]:
        c = self.config
        project = obj(self.http.call("GET", f"/rest/api/3/project/{c.project}"))
        if project.get("key") != c.project or project.get("archived") is True:
            raise ValueError("project unavailable")
        permissions = obj(
            obj(
                self.http.call(
                    "GET",
                    "/rest/api/3/mypermissions?"
                    + urlencode(
                        {
                            "projectKey": c.project,
                            "permissions": "BROWSE_PROJECTS,CREATE_ISSUES,EDIT_ISSUES",
                        }
                    ),
                )
            ).get("permissions")
        )
        for name in ("BROWSE_PROJECTS", "CREATE_ISSUES", "EDIT_ISSUES"):
            if obj(permissions.get(name)).get("havePermission") is not True:
                raise ValueError("planning permission unavailable")
        base = f"/rest/api/3/issue/createmeta/{c.project}/issuetypes"
        types = self.pages(base, "issueTypes")
        matches = [item for item in types if item.get("id") == c.issue_type]
        if len(matches) != 1 or matches[0].get("subtask") is not False:
            raise ValueError("configured standalone issue type unavailable")
        rows = self.pages(base + "/" + c.issue_type, "fields")
        from ..api_wire import text

        fields = {text(item.get("fieldId"), 128): item for item in rows}
        if (
            len(fields) != len(rows)
            or not {"project", "issuetype", "summary", "labels", c.contract_field} <= fields.keys()
        ):
            raise ValueError("required create fields unavailable")
        for name, item in fields.items():
            if item.get("required") is True and name not in {"project", "issuetype", "summary"}:
                raise ValueError("unsupported required create field")
        for name in ("summary", "labels", c.contract_field):
            if "set" not in fields[name].get("operations", []):
                raise ValueError("field cannot be set")
        if obj(fields[c.contract_field].get("schema")).get("custom") != TEXTAREA:
            raise ValueError("planning requires a multiline Jira text field")
        if fields[c.contract_field].get("hasDefaultValue") is True:
            raise ValueError("contract field must initially be empty")
        return {
            "project": c.project,
            "issue_type": c.issue_type,
            "contract_field": c.contract_field,
            "contract_format": "plain-adf",
            "required_fields": ["project", "issuetype", "summary"],
        }

    def guard(self, plan: Plan, version: int | None = None) -> int:
        if self.evidence is None:
            raise ValueError("authenticated evidence configuration required")
        if plan.project != self.config.project or plan.repository != self.config.repository:
            raise ValueError("plan outside configured project/repository")
        context = self.evidence.context(plan.scope, version)
        for node in plan.nodes:
            for evidence in node.template.evidence:
                if not evidence.valid_for(plan.scope, self.clock()):
                    raise ValueError("stale plan evidence")
                self.evidence.verify(evidence)
        # Recheck after potentially many evidence reads.
        return int(self.evidence.context(plan.scope, context["version"])["version"])

    def approval(self, plan: Plan) -> str:
        return digest({"target": self.config.identity(), "plan": plan.json()})

    def dry_run(self, plan: Plan) -> dict[str, Any]:
        plan = Plan.parse(plan.json())
        version = self.guard(plan)
        discovery = self.discover()
        # Verify the eventual contract fits the Jira transport before any writes.
        keys = {
            n.id: self.config.project + "-" + "9" * 20 + str(i) for i, n in enumerate(plan.nodes)
        }
        for node in plan.nodes:
            document(
                canonical({"schema_version": 2, "ticket": ticket_json(plan.ticket(node, keys))})
            )
        self.guard(plan, version)
        return {
            "schema_version": 2,
            "read_only": True,
            "approval_digest": self.approval(plan),
            "context_version": version,
            "target": self.config.identity(),
            "discovery": discovery,
            "order": [n.id for n in plan.order()],
            "plan": plan.json(),
            "unverified": [
                "issue creation and edit permission scopes",
                "Jira automation",
                "live execution",
            ],
        }

    def marker(self, plan: Plan, node: Node) -> dict[str, str]:
        return {"plan_id": plan.id, "node_id": node.id, "approval_digest": self.approval(plan)}

    def label(self, plan: Plan, node: Node) -> str:
        return "sre-plan-" + digest([self.config.identity(), plan.id, node.id])

    def search(self, plan: Plan, node: Node) -> str | None:
        query = urlencode(
            {
                "jql": f'project = "{plan.project}" AND labels = "{self.label(plan, node)}"',
                "fields": "key",
                "maxResults": 2,
            }
        )
        result = obj(self.http.call("GET", "/rest/api/3/search/jql?" + query))
        issues = result.get("issues")
        if (
            not isinstance(issues, list)
            or len(issues) > 1
            or result.get("isLast") is not True
            or result.get("nextPageToken")
        ):
            raise ValueError("ambiguous issue reconciliation")
        return self.key(obj(issues[0]).get("key")) if issues else None

    def key(self, value: object) -> str:
        if not isinstance(value, str) or not re.fullmatch(
            re.escape(self.config.project) + r"-[1-9][0-9]*", value
        ):
            raise ValueError("issue identity outside project")
        return value

    def issue(
        self, plan: Plan, node: Node, key: str, *, allow_completed: bool = False
    ) -> dict[str, Any]:
        key = self.key(key)
        field = self.config.contract_field
        issue = obj(
            self.http.call(
                "GET",
                f"/rest/api/3/issue/{key}?fields=project,issuetype,summary,labels,status,{field}",
            )
        )
        fields = obj(issue.get("fields"))
        marker = obj(self.http.call("GET", f"/rest/api/3/issue/{key}/properties/{MARKER}"))
        if (
            issue.get("key") != key
            or obj(fields.get("project")).get("key") != plan.project
            or obj(fields.get("issuetype")).get("id") != self.config.issue_type
            or fields.get("summary") != node.summary
            or fields.get("labels") != [self.label(plan, node)]
            or obj(obj(fields.get("status")).get("statusCategory")).get("key")
            not in (
                {"new", "indeterminate", "done"} if allow_completed else {"new", "indeterminate"}
            )
            or marker.get("value") != self.marker(plan, node)
        ):
            raise ValueError("existing issue differs from planned identity")
        return fields

    def publish(self, plan: Plan, approved: str, journal: Path) -> dict[str, object]:
        if not self.config.enabled or approved != self.approval(plan):
            raise ValueError("explicit matching plan approval and write enable flag required")
        report = self.dry_run(plan)
        identity = digest([self.config.url, self.config.project, plan.id])
        with state_file(journal, identity) as path:
            state: dict[str, Any] = {
                "approval": approved,
                "context_version": report["context_version"],
                "nodes": {},
            }
            if path.exists():
                _private(path, directory=False)
                stored = obj(decode(_read(path, 100_000)))
                state = obj(stored.get("state"))
                if (
                    stored.get("checksum") != digest(state)
                    or set(state) != {"approval", "context_version", "nodes"}
                    or state["approval"] != approved
                    or type(state["context_version"]) is not int
                    or state["context_version"] != report["context_version"]
                ):
                    raise ValueError("plan journal conflict")
            nodes = obj(state["nodes"])
            if not set(nodes) <= {n.id for n in plan.nodes}:
                raise ValueError("unknown journal node")
            keys: dict[str, str] = {}

            def guard() -> None:
                self.guard(plan, state["context_version"])

            guard()
            for node in plan.order():
                record = nodes.get(node.id)
                if record is not None and (
                    not isinstance(record, dict)
                    or set(record) != {"key", "ready"}
                    or type(record["ready"]) is not bool
                ):
                    raise ValueError("invalid journal node")
                key = record["key"] if record is not None else None
                if key is None:
                    key = self.search(plan, node)
                    if key is None and record is not None:
                        raise ValueError(
                            "uncertain create; inspect Jira and retry reconciliation, "
                            "never recreate"
                        )
                    if key is None:
                        guard()
                        nodes[node.id] = {"key": None, "ready": False}
                        save(path, state)  # Durable intent BEFORE the non-idempotent POST.
                        guard()
                        result = obj(
                            self.http.call(
                                "POST",
                                "/rest/api/3/issue",
                                {
                                    "fields": {
                                        "project": {"key": plan.project},
                                        "issuetype": {"id": self.config.issue_type},
                                        "summary": node.summary,
                                        "labels": [self.label(plan, node)],
                                    },
                                    "properties": [
                                        {"key": MARKER, "value": self.marker(plan, node)}
                                    ],
                                },
                            )
                        )
                        key = self.key(result.get("key"))
                    # Persist acknowledged/reconciled identity before any further remote work.
                    nodes[node.id] = {"key": key, "ready": False}
                    save(path, state)
                key = self.key(key)
                keys[node.id] = key
                guard()
                current = self.issue(plan, node, key)
                contract = canonical(
                    {"schema_version": 2, "ticket": ticket_json(plan.ticket(node, keys))}
                )
                old = current.get(self.config.contract_field)
                if old is None:
                    if nodes[node.id]["ready"]:
                        raise ValueError("completed contract was removed")
                    edit = obj(
                        obj(self.http.call("GET", f"/rest/api/3/issue/{key}/editmeta")).get(
                            "fields"
                        )
                    )
                    metadata = obj(edit.get(self.config.contract_field))
                    if (
                        "set" not in metadata.get("operations", [])
                        or obj(metadata.get("schema")).get("custom") != TEXTAREA
                    ):
                        raise ValueError("contract field is not editable")
                    guard()
                    # The optional field stays empty until the real key is known.
                    if self.issue(plan, node, key).get(self.config.contract_field) is not None:
                        raise ValueError("contract changed before initialization")
                    guard()
                    self.http.call(
                        "PUT",
                        f"/rest/api/3/issue/{key}",
                        {"fields": {self.config.contract_field: document(contract)}},
                    )
                elif plain(old) != contract:
                    raise ValueError("existing contract differs; never overwrite")
                guard()
                if plain(self.issue(plan, node, key).get(self.config.contract_field)) != contract:
                    raise ValueError("contract write not verified")
                nodes[node.id]["ready"] = True
                save(path, state)
            for node in plan.order():
                contract = canonical(
                    {"schema_version": 2, "ticket": ticket_json(plan.ticket(node, keys))}
                )
                if (
                    plain(self.issue(plan, node, keys[node.id]).get(self.config.contract_field))
                    != contract
                ):
                    raise ValueError("contract changed before final verification")
            guard()
            return {
                "schema_version": 2,
                "approval_digest": approved,
                "issues": keys,
                "execution_started": False,
                "outcome": "tickets_prepared",
            }

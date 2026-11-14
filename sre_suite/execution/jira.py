"""Jira Cloud ticket intake and opt-in idempotent execution-property writeback."""

import base64
import re
from collections.abc import Callable
from typing import Any

from ..api_wire import decode
from ..contracts import JiraExecutionTicket
from .config import Settings
from .http import JsonHttp, RemoteError
from .jira_text import plain
from .wire import digest, ticket_envelope


class JiraClient:
    def __init__(self, settings: Settings, http: JsonHttp | None = None) -> None:
        self.settings = settings
        auth = base64.b64encode(f"{settings.jira_email}:{settings.jira_token}".encode()).decode()
        self.http = http or JsonHttp(settings.jira_url, "Basic " + auth)

    def _key(self, key: str) -> str:
        if not re.fullmatch(re.escape(self.settings.jira_project) + r"-[1-9][0-9]*", key):
            raise ValueError("ticket outside the authorized Jira project")
        return key

    def get_ticket(
        self, key: str, heartbeat: Callable[[], None] = lambda: None
    ) -> JiraExecutionTicket:
        key = self._key(key)
        field_name = self.settings.jira_contract_field
        response = self.http.call("GET", f"/rest/api/3/issue/{key}?fields={field_name},status")
        if not isinstance(response, dict) or response.get("key") != key:
            raise ValueError("Jira issue identity mismatch")
        raw_fields = response.get("fields")
        if not isinstance(raw_fields, dict):
            raise ValueError("Jira contract field must contain a JSON text envelope")
        ticket = ticket_envelope(decode(plain(raw_fields.get(field_name)).encode()))
        if ticket.key != key or ticket.repository != self.settings.repository:
            raise ValueError("ticket key or repository is not authorized")
        for dependency in ticket.dependencies:
            heartbeat()
            dependency = self._key(dependency)
            item = self.http.call("GET", f"/rest/api/3/issue/{dependency}?fields=status")
            if (
                not isinstance(item, dict)
                or item.get("key") != dependency
                or item.get("fields", {}).get("status", {}).get("name")
                not in self.settings.jira_done_statuses
            ):
                raise ValueError("ticket dependency is not accepted")
        return ticket

    def publish(
        self,
        key: str,
        execution_id: str,
        report: dict[str, object],
        guard: Callable[[], None] = lambda: None,
    ) -> bool:
        key = self._key(key)
        if not self.settings.jira_writes:
            return False
        path = f"/rest/api/3/issue/{key}/properties/sre.run.{digest(execution_id)}"
        try:
            old = self.http.call("GET", path)
        except RemoteError as error:
            if error.status != 404:
                raise
        else:
            if not isinstance(old, dict) or old.get("value") != report:
                raise ValueError("existing execution property has different content")
            return True
        # PUT is repeatable with the same deterministic report after a lost response.
        guard()
        self.http.call("PUT", path, report)
        guard()
        return True

    def transition(
        self,
        key: str,
        execution_id: str,
        manifest: str,
        scope_digest: str,
        milestone: str,
        guard: Callable[[], None],
        heartbeat: Callable[[], None] = lambda: None,
    ) -> bool:
        def call(method: str, path: str, payload: object = None) -> Any:
            heartbeat()
            return self.http.call(method, path, payload)

        rules = {rule.milestone: rule for rule in self.settings.jira_transitions}
        if not rules:
            return False
        if not self.settings.jira_writes or milestone not in rules:
            raise ValueError("Jira transition not authorized")
        key = self._key(key)
        rule = rules[milestone]
        marker_key = "sre.transition." + digest([execution_id, milestone])
        marker = {
            "execution_id": execution_id,
            "manifest": manifest,
            "scope_digest": scope_digest,
            "milestone": milestone,
            "transition_id": rule.transition_id,
            "status_id": rule.status_id,
            "from_status_ids": list(rule.from_status_ids),
        }
        path = f"/rest/api/3/issue/{key}"

        def current_status() -> str:
            issue = call("GET", path + "?fields=status")
            if not isinstance(issue, dict) or issue.get("key") != key:
                raise ValueError("Jira status identity mismatch")
            status = issue.get("fields", {}).get("status", {})
            if status.get("statusCategory", {}).get("key") not in {"new", "indeterminate"}:
                raise ValueError("terminal/unknown Jira status cannot be changed")
            return str(status.get("id", ""))

        guard()
        try:
            old = call("GET", path + "/properties/" + marker_key)
        except RemoteError as error:
            if error.status != 404:
                raise
        else:
            if not isinstance(old, dict) or old.get("value") != marker:
                raise ValueError("Jira transition marker conflict")
            if current_status() != rule.status_id:
                raise ValueError("Jira status changed after completed transition")
            guard()
            return True
        if current_status() not in rule.from_status_ids:
            raise ValueError("Jira source status not authorized")
        available = call("GET", path + "/transitions?expand=transitions.fields")
        if not isinstance(available, dict) or not isinstance(available.get("transitions"), list):
            raise ValueError("invalid Jira transitions response")
        matches = [
            t
            for t in available["transitions"]
            if isinstance(t, dict) and t.get("id") == rule.transition_id
        ]
        if len(matches) != 1:
            raise ValueError("configured Jira transition unavailable")
        transition = matches[0]
        target = transition.get("to", {})
        if (
            transition.get("isAvailable") is False
            or target.get("id") != rule.status_id
            or target.get("statusCategory", {}).get("key") not in {"new", "indeterminate"}
            or any(
                f.get("required") and not f.get("hasDefaultValue")
                for f in transition.get("fields", {}).values()
            )
        ):
            raise ValueError("unsafe Jira destination or required transition fields")
        guard()
        if current_status() not in rule.from_status_ids:
            raise ValueError("Jira status changed before transition")
        guard()
        # Jira accepts issue properties on the transition request itself, allowing
        # a lost response to reconcile both the marker and resulting status.
        call(
            "POST",
            path + "/transitions",
            {
                "transition": {"id": rule.transition_id},
                "properties": [{"key": marker_key, "value": marker}],
            },
        )
        guard()
        old = call("GET", path + "/properties/" + marker_key)
        if (
            not isinstance(old, dict)
            or old.get("value") != marker
            or current_status() != rule.status_id
        ):
            raise ValueError("Jira transition could not be reconciled")
        return True

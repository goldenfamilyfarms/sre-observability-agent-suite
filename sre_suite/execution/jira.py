"""Jira Cloud ticket intake and opt-in idempotent execution-property writeback."""

import base64
import re
from collections.abc import Callable

from ..api_wire import decode
from ..contracts import JiraExecutionTicket
from .config import Settings
from .http import JsonHttp, RemoteError
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
        if not isinstance(raw_fields, dict) or not isinstance(raw_fields.get(field_name), str):
            raise ValueError("Jira contract field must contain a JSON text envelope")
        ticket = ticket_envelope(decode(raw_fields[field_name].encode()))
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

    def publish(self, key: str, execution_id: str, report: dict[str, object]) -> bool:
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
        self.http.call("PUT", path, report)
        return True

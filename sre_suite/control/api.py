"""Scoped control API. Roles and human authority come from server configuration, never payloads."""

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any
from wsgiref.types import WSGIEnvironment

from .. import api_wire
from ..api import ApiError, EvidenceContextApi
from ..api_storage import ApiRepositories
from ..auth import Action, Forbidden, Unauthenticated
from ..contracts import Environment, Scope
from ..execution.wire import canonical, digest
from ..memory import Finding, FindingState, MemoryKind, _decode
from ..serialization import scope_from_dict
from ..telemetry.contracts import integer
from .contracts import Binding, identifier, timestamp, wire
from .environments import Environments
from .store import ControlStore


@dataclass(frozen=True)
class ControlPolicy:
    humans: frozenset[str]
    stewards: frozenset[str]
    single_valued_predicates: frozenset[str] = frozenset()
    automatic_bindings: tuple[tuple[str, str], ...] = ()
    production_enabled: bool = False

    def __post_init__(self) -> None:
        if type(self.production_enabled) is not bool:
            raise ValueError("explicit production flag required")
        if self.humans & self.stewards:
            raise ValueError("human approvers and automated stewards must be distinct")
        for subject in self.humans | self.stewards:
            identifier(subject)
        for subject, binding in self.automatic_bindings:
            identifier(subject)
            if len(binding) != 64 or any(c not in "0123456789abcdef" for c in binding):
                raise ValueError("automatic approval requires an exact binding digest")


OPERATIONS = {
    "environment-register": Action.ENVIRONMENT_REGISTER,
    "propose": Action.FINDING_PROPOSE,
    "pending": Action.FINDING_RECONCILE,
    "reconcile": Action.FINDING_RECONCILE,
    "approve": Action.APPROVAL_WRITE,
    "revoke": Action.APPROVAL_WRITE,
    "approval": Action.APPROVAL_READ,
    "events": Action.EVENT_READ,
    "ack": Action.EVENT_ACK,
    "artifact-put": Action.ARTIFACT_WRITE,
    "artifact-get": Action.ARTIFACT_READ,
}


class ControlApi(EvidenceContextApi):
    def __init__(self, *args: Any, policy: ControlPolicy, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.policy = policy

    def _authorize(self, header: str, subject: str, scope: Scope, action: Action) -> datetime:
        now = self._clock()
        credential = self._auth.authenticate(header, now)
        if credential.subject != subject:
            raise Unauthenticated
        self._auth.authorize(credential, scope, action)
        if scope.environment == Environment.PRODUCTION and not self.policy.production_enabled:
            raise Forbidden
        return now

    @staticmethod
    def _current(repositories: ApiRepositories, scope: Scope, now: datetime) -> None:
        if scope.environment == Environment.PRODUCTION:
            try:
                Environments(repositories.previews._db).current(scope, now)
            except ValueError as error:
                raise ApiError(409, "environment_not_current") from error
        else:
            EvidenceContextApi._current(repositories, scope, now)

    def _handle(self, environ: WSGIEnvironment) -> bytes:
        path = environ.get("PATH_INFO", "")
        if not isinstance(path, str) or not path.startswith("/v1/control/"):
            return super()._handle(environ)
        if environ.get("wsgi.url_scheme") != "https":
            raise ApiError(400, "https_required")
        header = environ.get("HTTP_AUTHORIZATION", "")
        if not isinstance(header, str):
            raise Forbidden
        credential = self._auth.authenticate(header, self._clock())
        operation = path.removeprefix("/v1/control/")
        if operation not in OPERATIONS:
            raise ApiError(404, "not_found")
        if environ.get("REQUEST_METHOD") != "POST" or environ.get("QUERY_STRING"):
            raise ApiError(405, "method_not_allowed")
        try:
            raw = api_wire.envelope(
                api_wire.decode(self._body(environ)), {"scope", "operation_id", "payload"}
            )
            scope = scope_from_dict(raw["scope"])
            operation_id = identifier(raw["operation_id"])
            if not isinstance(raw["payload"], dict):
                raise ValueError("payload object required")
            payload = wire(raw["payload"])
        except (ValueError, TypeError) as error:
            raise ApiError(400, "invalid_request") from error
        subject = credential.subject
        action = OPERATIONS[operation]
        self._authorize(header, subject, scope, action)
        db = self._database()
        try:
            with db.transaction(write=True):
                repos = ApiRepositories(db)
                now = self._authorize(header, subject, scope, action)
                if operation != "environment-register":
                    self._current(repos, scope, now)
                store = ControlStore(db, scope)
                try:
                    result = self.apply(
                        operation, payload, subject, repos, store, now, operation_id
                    )
                except (ValueError, TypeError, KeyError) as error:
                    raise ApiError(409, "control_conflict_or_invalid") from error
                now = self._authorize(header, subject, scope, action)
                self._current(repos, scope, now)
                candidates = [payload.get("evidence"), result.get("evidence")]
                if operation == "reconcile":
                    proposal = store.get("proposal", identifier(payload["finding_id"]))
                    if proposal:
                        candidates.append(proposal[1]["finding"]["evidence"])
                for candidate in candidates:
                    if candidate is not None:
                        repos.evidence.read(api_wire.evidence_from_wire(candidate), scope, now)
                # Time-sensitive results must still be valid when the transaction commits.
                if operation in {"approve", "approval"} and result.get("active"):
                    if not now < timestamp(result["expires_at"]):
                        raise ApiError(409, "approval_expired")
                body = canonical(
                    {"schema_version": 2, "scope": asdict(scope), "result": result}
                ).encode()
                if len(body) > api_wire.MAX_RESPONSE_BYTES:
                    raise ApiError(413, "response_too_large")
                return body
        finally:
            db.close()

    def apply(
        self,
        operation: str,
        payload: dict[str, Any],
        subject: str,
        repos: ApiRepositories,
        store: ControlStore,
        now: datetime,
        operation_id: str,
    ) -> dict[str, Any]:
        if operation == "environment-register":
            if subject not in self.policy.humans or not self.policy.production_enabled:
                raise Forbidden
            Environments(store.db).register(store.scope, payload, now)
            result = {"scope": asdict(store.scope), "registered": True}
            return store.once(
                subject,
                operation_id,
                payload,
                lambda: {
                    **result,
                    "event": store.event("environment-registered", subject, result, now),
                },
            )
        if operation in {"approve", "approval", "revoke"}:
            return self.approval(operation, payload, subject, repos, store, now)
        if operation in {"propose", "pending", "reconcile"}:
            return self.knowledge(operation, payload, subject, repos, store, now, operation_id)
        if operation in {"events", "ack"}:
            if operation == "ack":
                return store.once(
                    subject,
                    operation_id,
                    {"operation": operation, **payload},
                    lambda: self.events(operation, payload, subject, store, now),
                )
            return self.events(operation, payload, subject, store, now)
        if operation == "artifact-put":
            row = api_wire.fields(payload, {"key", "evidence"})
            key = identifier(row["key"])
            evidence = api_wire.evidence_from_wire(row["evidence"])
            if evidence.producer != subject:
                raise Forbidden
            content = repos.evidence.read(evidence, store.scope, now)
            value = {"evidence": asdict(evidence), "content_sha256": digest(json.loads(content))}
            previous = store.get("artifact", key)
            if previous and canonical(previous[1]) != canonical(value):
                raise ValueError("immutable artifact key conflict")
            if not previous:
                store.put("artifact", key, 0, value)
                store.event("artifact-published", subject, {"key": key, **value}, now)
            return wire(value)
        row = api_wire.fields(payload, {"key"})
        saved = store.get("artifact", identifier(row["key"]))
        if not saved:
            raise ValueError("artifact unavailable")
        evidence = api_wire.evidence_from_wire(saved[1]["evidence"])
        repos.evidence.read(evidence, store.scope, now)
        return saved[1]

    def approval(
        self,
        operation: str,
        payload: dict[str, Any],
        subject: str,
        repos: ApiRepositories,
        store: ControlStore,
        now: datetime,
    ) -> dict[str, Any]:
        names = {"binding"} | ({"expires_at"} if operation == "approve" else set())
        row = api_wire.fields(payload, names)
        binding = Binding.parse(row["binding"])
        if binding.scope != store.scope or repos.knowledge.version != binding.context_version:
            raise ValueError("approval scope/context changed")
        current = store.get("approval", binding.identity)
        if operation == "approve":
            expiry = timestamp(row["expires_at"])
            if not now < expiry <= now + timedelta(hours=24):
                raise ValueError("approval expiry outside budget")
            human = subject in self.policy.humans
            if not human and (
                binding.target["environment"] == "production"
                or (subject, binding.identity) not in self.policy.automatic_bindings
            ):
                raise Forbidden
            if subject in binding.target.get("worker_subjects", []):
                raise Forbidden
            value = {
                "binding": binding.wire(),
                "approver": subject,
                "human": human,
                "expires_at": expiry.isoformat(),
                "revoked": False,
            }
            if current:
                # Never restore a revoked approval or extend it by retrying.
                if current[1].get("revoked") or any(
                    current[1].get(k) != v for k, v in value.items()
                ):
                    raise ValueError("approval already exists with another state")
            else:
                store.put("approval", binding.identity, 0, value)
                store.event("approval-issued", subject, value, now)
                current = (1, value)
        elif operation == "revoke":
            if subject not in self.policy.humans or current is None:
                raise Forbidden
            if not current[1]["revoked"]:
                value = {**current[1], "revoked": True}
                store.put("approval", binding.identity, current[0], value)
                store.event("approval-revoked", subject, {"binding": binding.wire()}, now)
                current = (current[0] + 1, value)
        if current is None:
            return {"active": False, "binding": binding.wire()}
        value = current[1]
        if value["binding"] != binding.wire():
            raise ValueError("approval binding mismatch")
        active = not value["revoked"] and now < timestamp(value["expires_at"])
        # Configuration removal revokes authority without trusting a cached credential decision.
        active = active and (
            value["approver"] in self.policy.humans
            if value["human"]
            else binding.target["environment"] != "production"
            and (value["approver"], binding.identity) in self.policy.automatic_bindings
        )
        return {**value, "active": bool(active), "version": current[0]}

    def knowledge(
        self,
        operation: str,
        payload: dict[str, Any],
        subject: str,
        repos: ApiRepositories,
        store: ControlStore,
        now: datetime,
        operation_id: str,
    ) -> dict[str, Any]:
        if operation == "propose":
            row = api_wire.fields(
                payload, {"id", "kind", "subject", "predicate", "value", "evidence"}
            )
            identity = "finding-" + digest([subject, asdict(store.scope), identifier(row["id"])])
            evidence = api_wire.evidence_from_wire(row["evidence"])
            repos.evidence.read(evidence, store.scope, now)
            finding = Finding(
                identity,
                store.scope,
                MemoryKind(api_wire.text(row["kind"], 32)),
                api_wire.text(row["subject"], 240),
                api_wire.text(row["predicate"], 128),
                api_wire.text(row["value"], 4000),
                evidence,
            )
            value = {"finding": wire(asdict(finding)), "proposer": subject}
            previous = store.get("proposal", identity)
            if previous and previous[1] != value:
                raise ValueError("proposal changed")
            if not previous:
                repos.knowledge.propose_finding(finding)
                store.put("proposal", identity, 0, value)
                store.event("finding-proposed", subject, value, now)
            return {"finding_id": identity, "state": "proposed"}
        if subject not in self.policy.stewards:
            raise Forbidden
        if operation == "pending":
            api_wire.fields(payload, set())
            rows = store.db.execute(
                "SELECT payload FROM findings WHERE state=? ORDER BY id", (FindingState.PROPOSED,)
            ).fetchall()
            selected = []
            for record in rows:
                finding = _decode(record[0])
                proposal = (
                    store.get("proposal", finding.id) if finding.scope == store.scope else None
                )
                if proposal and proposal[1]["proposer"] != subject:
                    selected.append(proposal[1])
                if len(selected) == 100:
                    break
            return {"context_version": repos.knowledge.version, "proposals": selected}
        row = api_wire.fields(payload, {"finding_id", "decision", "expected_version", "supersedes"})
        finding_id = identifier(row["finding_id"])
        expected = integer(row["expected_version"], 0, 2**63 - 1)
        decision = row["decision"]
        if decision not in {"verify", "dispute", "corroborate", "stale"}:
            raise ValueError("unsupported reconciliation decision")
        proposal = store.get("proposal", finding_id)
        if not proposal or proposal[1]["proposer"] == subject:
            raise Forbidden
        finding = _decode(canonical({"schema_version": 2, **proposal[1]["finding"]}))
        repos.evidence.read(finding.evidence, store.scope, now)

        def apply() -> dict[str, Any]:
            if repos.knowledge.version != expected:
                raise ValueError("context changed before reconciliation")
            existing = store.db.execute(
                "SELECT state FROM findings WHERE id=?", (finding_id,)
            ).fetchone()
            if not existing or existing[0] in {"stale", "superseded"}:
                raise ValueError("terminal or unknown finding; propose fresh evidence")
            supersedes = row["supersedes"]
            if supersedes is not None:
                if decision != "verify":
                    raise ValueError("only a verified replacement can supersede a fact")
                old = store.db.execute(
                    "SELECT payload, state FROM findings WHERE id=?", (identifier(supersedes),)
                ).fetchone()
                if not old or old[1] != "verified":
                    raise ValueError("superseded fact must be canonical")
                prior = _decode(old[0])
                if (prior.scope, prior.kind, prior.subject, prior.predicate) != (
                    finding.scope,
                    finding.kind,
                    finding.subject,
                    finding.predicate,
                ) or prior.id == finding.id:
                    raise ValueError("supersession identity mismatch")
                self.set_finding_state(store, prior.id, FindingState.SUPERSEDED)
            if decision == "verify":
                if existing[0] in {"corroborated", "disputed"}:
                    store.db.execute(
                        "UPDATE findings SET state=? WHERE id=?", ("proposed", finding_id)
                    )
                repos.knowledge._single_valued_predicates = self.policy.single_valued_predicates
                state = repos.knowledge.reconcile_finding(finding_id, repos.knowledge.version, now)
                if supersedes is not None and state != FindingState.VERIFIED:
                    raise ValueError("replacement conflicts with another canonical fact")
            else:
                state = {
                    "dispute": FindingState.DISPUTED,
                    "corroborate": FindingState.CORROBORATED,
                    "stale": FindingState.STALE,
                }[str(decision)]
                self.set_finding_state(store, finding_id, state)
            result = {
                "finding_id": finding_id,
                "state": str(state),
                "context_version": repos.knowledge.version,
            }
            store.event("finding-reconciled", subject, {**result, "supersedes": supersedes}, now)
            return result

        return store.once(subject, operation_id, payload, apply)

    @staticmethod
    def set_finding_state(store: ControlStore, finding_id: str, state: FindingState) -> None:
        store.db.execute("UPDATE findings SET state=? WHERE id=?", (state, finding_id))
        store.db.execute("UPDATE metadata SET version=version+1 WHERE id=1")
        row = store.db.execute("SELECT version FROM metadata WHERE id=1").fetchone()
        assert row is not None
        store.db.execute("INSERT INTO events VALUES (?, ?, ?)", (int(row[0]), finding_id, state))

    def events(
        self,
        operation: str,
        payload: dict[str, Any],
        subject: str,
        store: ControlStore,
        now: datetime,
    ) -> dict[str, Any]:
        row = api_wire.fields(
            payload,
            {"consumer"}
            if operation == "events"
            else {"consumer", "expected_cursor", "batch_sha256"},
        )
        consumer = digest([subject, identifier(row["consumer"])])
        saved = store.get("cursor", consumer)
        cursor = saved[1]["cursor"] if saved else 0
        lease = store.get("event-batch", consumer)
        if lease and lease[1].get("batch") is not None:
            response = lease[1]["batch"]
        else:
            batch = store.events(cursor)
            response = {
                "events": batch,
                "cursor": cursor,
                "next_cursor": batch[-1]["sequence"] if batch else cursor,
            }
            response["batch_sha256"] = digest(response)
            if operation == "events" and batch:
                store.put("event-batch", consumer, lease[0] if lease else 0, {"batch": response})
                lease = store.get("event-batch", consumer)
        if operation == "ack":
            if row["expected_cursor"] != cursor or row["batch_sha256"] != response["batch_sha256"]:
                raise ValueError("event batch changed or acknowledgement is out of order")
            store.put(
                "cursor", consumer, saved[0] if saved else 0, {"cursor": response["next_cursor"]}
            )
            if lease:
                store.put("event-batch", consumer, lease[0], {"batch": None})
            return {"cursor": response["next_cursor"]}
        return dict(response)

"""Authenticated, exact-scope WSGI evidence/context API. No server is launched here."""

import base64
import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from http import HTTPStatus
from typing import BinaryIO, cast
from wsgiref.types import StartResponse, WSGIEnvironment

from . import api_wire
from .api_storage import ApiRepositories
from .auth import Action, Forbidden, TokenAuthenticator, Unauthenticated
from .contracts import Environment, Evidence, Scope
from .database import Database
from .serialization import encode_scope, scope_from_dict


class ApiError(Exception):
    def __init__(self, status: int, code: str) -> None:
        self.status = status
        self.code = code


def utc_now() -> datetime:
    return datetime.now(UTC)


def evidence_id(subject: str, scope: Scope, client_id: str) -> str:
    """Namespace immutable IDs by caller and complete preview scope."""
    identity = json.dumps([subject, encode_scope(scope), client_id], separators=(",", ":"))
    return "ev-" + hashlib.sha256(identity.encode()).hexdigest()


class EvidenceContextApi:
    ROUTES = {
        "/v1/evidence": Action.EVIDENCE_WRITE,
        "/v1/evidence/read": Action.EVIDENCE_READ,
        "/v1/context": Action.CONTEXT_READ,
    }

    def __init__(
        self,
        authenticator: TokenAuthenticator,
        database: Callable[[], Database],
        *,
        clock: Callable[[], datetime] = utc_now,
        allow_preview_registration: bool = False,
    ) -> None:
        if type(allow_preview_registration) is not bool:
            raise ValueError("registration opt-in must be boolean")
        self._auth = authenticator
        self._database = database
        self._clock = clock
        self._allow_preview_registration = allow_preview_registration

    def __call__(self, environ: WSGIEnvironment, start_response: StartResponse) -> list[bytes]:
        try:
            body = self._handle(environ)
            status = 200
        except Unauthenticated:
            status, body = 401, b'{"error":"invalid_token"}'
        except Forbidden:
            status, body = 403, b'{"error":"insufficient_scope"}'
        except ApiError as error:
            status = error.status
            body = json.dumps({"error": error.code}).encode()
        except Exception:
            # Never return DSNs, tokens, exception text, evidence or SQL diagnostics.
            status, body = 503, b'{"error":"service_unavailable"}'
        headers = [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("Pragma", "no-cache"),
            ("X-Content-Type-Options", "nosniff"),
        ]
        if status == 401:
            headers.append(("WWW-Authenticate", 'Bearer realm="sre-suite", error="invalid_token"'))
        if status == 405:
            headers.append(("Allow", "POST"))
        start_response(f"{status} {HTTPStatus(status).phrase}", headers)
        return [body]

    def _handle(self, environ: WSGIEnvironment) -> bytes:
        # The hosting server owns this value. Never trust X-Forwarded-Proto here.
        if environ.get("wsgi.url_scheme") != "https":
            raise ApiError(400, "https_required")
        authorization = environ.get("HTTP_AUTHORIZATION", "")
        if not isinstance(authorization, str):
            raise Unauthenticated
        credential = self._auth.authenticate(authorization, self._clock())
        if environ.get("QUERY_STRING"):
            raise ApiError(400, "query_parameters_not_supported")
        if environ.get("REQUEST_METHOD") != "POST":
            raise ApiError(405, "method_not_allowed")
        path = environ.get("PATH_INFO", "")
        action = self.ROUTES.get(path)
        if path == "/v1/previews/register" and self._allow_preview_registration:
            action = Action.PREVIEW_REGISTER
        if action is None:
            raise ApiError(404, "not_found")
        raw_body = self._body(environ)
        try:
            raw = api_wire.decode(raw_body)
            preview = None
            expected_generation = 0
            evidence = None
            content = b""
            expected_version = None
            if action is Action.PREVIEW_REGISTER:
                preview, expected_generation = api_wire.registration_from_wire(raw)
                scope = preview.scope
            elif action is Action.CONTEXT_READ:
                payload = api_wire.envelope(raw, {"scope", "expected_version"})
                scope = scope_from_dict(payload["scope"])
                api_wire.text(scope.service, 128)
                expected_version = payload["expected_version"]
                if expected_version is not None and (
                    type(expected_version) is not int or not 0 <= expected_version < 2**63
                ):
                    raise ValueError("invalid context version")
            elif action is Action.EVIDENCE_WRITE:
                payload = api_wire.envelope(raw, {"evidence", "content_base64"})
                evidence = api_wire.evidence_from_wire(
                    payload["evidence"], producer=credential.subject
                )
                scope = evidence.scope
                evidence = replace(evidence, id=evidence_id(credential.subject, scope, evidence.id))
                content = api_wire.content_from_wire(payload["content_base64"])
            else:
                payload = api_wire.envelope(raw, {"evidence"})
                evidence = api_wire.evidence_from_wire(payload["evidence"])
                scope = evidence.scope
        except (ValueError, TypeError, RecursionError, OverflowError) as error:
            raise ApiError(400, "invalid_request") from error
        self._authorize(authorization, credential.subject, scope, action)
        database = self._database()
        try:
            # Reads also acquire the generation lock: one consistent view across
            # preview state, evidence and canonical context on this connection.
            with database.transaction(write=True):
                repositories = ApiRepositories(database)
                now = self._authorize(authorization, credential.subject, scope, action)
                if preview is not None:
                    try:
                        repositories.previews.start(preview, expected_generation, now)
                    except ValueError as error:
                        raise ApiError(409, "preview_registration_conflict") from error
                self._current(repositories, scope, now)
                context_evidence: tuple[Evidence, ...] = ()
                try:
                    if preview is not None:
                        response = {
                            "schema_version": 2,
                            "preview": asdict(preview),
                            "expected_generation": expected_generation,
                        }
                    elif action is Action.CONTEXT_READ:
                        snapshot = repositories.knowledge.get_context_snapshot(scope, now)
                        if expected_version is not None and snapshot.version != expected_version:
                            raise ApiError(409, "context_changed")
                        if len(snapshot.findings) > 200:
                            raise ApiError(413, "context_too_large")
                        context_evidence = tuple(item.evidence for item in snapshot.findings)
                        response = {"schema_version": 2, **asdict(snapshot)}
                    elif action is Action.EVIDENCE_WRITE and evidence is not None:
                        repositories.evidence.put(evidence, content, now)
                        response = {"schema_version": 2, "evidence": asdict(evidence)}
                    elif evidence is not None:
                        try:
                            content = repositories.evidence.read(evidence, scope, now)
                        except ValueError as error:
                            raise ApiError(404, "evidence_unavailable") from error
                        response = {
                            "schema_version": 2,
                            "evidence": asdict(evidence),
                            "content_base64": base64.b64encode(content).decode("ascii"),
                        }
                    else:
                        raise RuntimeError("missing decoded evidence")
                except ValueError as error:
                    raise ApiError(409, "evidence_conflict_or_invalid") from error
                result = json.dumps(response, default=str, sort_keys=True).encode("utf-8")
                if len(result) > api_wire.MAX_RESPONSE_BYTES:
                    raise ApiError(413, "response_too_large")
                # Recheck after storage work: expiry/revocation must roll back uploads.
                now = self._authorize(authorization, credential.subject, scope, action)
                self._current(repositories, scope, now)
                if any(not item.valid_for(scope, now) for item in context_evidence):
                    raise ApiError(409, "context_changed")
                if evidence is not None and not evidence.valid_for(scope, now):
                    raise ApiError(409, "evidence_expired")
                return result
        finally:
            database.close()

    def _authorize(self, header: str, subject: str, scope: Scope, action: Action) -> datetime:
        now = self._clock()
        credential = self._auth.authenticate(header, now)
        if credential.subject != subject:
            raise Unauthenticated
        self._auth.authorize(credential, scope, action)
        if scope.environment is Environment.PRODUCTION:
            raise Forbidden  # This API is for the preview pilot, never production approval.
        return now

    @staticmethod
    def _current(repositories: ApiRepositories, scope: Scope, now: datetime) -> None:
        try:
            repositories.previews.require_current(scope, now)
        except ValueError as error:
            raise ApiError(409, "preview_not_current") from error

    @staticmethod
    def _body(environ: WSGIEnvironment) -> bytes:
        if environ.get("CONTENT_TYPE", "").lower() not in {
            "application/json",
            "application/json; charset=utf-8",
        } or environ.get("HTTP_CONTENT_ENCODING"):
            raise ApiError(415, "unsupported_media_type")
        if environ.get("HTTP_TRANSFER_ENCODING"):
            raise ApiError(400, "unsupported_framing")
        length = environ.get("CONTENT_LENGTH", "")
        if not isinstance(length, str) or not length.isascii() or not length.isdecimal():
            raise ApiError(411, "content_length_required")
        if len(length) > 7 or int(length) > api_wire.MAX_BODY_BYTES:
            raise ApiError(413, "request_too_large")
        body = cast(BinaryIO, environ["wsgi.input"]).read(int(length))
        if not isinstance(body, bytes) or len(body) != int(length):
            raise ApiError(400, "truncated_body")
        return body

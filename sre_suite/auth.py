"""Operator-provisioned opaque service credentials; never a caller-supplied role.

This is a bounded pilot authenticator, not an OAuth issuer or a human approval API.
Only SHA-256 digests of high-entropy bearer credentials belong in configuration.
"""

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from threading import RLock

from .contracts import Scope, aware


class Action(StrEnum):
    PREVIEW_REGISTER = "preview:register"
    EVIDENCE_WRITE = "evidence:write"
    EVIDENCE_READ = "evidence:read"
    CONTEXT_READ = "context:read"


WORKER_ACTIONS = (Action.EVIDENCE_WRITE, Action.EVIDENCE_READ, Action.CONTEXT_READ)


@dataclass(frozen=True)
class Grant:
    scope: Scope
    action: Action

    def __post_init__(self) -> None:
        if not isinstance(self.scope, Scope) or not isinstance(self.action, Action):
            raise ValueError("grant requires an exact scope and supported action")


@dataclass(frozen=True)
class Credential:
    subject: str
    token_sha256: str
    audience: str
    not_before: datetime
    expires_at: datetime
    grants: frozenset[Grant]

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", self.subject):
            raise ValueError("invalid service subject")
        if not re.fullmatch(r"[0-9a-f]{64}", self.token_sha256):
            raise ValueError("credential requires a SHA-256 digest")
        if not self.audience or len(self.audience) > 128:
            raise ValueError("credential requires an audience")
        aware(self.not_before)
        aware(self.expires_at)
        if self.expires_at <= self.not_before:
            raise ValueError("invalid credential validity window")
        if not isinstance(self.grants, frozenset) or not self.grants or len(self.grants) > 1000:
            raise ValueError("credential requires bounded immutable grants")
        if any(not isinstance(grant, Grant) for grant in self.grants):
            raise ValueError("invalid credential grant")


class Unauthenticated(Exception):
    pass


class Forbidden(Exception):
    pass


def token_digest(token: str) -> str:
    """Use secrets.token_urlsafe(32) or stronger; syntax cannot prove entropy."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token):
        raise ValueError("expected a high-entropy URL-safe service token")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class TokenAuthenticator:
    def __init__(self, audience: str, credentials: tuple[Credential, ...]) -> None:
        if not audience or len(audience) > 128:
            raise ValueError("authenticator requires a service audience")
        self._audience = audience
        self._lock = RLock()
        self.replace_credentials(credentials)

    def replace_credentials(self, credentials: tuple[Credential, ...]) -> None:
        """Trusted operator reload. Removing a digest revokes it on subsequent checks.

        Reload every serving process; this local registry is not distributed revocation.
        """
        if not isinstance(credentials, tuple) or len(credentials) > 1000:
            raise ValueError("expected a bounded credential tuple")
        if any(not isinstance(item, Credential) for item in credentials):
            raise ValueError("invalid credential configuration")
        if len({item.token_sha256 for item in credentials}) != len(credentials):
            raise ValueError("duplicate credential digest")
        with self._lock:
            self._credentials = credentials

    def authenticate(self, authorization: str, now: datetime) -> Credential:
        aware(now)
        match = re.fullmatch(r"(?i:Bearer) ([A-Za-z0-9_-]{43,128})", authorization)
        if not match:
            raise Unauthenticated
        digest = token_digest(match[1])
        with self._lock:
            # Fixed-size digests are compared without comparing caller-controlled strings.
            credential = next(
                (
                    item
                    for item in self._credentials
                    if hmac.compare_digest(item.token_sha256, digest)
                ),
                None,
            )
        if (
            credential is None
            or credential.audience != self._audience
            or not credential.not_before <= now < credential.expires_at
        ):
            raise Unauthenticated
        return credential

    @staticmethod
    def authorize(credential: Credential, scope: Scope, action: Action) -> None:
        if Grant(scope, action) not in credential.grants:
            raise Forbidden

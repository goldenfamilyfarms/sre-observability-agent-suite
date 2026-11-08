"""Bounded HTTPS requests with redirect refusal and redacted failures."""

import json
from collections.abc import Mapping
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..api_wire import decode
from .config import https_url


class RemoteError(Exception):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"remote request failed ({status})")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        return None


class JsonHttp:
    def __init__(
        self,
        base: str,
        authorization: str | None,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = 20,
    ) -> None:
        if not 0 < timeout <= 20:
            raise ValueError("invalid HTTP timeout")
        self.base = https_url(base)
        self._authorization = authorization
        self._headers = dict(headers or {})
        self._timeout = timeout
        self._opener = build_opener(NoRedirect())

    def call(self, method: str, path: str, payload: object = None) -> object:
        if not path.startswith("/") or path.startswith("//") or ".." in path:
            raise ValueError("invalid relative endpoint")
        body = (
            None if payload is None else json.dumps(payload, default=str, allow_nan=False).encode()
        )
        if body is not None and len(body) > 1_500_000:
            raise ValueError("outgoing body too large")
        request = Request(
            self.base + path,
            body,
            method=method,
            headers={
                **({"Authorization": self._authorization} if self._authorization else {}),
                **self._headers,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read(1_500_001)
                if len(raw) > 1_500_000:
                    raise ValueError("remote response too large")
                return decode(raw) if raw else None
        except HTTPError as error:
            error.close()
            raise RemoteError(error.code) from None
        except (URLError, TimeoutError, OSError):
            raise RemoteError(0) from None

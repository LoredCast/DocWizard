"""Incoming HTTP requests: query, cookies, raw bodies, JSON and forms, with limits."""

from __future__ import annotations

import json
from http.cookies import CookieError, SimpleCookie
from typing import Any
from urllib.parse import parse_qs

DEFAULT_BODY_LIMIT = 1 << 20  # 1 MiB
MAX_JSON_DEPTH = 32


class HTTPError(Exception):
    def __init__(self, status: int, code: str, message: str = "", headers: list | None = None):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code.replace("_", " ").capitalize()
        self.headers = headers or []


def not_found(what: str = "Not found") -> HTTPError:
    return HTTPError(404, "not_found", what)


def forbidden(what: str = "You don't have access to this.") -> HTTPError:
    return HTTPError(403, "forbidden", what)


def bad_request(what: str) -> HTTPError:
    return HTTPError(400, "bad_request", what)


def _depth(value: Any, level: int = 0) -> int:
    if level > MAX_JSON_DEPTH:
        return level
    if isinstance(value, dict):
        return max([_depth(v, level + 1) for v in value.values()], default=level + 1)
    if isinstance(value, list):
        return max([_depth(v, level + 1) for v in value], default=level + 1)
    return level


class Request:
    def __init__(self, scope: dict, receive):
        self.scope = scope
        self._receive = receive
        self.method: str = scope["method"]
        self.path: str = scope["path"]
        self.raw_path: str = scope.get("raw_path", b"").decode("latin-1") or self.path
        self.headers: dict[str, str] = {}
        for k, v in scope.get("headers", []):
            name = k.decode("latin-1").lower()
            value = v.decode("latin-1")
            self.headers[name] = f"{self.headers[name]}, {value}" if name in self.headers else value
        qs = scope.get("query_string", b"").decode("latin-1")
        self.query: dict[str, str] = {k: v[0] for k, v in parse_qs(qs, keep_blank_values=True).items()}
        self.path_params: dict[str, str] = {}
        self._body: bytes | None = None
        # Filled in by middleware.
        self.session = None
        self.user = None
        self.guest = None
        self.nonce: str = ""
        self.request_id: str = ""

    # -- headers --------------------------------------------------------
    @property
    def host(self) -> str:
        return self.headers.get("host", "")

    @property
    def scheme(self) -> str:
        return self.headers.get("x-forwarded-proto") or self.scope.get("scheme", "http")

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.host}".lower()

    @property
    def client_ip(self) -> str:
        fwd = self.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
        client = self.scope.get("client")
        return client[0] if client else ""

    @property
    def cookies(self) -> dict[str, str]:
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("cookie", ""))
        except CookieError:
            return {}
        return {k: m.value for k, m in jar.items()}

    @property
    def wants_json(self) -> bool:
        return self.path.startswith("/api/")

    # -- bodies ---------------------------------------------------------
    async def body(self, limit: int = DEFAULT_BODY_LIMIT) -> bytes:
        if self._body is not None:
            return self._body
        declared = self.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            raise HTTPError(413, "too_large", "The request body is too large.")
        chunks, size = [], 0
        while True:
            message = await self._receive()
            if message["type"] == "http.disconnect":
                raise HTTPError(400, "disconnected")
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > limit:
                raise HTTPError(413, "too_large", "The request body is too large.")
            chunks.append(chunk)
            if not message.get("more_body"):
                break
        self._body = b"".join(chunks)
        return self._body

    async def json(self, limit: int = DEFAULT_BODY_LIMIT) -> Any:
        raw = await self.body(limit)
        try:
            value = json.loads(raw or b"null")
        except (ValueError, UnicodeDecodeError):
            raise bad_request("The request body is not valid JSON.") from None
        if _depth(value) > MAX_JSON_DEPTH:
            raise bad_request("The JSON body is nested too deeply.")
        return value

    async def json_object(self, limit: int = DEFAULT_BODY_LIMIT) -> dict:
        value = await self.json(limit)
        if not isinstance(value, dict):
            raise bad_request("Expected a JSON object.")
        return value

    async def form(self, limit: int = 64 * 1024) -> dict[str, str]:
        ctype = self.headers.get("content-type", "")
        if not ctype.startswith("application/x-www-form-urlencoded"):
            raise HTTPError(415, "unsupported_media_type", "Expected a URL-encoded form.")
        raw = (await self.body(limit)).decode("utf-8", "replace")
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    @property
    def actor_key(self) -> str:
        """Who is asking, for ownership of jobs and similar: "u12" or "g3"."""
        if self.user is not None:
            return f"u{self.user.id}"
        if self.guest is not None:
            return f"g{self.guest.id}"
        return ""

    @property
    def actor_name(self) -> str:
        if self.user is not None:
            return self.user.display_name
        if self.guest is not None:
            return self.guest.label
        return ""

    def param(self, name: str) -> str:
        # ASGI servers pass an already percent-decoded path.
        return self.path_params[name]

"""HTML, JSON and file responses, plus the security headers every response gets."""

from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
from typing import Any

from .request import HTTPError

# Content types that are safe to show inline on the app origin (§7.5).
INLINE_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp"}


class Response:
    def __init__(self, body: bytes | str = b"", status: int = 200, content_type: str = "text/plain; charset=utf-8",
                 headers: list[tuple[str, str]] | None = None):
        self.body = body.encode() if isinstance(body, str) else body
        self.status = status
        self.headers: list[tuple[str, str]] = [("content-type", content_type)]
        if headers:
            self.headers.extend(headers)
        self.file_path: Path | None = None

    def header(self, name: str) -> str | None:
        name = name.lower()
        for k, v in self.headers:
            if k == name:
                return v
        return None

    def set_header(self, name: str, value: str) -> None:
        name = name.lower()
        self.headers = [(k, v) for k, v in self.headers if k != name]
        self.headers.append((name, value))

    def set_cookie(self, name: str, value: str, *, max_age: int | None = None, secure: bool = True,
                   http_only: bool = True, same_site: str = "Lax", path: str = "/") -> None:
        parts = [f"{name}={value}", f"Path={path}", f"SameSite={same_site}"]
        if max_age is not None:
            parts.append(f"Max-Age={max_age}")
        if secure:
            parts.append("Secure")
        if http_only:
            parts.append("HttpOnly")
        self.headers.append(("set-cookie", "; ".join(parts)))

    def delete_cookie(self, name: str, *, secure: bool = True) -> None:
        self.set_cookie(name, "", max_age=0, secure=secure)

    async def send(self, send) -> None:
        raw_headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in self.headers]
        if self.file_path is not None:
            size = self.file_path.stat().st_size
            raw_headers.append((b"content-length", str(size).encode()))
            await send({"type": "http.response.start", "status": self.status, "headers": raw_headers})
            fd = os.open(self.file_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as fh:
                while True:
                    chunk = fh.read(256 * 1024)
                    more = len(chunk) == 256 * 1024
                    await send({"type": "http.response.body", "body": chunk, "more_body": more})
                    if not more:
                        break
            return
        raw_headers.append((b"content-length", str(len(self.body)).encode()))
        await send({"type": "http.response.start", "status": self.status, "headers": raw_headers})
        await send({"type": "http.response.body", "body": self.body})


def html(body: str, status: int = 200) -> Response:
    return Response(body, status, "text/html; charset=utf-8")


def json_response(data: Any, status: int = 200) -> Response:
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    resp = Response(body, status, "application/json")
    resp.set_header("cache-control", "no-store")
    return resp


def error_json(err: HTTPError) -> Response:
    resp = json_response({"error": {"code": err.code, "message": err.message}}, err.status)
    for k, v in err.headers:
        resp.set_header(k, v)
    return resp


def redirect(location: str, status: int = 303) -> Response:
    return Response(b"", status, headers=[("location", location)])


def guess_type(name: str) -> str:
    ctype, _ = mimetypes.guess_type(name)
    return ctype or "application/octet-stream"


def user_content(path: Path, name: str, *, cache: str = "private, no-cache") -> Response:
    """Serve untrusted user content on the app origin (§7.5)."""
    ctype = guess_type(name)
    resp = Response(b"", 200, ctype if ctype in INLINE_TYPES else "application/octet-stream")
    resp.file_path = path
    if ctype not in INLINE_TYPES:
        safe = "".join(ch for ch in os.path.basename(name) if ch.isalnum() or ch in "._- ") or "download"
        resp.set_header("content-disposition", f'attachment; filename="{safe}"')
    if ctype == "application/pdf":
        # The browser's PDF viewer doesn't run in a sandbox; the editor frames PDFs for the preview.
        resp.set_header("content-security-policy", "frame-ancestors 'self'")
    else:
        resp.set_header("content-security-policy", "sandbox; default-src 'none'")
    resp.set_header("x-content-type-options", "nosniff")
    resp.set_header("cross-origin-resource-policy", "same-origin")
    resp.set_header("cache-control", cache)
    return resp


def app_csp(nonce: str, preview_origin: str) -> str:
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        f"style-src 'self' 'nonce-{nonce}'; "
        "style-src-attr 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        f"frame-src 'self' {preview_origin}; "
        "connect-src 'self'; "
        "worker-src 'self'; object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    )


def apply_security_headers(resp: Response, nonce: str, preview_origin: str, secure: bool) -> None:
    if resp.header("content-security-policy") is None:
        resp.set_header("content-security-policy", app_csp(nonce, preview_origin))
    resp.set_header("x-content-type-options", "nosniff")
    if resp.header("referrer-policy") is None:
        resp.set_header("referrer-policy", "same-origin")
    if resp.header("cross-origin-opener-policy") is None:
        resp.set_header("cross-origin-opener-policy", "same-origin")
    if secure:
        resp.set_header("strict-transport-security", "max-age=31536000")

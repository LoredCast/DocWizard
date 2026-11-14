"""CSRF protection: matching Origin plus a per-session token (§13.3)."""

from __future__ import annotations

import hmac

from .request import HTTPError, Request

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def check_origin(request: Request, app_origin: str) -> None:
    origin = request.headers.get("origin")
    if origin is None:
        # Browsers send Origin on every unsafe request; fall back to Referer.
        referer = request.headers.get("referer", "")
        if not referer.lower().startswith(app_origin + "/") and referer.lower() != app_origin:
            raise HTTPError(403, "bad_origin", "Cross-site request refused.")
        return
    if origin.lower() != app_origin:
        raise HTTPError(403, "bad_origin", "Cross-site request refused.")


def check_token(supplied: str | None, expected: str | None) -> None:
    if not supplied or not expected or not hmac.compare_digest(supplied, expected):
        raise HTTPError(403, "bad_csrf", "The form expired. Reload the page and try again.")

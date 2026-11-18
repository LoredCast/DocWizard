"""An optional anti-bot challenge on registration and password reset (§13.2).

Off by default. With ``security.captcha_provider`` set to ``turnstile`` (Cloudflare)
or ``hcaptcha``, the forms show the provider's widget and the server checks its
answer with the provider through the outbound request policy (§15) before doing
anything else.
"""

from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import urlencode

from ..netpolicy import FetchError, PolicyError

log = logging.getLogger("docwizard.auth")

PROVIDERS = {
    "turnstile": {
        "script": "https://challenges.cloudflare.com/turnstile/v0/api.js",
        "origin": "https://challenges.cloudflare.com",
        "verify": "https://challenges.cloudflare.com/turnstile/v0/siteverify",
        "field": "cf-turnstile-response",
        "class": "cf-turnstile",
    },
    "hcaptcha": {
        "script": "https://js.hcaptcha.com/1/api.js",
        "origin": "https://hcaptcha.com https://*.hcaptcha.com",
        "verify": "https://api.hcaptcha.com/siteverify",
        "field": "h-captcha-response",
        "class": "h-captcha",
    },
}


def widget(cfg) -> dict | None:
    """What a form needs to show the challenge, or None when it is off."""
    provider = PROVIDERS.get(cfg.security.captcha_provider)
    if provider is None:
        return None
    return {"script": provider["script"], "class": provider["class"], "site_key": cfg.security.captcha_site_key}


def csp_sources(cfg) -> str:
    provider = PROVIDERS.get(cfg.security.captcha_provider)
    return provider["origin"] if provider else ""


def with_widget_csp(resp, cfg, nonce: str, preview_origin: str):
    """Let the provider's script and frame load on this page only."""
    from ..http.response import app_csp

    extra = csp_sources(cfg)
    if not extra:
        return resp
    csp = app_csp(nonce, preview_origin)
    csp = csp.replace("script-src 'self'", f"script-src 'self' {extra}", 1)
    csp = csp.replace("frame-src 'self'", f"frame-src 'self' {extra}", 1)
    csp = csp.replace("connect-src 'self'", f"connect-src 'self' {extra}", 1)
    csp = csp.replace("style-src 'self'", f"style-src 'self' {extra}", 1)
    resp.set_header("content-security-policy", csp)
    return resp


async def verify(app, form: dict, ip: str) -> bool:
    """True when the challenge is off or was solved. Fails closed when the provider can't be reached."""
    provider = PROVIDERS.get(app.cfg.security.captcha_provider)
    if provider is None:
        return True
    answer = str(form.get(provider["field"], "") or "")
    if not answer or len(answer) > 4096:
        return False
    if app.captcha_verifier is not None:  # tests
        return bool(app.captcha_verifier(answer, ip))
    body = urlencode({"secret": app.cfg.security.captcha_secret, "response": answer, "remoteip": ip}).encode()

    def call():
        return app.net.fetch("POST", provider["verify"], body=body, timeout=10, max_bytes=100_000,
                             headers={"Content-Type": "application/x-www-form-urlencoded"})

    try:
        resp = await asyncio.to_thread(call)
        return resp.status == 200 and bool(json.loads(resp.body).get("success"))
    except (FetchError, PolicyError, ValueError) as exc:
        log.warning("anti-bot check failed: %s", exc)
        return False

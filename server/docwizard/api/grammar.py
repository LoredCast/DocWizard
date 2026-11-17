"""Grammar checking (§6.8): which checkers this server offers, and LanguageTool through the app.

Harper runs in the browser from web/vendor/harper/ (installed by deploy/fetch_tools.py), so
texts never leave the reader's machine. A LanguageTool server, when the admin configures one,
checks other languages too; the editor sends the text here and the app passes it on, so the
LanguageTool server can stay internal and the page's content security policy stays strict.
"""

from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlencode, urlsplit

from ..auth.throttle import TokenBucket
from ..http.request import HTTPError, Request, bad_request
from ..http.response import Response, json_response
from ..netpolicy import FetchError, Policy, PolicyError

CHOICES = ("auto", "harper", "languagetool", "off")
MAX_TEXT = 200_000
_LANG = re.compile(r"^(auto|[a-z]{2,3}(?:-[A-Za-z]{2,4})?)$")


def harper_installed(app) -> bool:
    from ..asgi import WEB_DIR

    return app.cfg.grammar.harper and (WEB_DIR / "vendor" / "harper" / "harper_wasm_bg.wasm").is_file()


def available(app) -> dict:
    return {"harper": harper_installed(app), "languagetool": bool(app.cfg.grammar.languagetool_url)}


def choice_of(prefs: dict | None) -> str:
    choice = (prefs or {}).get("grammar", "auto")
    return choice if choice in CHOICES else "auto"


def register(app) -> None:
    r = app.router
    bucket = TokenBucket(30, 0.5)  # per person: a burst of 30 checks, then one every two seconds

    async def check(req: Request) -> Response:
        cfg = app.cfg.grammar
        if not cfg.languagetool_url:
            raise HTTPError(404, "not_configured", "This server has no LanguageTool.")
        body = await req.json_object(4 * MAX_TEXT)
        text, lang = body.get("text"), str(body.get("language") or "auto")
        if not isinstance(text, str) or len(text) > MAX_TEXT:
            raise bad_request("Send at most 200,000 characters at a time.")
        if not _LANG.match(lang):
            raise bad_request("Unknown language.")
        if not bucket.allow(req.actor_key):
            raise HTTPError(429, "rate_limited", "Checking too often. Wait a moment.")
        form = {"text": text, "language": lang}
        if lang == "auto":
            form["preferredVariants"] = "en-US,de-DE,pt-PT,ca-ES"
        if cfg.languagetool_api_key:
            form.update(apiKey=cfg.languagetool_api_key, username=cfg.languagetool_username)
        url = cfg.languagetool_url.rstrip("/") + "/v2/check"
        # The admin chose this server, so it may be on the internal network.
        policy = Policy([*app.policy("allow_private_outbound"), urlsplit(url).hostname or ""],
                        resolver=app.net_resolver)
        try:
            resp = await asyncio.to_thread(
                policy.fetch, "POST", url, body=urlencode(form).encode(), timeout=30,
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        except (FetchError, PolicyError) as exc:
            raise HTTPError(502, "languagetool_unreachable", f"LanguageTool can't be reached: {exc}") from None
        if resp.status != 200:
            raise HTTPError(502, "languagetool_error", f"LanguageTool answered {resp.status}.")
        try:
            data = json.loads(resp.body)
        except ValueError:
            raise HTTPError(502, "languagetool_error", "LanguageTool sent something unexpected.") from None
        matches = []
        for m in data.get("matches", [])[:500]:
            try:
                start, length = int(m["offset"]), int(m["length"])
            except (KeyError, TypeError, ValueError):
                continue
            rule = m.get("rule") or {}
            matches.append({
                "from": start, "to": start + length, "message": str(m.get("message", ""))[:500],
                "title": str(m.get("shortMessage") or (rule.get("category") or {}).get("name") or "")[:120],
                "kind": "spelling" if rule.get("issueType") == "misspelling" else "grammar",
                "suggestions": [str(x.get("value", ""))[:200] for x in (m.get("replacements") or [])[:5]],
                "rule": str(rule.get("id", ""))[:80],
            })
        language = (data.get("language") or {}).get("detectedLanguage", {}).get("code") or lang
        return json_response({"matches": matches, "language": language})

    r.add("POST", "/api/grammar/check", check, guest=True)

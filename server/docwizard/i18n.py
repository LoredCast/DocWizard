"""Interface languages: English (the source strings), German, French and Spanish (§21, M6).

Strings are looked up by their English text in ``locale/<lang>.json``; a missing
entry falls back to English. Placeholders use ``str.format`` names, so a
translation can reorder them. Pages get ``_()``; scripts get the same catalog
through a JSON block in the page, and error messages are translated where they
leave the server.
"""

from __future__ import annotations

import datetime as dt
import json
from functools import lru_cache
from pathlib import Path

LANGUAGES = {"en": "English", "de": "Deutsch", "fr": "Français", "es": "Español"}
DEFAULT = "en"
LOCALE_DIR = Path(__file__).resolve().parent / "locale"


@lru_cache(maxsize=None)
def catalog(lang: str) -> dict[str, str]:
    if lang == DEFAULT or lang not in LANGUAGES:
        return {}
    try:
        data = json.loads((LOCALE_DIR / f"{lang}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str) and v and not k.startswith("//")}


def gettext(lang: str, message: str, **values) -> str:
    text = catalog(lang).get(message, message)
    if values:
        try:
            return text.format(**values)
        except (KeyError, IndexError, ValueError):
            return message.format(**values)
    return text


def pick(accept_language: str | None) -> str:
    """The best supported language from an Accept-Language header."""
    best, best_q = DEFAULT, 0.0
    for part in (accept_language or "").split(","):
        tag, _, params = part.strip().partition(";")
        code = tag.strip().lower().split("-")[0]
        q = 1.0
        if params.strip().startswith("q="):
            try:
                q = float(params.strip()[2:])
            except ValueError:
                q = 0.0
        if code in LANGUAGES and q > best_q:
            best, best_q = code, q
    return best


_MONTHS_DE = ["Jan.", "Feb.", "März", "Apr.", "Mai", "Juni", "Juli", "Aug.", "Sept.", "Okt.", "Nov.", "Dez."]
_MONTHS_FR = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
_MONTHS_ES = ["ene.", "feb.", "mar.", "abr.", "may.", "jun.", "jul.", "ago.", "sept.", "oct.", "nov.", "dic."]


def reltime(lang: str, value) -> str:
    """Relative for the last 7 days, then the date (§25.12). Takes ISO strings or Unix times."""
    if not value:
        return ""
    if isinstance(value, (int, float)):
        when = dt.datetime.fromtimestamp(value, dt.UTC)
    else:
        try:
            when = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return str(value)
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    secs = (dt.datetime.now(dt.UTC) - when).total_seconds()
    _ = lambda m, **kw: gettext(lang, m, **kw)  # noqa: E731
    if secs < 60:
        return _("just now")
    if secs < 3600:
        return _("{n} min ago", n=int(secs // 60))
    if secs < 86400:
        return _("{n} h ago", n=int(secs // 3600))
    if secs < 2 * 86400:
        return _("yesterday")
    if secs < 7 * 86400:
        return _("{n} days ago", n=int(secs // 86400))
    if lang == "de":
        return f"{when.day}. {_MONTHS_DE[when.month - 1]} {when.year}"
    if lang == "fr":
        return f"{when.day} {_MONTHS_FR[when.month - 1]} {when.year}"
    if lang == "es":
        return f"{when.day} {_MONTHS_ES[when.month - 1]} {when.year}"
    return f"{when.day} {when.strftime('%b %Y')}"

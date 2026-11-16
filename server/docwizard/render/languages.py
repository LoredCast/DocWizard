"""Document languages (§9.8): the tags DocWizard knows, for the project setting and validation.

The data is filters/locales.json, which the render filters (lang.lua, quotes.lua) read too, so
the languages offered here are the ones whose dates, quotation marks and terms the renderer has.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path

LOCALES = Path(__file__).resolve().parents[3] / "filters" / "locales.json"
_TAG = re.compile(r"^([a-zA-Z]{2,3})(?:[-_]([a-zA-Z]{4}))?(?:[-_]([a-zA-Z]{2}|\d{3}))?$")


@cache
def data() -> dict:
    try:
        return json.loads(LOCALES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"languages": {}, "names": {}}


def normalize(tag: str | None) -> str | None:
    """'de_at' -> 'de-AT', 'German' -> 'de'. None if *tag* isn't a language tag."""
    if not isinstance(tag, str):
        return None
    tag = tag.strip()
    if not tag:
        return None
    named = data()["names"].get(tag.lower())
    if named:
        return named
    m = _TAG.match(tag)
    if not m:
        return None
    lang, script, region = m.groups()
    out = [lang.lower()]
    if script:
        out.append(script.title())
    if region:
        out.append(region.upper())
    return "-".join(out)


def choices() -> list[tuple[str, str]]:
    """(tag, label) for the language select: regional variants under their language, by name."""
    langs = data()["languages"]
    out = []
    for tag, entry in langs.items():
        native = entry.get("native") or entry.get("name") or tag
        name = entry.get("name") or tag
        out.append((tag, native if native == name else f"{native} · {name}"))
    return sorted(out, key=lambda item: item[1].lower())


def full_support(tag: str) -> bool:
    """Whether DocWizard writes dates and terms in this language (others get quotation marks only)."""
    base = (normalize(tag) or "").split("-")[0]
    return "terms" in data()["languages"].get(base, {})

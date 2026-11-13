"""Jinja2 environment. Autoescaping is always on (§6.2)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import jinja2

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

_JSON_ESCAPES = {
    "<": "\\u003c",
    ">": "\\u003e",
    "&": "\\u0026",
    " ": "\\u2028",
    " ": "\\u2029",
}


def script_json(value) -> str:
    """JSON safe to embed in <script type="application/json"> (§6.2)."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    for ch, esc in _JSON_ESCAPES.items():
        text = text.replace(ch, esc)
    return text


def filesize(n: int | None) -> str:
    if n is None:
        return ""
    for unit, size in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= size:
            return f"{n / size:.0f} {unit}" if n / size >= 10 else f"{n / size:.1f} {unit}"
    return f"{n} B"


def reltime(value: str | None) -> str:
    """Relative for the last 7 days, then the date (§25.12)."""
    if not value:
        return ""
    try:
        when = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    secs = (dt.datetime.now(dt.UTC) - when).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} h ago"
    if secs < 2 * 86400:
        return "yesterday"
    if secs < 7 * 86400:
        return f"{int(secs // 86400)} days ago"
    return f"{when.day} {when.strftime('%b %Y')}"


def make_env() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATE_DIR),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["filesize"] = filesize
    env.filters["reltime"] = reltime
    return env

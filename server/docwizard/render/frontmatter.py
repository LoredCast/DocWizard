"""Reading and editing a document's YAML front matter without a YAML library (§9.4).

Only what the preflight check and the anonymizer need: where the block is, its top-level
keys with their lines, plain values (scalars, lists in either style) and the names
under ``author:``. The text outside the block is never touched.
"""

from __future__ import annotations

import re

_KEY = re.compile(r"^([A-Za-z_][\w-]*)[ \t]*:(.*)$")


def block(text: str) -> tuple[int, int, list[str]] | None:
    """(first line, closing line, lines of the whole text) for a front matter block at the top."""
    lines = text.split("\n")
    if not lines or lines[0].rstrip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].rstrip() in ("---", "..."):
            return 0, i, lines
    return None


def entries(text: str) -> list[tuple[str, int, int]]:
    """Top-level keys with the lines they span: [(key, start, end)], *end* exclusive."""
    found = block(text)
    if found is None:
        return []
    _start, close, lines = found
    out: list[tuple[str, int, int]] = []
    for i in range(1, close):
        line = lines[i]
        m = _KEY.match(line)
        if m and not line.startswith((" ", "\t", "-", "#")):
            if out:
                out[-1] = (out[-1][0], out[-1][1], i)
            out.append((m.group(1).lower(), i, close))
    return out


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _strip_comment(value: str) -> str:
    return re.sub(r"\s+#.*$", "", value)


def raw(text: str, key: str) -> list[str] | None:
    """The lines of *key*'s entry, the key line first."""
    found = block(text)
    if found is None:
        return None
    lines = found[2]
    for k, start, end in entries(text):
        if k == key:
            return lines[start:end]
    return None


def value(text: str, key: str):
    """A scalar as a string, a list of strings, a block scalar's text, or None."""
    lines = raw(text, key)
    if not lines:
        return None
    first = _strip_comment(_KEY.match(lines[0]).group(2)).strip()
    rest = [line for line in lines[1:] if line.strip() and not line.lstrip().startswith("#")]
    if first.startswith("[") and first.endswith("]"):
        return [_unquote(p) for p in first[1:-1].split(",") if p.strip()]
    if first in ("|", ">", "|-", ">-", "|+", ">+"):
        return "\n".join(line.strip() for line in lines[1:])
    if first:
        return _unquote(first)
    items = [_unquote(_strip_comment(line.strip()[1:])) for line in rest if line.lstrip().startswith("- ")]
    return items or None


def number(text: str, key: str) -> int | None:
    v = value(text, key)
    if isinstance(v, str):
        m = re.fullmatch(r"\s*([\d][\d,. ]*)\s*", v)
        if m:
            digits = re.sub(r"[^\d]", "", m.group(1))
            return int(digits) if digits else None
    return None


def author_names(text: str) -> list[str]:
    """The names under author: (a string, a list of strings or of name: entries)."""
    lines = raw(text, "author") or raw(text, "authors")
    if not lines:
        return []
    names: list[str] = []
    first = _strip_comment(_KEY.match(lines[0]).group(2)).strip()
    if first.startswith("["):
        return [_unquote(p) for p in first.strip("[]").split(",") if p.strip()]
    if first and first not in ("|", ">"):
        return [_unquote(first)]
    for line in lines[1:]:
        s = line.strip()
        m = re.match(r"^-?\s*name\s*:\s*(.+)$", s)
        if m:
            names.append(_unquote(_strip_comment(m.group(1))))
        elif s.startswith("- ") and ":" not in s:
            names.append(_unquote(_strip_comment(s[2:])))
        elif s.startswith("- ") and re.match(r"^-\s*[\w-]+\s*:", s) is None:
            names.append(_unquote(s[2:]))
    return [n for n in names if n]


def body_start(text: str) -> int:
    """The line where the text after the front matter starts."""
    found = block(text)
    return found[1] + 1 if found else 0

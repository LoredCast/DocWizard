"""Word documents with comments and tracked changes become Markdown with comment threads and
suggestions (§8.11).

The render worker converts the .docx with pandoc and ``filters/docx_import.lua``, which marks
comment ranges and tracked changes with private-use characters and appends their details as
JSON. :func:`parse` turns that into the document's text, in Word's original wording, plus
threads to create: comments where they were, and every tracked change as a suggestion that
an editor accepts or rejects, as they would in Word.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

O, C = "", ""
_MARK = re.compile(O + r"(/?)([CID])([^" + C + r"]*)" + C)
_DATA = re.compile(r"\n*<!-- dw-import (\{.*?\}) -->\s*$", re.S)


@dataclass
class Thread:
    start: int  # Python string offsets into the text
    end: int
    author: str
    body: str = ""
    suggestion: str | None = None
    date: str = ""
    replies: list[tuple[str, str]] = field(default_factory=list)  # (author, body)


def _word_around(text: str, a: int, b: int) -> tuple[int, int]:
    """Widen an empty range to a neighbouring word so a thread has text to sit on."""
    if a < b:
        return a, b
    s = a
    while s > 0 and not text[s - 1].isspace():
        s -= 1
    if s < a:
        return s, a
    e = a
    while e < len(text) and not text[e].isspace():
        e += 1
    if e > a:
        return a, e
    # Between spaces: take the word before.
    s = a
    while s > 0 and text[s - 1].isspace() and text[s - 1] != "\n":
        s -= 1
    e = s
    while s > 0 and not text[s - 1].isspace():
        s -= 1
    return (s, a) if s < e else (a, a)


def parse(markdown: str) -> tuple[str, list[Thread]]:
    """Split the worker's output into the original text and the threads to create."""
    data = {"comments": [], "changes": []}
    m = _DATA.search(markdown)
    if m:
        try:
            data = json.loads(m.group(1))
        except ValueError:
            pass
        markdown = markdown[:m.start()] + "\n"
    comments = {str(c.get("id")): c for c in data.get("comments", [])}
    changes = data.get("changes", [])

    out: list[str] = []
    length = 0
    pos = 0
    comment_at: dict[str, list[int]] = {}
    change_ranges: list[dict] = []  # {kind, n, at, text, start, end}
    inserting: dict | None = None
    deleting: dict | None = None
    for mk in _MARK.finditer(markdown):
        chunk = markdown[pos:mk.start()]
        pos = mk.end()
        if inserting is not None:
            inserting["text"] += chunk
        else:
            out.append(chunk)
            length += len(chunk)
        closing, kind, ident = mk.group(1) == "/", mk.group(2), mk.group(3)
        if kind == "C":
            rng = comment_at.setdefault(ident, [length, length])
            rng[1 if closing else 0] = length
            if not closing:
                rng[1] = max(rng[1], length)
        elif kind == "I":
            if not closing and inserting is None:
                inserting = {"kind": "I", "n": ident, "at": length, "text": ""}
            elif closing and inserting is not None:
                change_ranges.append(inserting)
                inserting = None
        elif kind == "D":
            if not closing:
                deleting = {"kind": "D", "n": ident, "start": length}
            elif deleting is not None:
                deleting["end"] = length
                change_ranges.append(deleting)
                deleting = None
    tail = markdown[pos:]
    if inserting is not None:
        inserting["text"] += tail
        change_ranges.append(inserting)
    else:
        out.append(tail)
    text = "".join(out)

    def info(n: str) -> dict:
        try:
            return changes[int(n) - 1]
        except (ValueError, IndexError):
            return {}

    threads: list[Thread] = []
    i = 0
    while i < len(change_ranges):
        ch = change_ranges[i]
        who = info(ch["n"])
        if ch["kind"] == "D":
            a, b, new = ch["start"], ch["end"], ""
            nxt = change_ranges[i + 1] if i + 1 < len(change_ranges) else None
            # A deletion right before an insertion is one replacement, as Word shows it.
            if nxt is not None and nxt["kind"] == "I" and nxt["at"] == b:
                new = nxt["text"]
                i += 1
            if a == b and not new:
                i += 1
                continue
            if a == b:
                a, b = _word_around(text, a, b)
                new = text[a:ch["start"]] + new + text[ch["start"]:b]
        else:
            if not ch["text"]:
                i += 1
                continue
            a, b = _word_around(text, ch["at"], ch["at"])
            new = text[a:ch["at"]] + ch["text"] + text[ch["at"]:b]
        i += 1
        if a == b or text[a:b] == new:
            continue
        threads.append(Thread(a, b, who.get("author") or "Word", suggestion=new, date=who.get("date", "")))

    by_range: dict[tuple[int, int], Thread] = {}
    for ident, (a, b) in comment_at.items():
        c = comments.get(ident)
        if c is None:
            continue
        a, b = _word_around(text, a, b)
        if a == b:
            continue
        body = (c.get("text") or "").strip() or "…"
        author = c.get("author") or "Word"
        # Word keeps replies as comments on the same text: they join the first one's thread.
        first = by_range.get((a, b))
        if first is not None:
            first.replies.append((author, body))
            continue
        t = Thread(a, b, author, body=body, date=c.get("date", ""))
        by_range[(a, b)] = t
        threads.append(t)
    threads.sort(key=lambda t: (t.start, t.end))
    return text, threads


def comment_markers(text: str, threads: list[dict]) -> str:
    """Put open comment threads into Markdown as pandoc comment spans, for DOCX export.

    *threads* have Python string offsets ``start``/``end``, ``id``, and ``comments``:
    a list of {author, date, body}. Anchors are moved off Markdown block syntax at the start
    of a line, and threads in the front matter or crossing a code fence are left out.
    """
    fm = re.match(r"^---\n.*?\n(?:---|\.\.\.)\n", text, re.S)
    body_start = fm.end() if fm else 0
    fences = [(m.start(), m.end()) for m in re.finditer(r"^(```|~~~).*?^\1[^\n]*$", text, re.S | re.M)]
    inserts: list[tuple[int, int, str]] = []  # (position, order, text)
    n = 0
    for t in threads:
        a, b = t["start"], t["end"]
        if a < body_start or b <= a or b > len(text):
            continue
        if any(fa <= b and a <= fb for fa, fb in fences) or "\n\n" in text[a:b]:
            continue  # inside code, or across paragraphs: a span can't hold it
        line_start = text.rfind("\n", 0, a) + 1
        lead = re.match(r"[ \t]*(?:#{1,6}[ \t]+|[-*+][ \t]+|\d+[.)][ \t]+|>[ \t]?|\[[ xX]\][ \t]+)*",
                        text[line_start:])
        a = max(a, line_start + (lead.end() if lead else 0))
        if b <= a:
            continue
        ids = []
        for c in t["comments"]:
            ident = str(n)
            n += 1
            ids.append(ident)
            attrs = f'id="{ident}" author="{_attr(c.get("author", ""))}" date="{_attr(c.get("date", ""))}"'
            inserts.append((a, 0, f"[{_escape(c.get('body', ''))}]{{.comment-start {attrs}}}"))
        for ident in ids:
            inserts.append((b, 1, f'[]{{.comment-end id="{ident}"}}'))
    # Insert from the end so earlier offsets stay valid; ends go before starts at the same place.
    for pos, _order, piece in sorted(inserts, key=lambda x: (x[0], -x[1]), reverse=True):
        text = text[:pos] + piece + text[pos:]
    return text


def from_utf16(text: str, offsets: list[int]) -> list[int]:
    """Python string indices for UTF-16 offsets (the editor's and the anchors' unit)."""
    wanted = sorted(set(offsets))
    found: dict[int, int] = {}
    u = 0
    j = 0
    for i, ch in enumerate(text):
        while j < len(wanted) and wanted[j] <= u:
            found[wanted[j]] = i
            j += 1
        u += 2 if ord(ch) > 0xFFFF else 1
    for w in wanted[j:]:
        found[w] = len(text)
    return [found[o] for o in offsets]


def _attr(value: str) -> str:
    return str(value).replace("\\", "").replace('"', "'").replace("\n", " ")


def _escape(body: str) -> str:
    body = " ".join(str(body).split())
    return re.sub(r"([\\`*_{}\[\]()#+\-.!<>$@~^|\"&])", r"\\\1", body)

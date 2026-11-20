"""Companion decks: a first draft of slides for a paper (§6.3).

The deck keeps the paper's structure: chapters with sections become section slides, sections
become slides with the first sentence of each paragraph (or the paper's own list items) as
points, figures and charts get slides of their own, and the paragraphs go in the speaker
notes. Citations stay, so the deck ends with the works it cites. The author then edits the
draft like any document; nothing links it back to the paper.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_POINTS = 5
MAX_WORDS = 24
MAX_NOTES = 900

FRONT_KEYS = ("title", "subtitle", "author", "date", "bibliography", "lang", "csl", "link-citations")
SKIP_TITLES = {"references", "bibliography", "works cited", "literature", "literatur", "literaturverzeichnis",
               "quellen", "sources", "bibliographie", "références"}

_FRONT = re.compile(r"^---[ \t]*\n(.*?)\n(?:---|\.\.\.)[ \t]*(?:\n|$)", re.S)
_ATX = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_DIV = re.compile(r"^ {0,3}(:{3,})[ \t]*(.*)$")
_IMAGE = re.compile(r'^!\[(?P<alt>[^\]]*)\]\((?P<src>[^)\s]+)(?:\s+"[^"]*")?\)(?P<attr>\{[^}]*\})?\s*$')
_LIST = re.compile(r"^(?:[-+*]|\d+[.)])[ \t]+(.*)$")
_SRC = re.compile(r'\bsrc\s*=\s*"?([^"\s}]+)"?')
_ATTR = re.compile(r"\s*\{[^}]*\}\s*$")
_FOOTNOTE_REF = re.compile(r"\[\^[^\]]+\]")
_FOOTNOTE_DEF = re.compile(r"^\[\^[^\]]+\]:")
_SENTENCE_END = re.compile(r"(?<=[.!?])[\"”’)]?\s+(?=[A-ZÀ-ÖØ-Þ0-9\"“(\[])")
_ABBREV = re.compile(r"(?:\b(?:e\.g|i\.e|cf|vs|al|etc|Fig|fig|Eq|eq|No|Dr|Mr|Mrs|Ms|Prof|S|p|pp|z\.B|d\.h|bzw|vgl|ca)\.)$")


@dataclass
class Section:
    level: int
    title: str
    paragraphs: list[str] = field(default_factory=list)
    points: list[str] = field(default_factory=list)
    figures: list[tuple[str, str]] = field(default_factory=list)  # (caption, the Markdown)
    charts: list[tuple[str, str]] = field(default_factory=list)  # (caption, the div's Markdown)
    equations: list[str] = field(default_factory=list)


def _front(text: str) -> tuple[list[str], str]:
    """The front matter lines worth keeping, and the body."""
    m = _FRONT.match(text)
    if not m:
        return [], text
    keep, take = [], False
    for line in m.group(1).split("\n"):
        key = re.match(r"^([\w-]+):", line)
        if key:
            take = key.group(1) in FRONT_KEYS
        elif line and not line[0].isspace():
            take = False
        if take:
            keep.append(line)
    return keep, text[m.end():]


def first_sentence(paragraph: str) -> str:
    text = " ".join(paragraph.split())
    pos = 0
    while True:
        m = _SENTENCE_END.search(text, pos)
        if not m:
            sentence = text
            break
        if _ABBREV.search(text[: m.start()]):
            pos = m.end()
            continue
        sentence = text[: m.start()].rstrip()
        break
    words = sentence.split()
    if len(words) > MAX_WORDS:
        sentence = " ".join(words[:MAX_WORDS]).rstrip(",;:") + " …"
    return sentence


def _clean(line: str) -> str:
    return _FOOTNOTE_REF.sub("", line).strip()


def parse(body: str) -> list[Section]:
    sections = [Section(0, "")]
    lines = body.split("\n")
    para: list[str] = []
    item: list[str] | None = None
    i = 0

    def flush():
        nonlocal para, item
        if para:
            sections[-1].paragraphs.append(" ".join(para))
        if item is not None:
            sections[-1].points.append(" ".join(item))
        para, item = [], None

    while i < len(lines):
        line = lines[i]
        fence = _FENCE.match(line)
        if fence:
            flush()
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith(fence.group(1)):
                i += 1
            i += 1
            continue
        div = _DIV.match(line)
        if div and div.group(2):
            flush()
            attrs = div.group(2)
            if re.search(r"(^|[\s{])\.(chart|data)\b", attrs):
                # A chart or a table from a data file: the whole block, on a slide of its own.
                block = [line]
                i += 1
                while i < len(lines) and not re.match(r"^ {0,3}:{3,}[ \t]*$", lines[i]):
                    block.append(lines[i])
                    i += 1
                block.append(lines[i] if i < len(lines) else ":::")
                caption = next((b.strip() for b in block[1:-1] if b.strip()), "")
                sections[-1].charts.append((caption, "\n".join(block)))
                i += 1
                continue
            if "#refs" in attrs:
                i += 1
                while i < len(lines) and not re.match(r"^ {0,3}:{3,}[ \t]*$", lines[i]):
                    i += 1
                i += 1
                continue
            i += 1
            continue
        if div:  # a closing fence
            flush()
            i += 1
            continue
        heading = _ATX.match(line)
        if heading:
            flush()
            title = _ATTR.sub("", heading.group(2)).strip()
            sections.append(Section(len(heading.group(1)), title))
            i += 1
            continue
        if line.strip().startswith("$$"):
            flush()
            eq = [line]
            if not (line.strip().endswith("$$") and len(line.strip()) > 2):
                i += 1
                while i < len(lines):
                    eq.append(lines[i])
                    if lines[i].strip().endswith("$$"):
                        break
                    i += 1
            sections[-1].equations.append("\n".join(eq))
            i += 1
            continue
        image = _IMAGE.match(line.strip())
        if image and not para:
            flush()
            sections[-1].figures.append((image.group("alt"), line.strip()))
            i += 1
            continue
        if not line.strip() or _FOOTNOTE_DEF.match(line) or line.lstrip().startswith(("|", "<!--")):
            flush()
            i += 1
            continue
        listed = _LIST.match(line)
        if listed and not line[0].isspace():
            if para:
                flush()
            if item is not None:
                sections[-1].points.append(" ".join(item))
            item = [_clean(listed.group(1))]
        elif item is not None and line[0].isspace():
            if not _LIST.match(line.strip()):
                item.append(_clean(line))
        elif line.startswith(">"):
            para.append(_clean(line.lstrip("> ")))
        else:
            if item is not None:
                flush()
            para.append(_clean(line))
        i += 1
    flush()
    return sections


def _notes(section: Section) -> list[str]:
    text = "\n\n".join(section.paragraphs)
    if not text:
        return []
    if len(text) > MAX_NOTES:
        text = text[:MAX_NOTES].rsplit(" ", 1)[0] + " …"
    return ["", "::: notes", text, ":::"]


def _points(section: Section) -> list[str]:
    points = section.points[:MAX_POINTS] if section.points else \
        [first_sentence(p) for p in section.paragraphs[:MAX_POINTS]]
    return [f"- {p}" for p in points if p]


def companion_deck(text: str, fallback_title: str = "") -> tuple[str, set[str]]:
    """The deck's Markdown, and the paths of project files it uses (pictures, data, bibliographies)."""
    front, body = _front(text.replace("\r\n", "\n"))
    if not any(line.startswith("title:") for line in front) and fallback_title:
        front.insert(0, "title: " + _yaml(fallback_title))
    paths: set[str] = set()
    for line in front:
        for m in re.finditer(r"[\w./-]+\.(?:bib|json|yaml|yml|ris)\b", line):
            paths.add(m.group(0))
    sections = [s for s in parse(body) if s.title.strip().lower() not in SKIP_TITLES]
    levels = [s.level for s in sections if s.level]
    top = min(levels) if levels else 1
    out = ["---", *front, "slide-level: 2", "---", ""]

    def slide(title: str, content: list[str], notes: list[str]) -> None:
        out.extend([f"## {title}" if title else "---", "", *content, *notes, ""])

    for idx, s in enumerate(sections):
        has_children = any(t.level == s.level + 1 for t in sections[idx + 1:idx + 1 + _until(sections, idx)])
        title = s.title
        points = _points(s)
        if s.level == top and has_children:
            out.extend([f"# {title}", ""])
            if points:
                slide(title, points, _notes(s))
        elif s.level == 0:
            if points or s.figures:
                slide("", points, _notes(s))
        elif points or (not s.figures and not s.charts and not s.equations):
            content = points + ([""] + [s.equations[0]] if s.equations else [])
            slide(title, content, _notes(s))
        elif s.equations:
            slide(title, [s.equations[0]], _notes(s))
        for caption, md in s.figures:
            paths.add(_IMAGE.match(md).group("src"))
            slide(_ATTR.sub("", caption).strip() or title, [md], [])
        for caption, md in s.charts:
            src = _SRC.search(md)
            if src:
                paths.add(src.group(1))
            slide(caption or title, [md], [])
    return "\n".join(out).rstrip("\n") + "\n", {p for p in paths if not re.match(r"^[a-z]+://", p)}


def _until(sections: list[Section], idx: int) -> int:
    """How many sections follow *idx* before one at its level or above."""
    n = 0
    for s in sections[idx + 1:]:
        if s.level <= sections[idx].level:
            break
        n += 1
    return n


def _yaml(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

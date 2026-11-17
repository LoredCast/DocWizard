"""Fountain (fountain.io) import and export for screenplays (§9.3).

DocWizard's script layouts already follow Fountain's rules, so most lines carry over as they
are. What differs: Fountain's title page is DocWizard's front matter; Fountain's notes,
boneyard, synopses and lower-level sections don't print, so they become HTML comments;
``_underline_`` is ``[underline]{.underline}``; ``##`` headings are scene headings in
DocWizard but unprinted sections in Fountain; and plain Markdown would read some action
lines (leading hyphens, indentation, ``@`` and ``$``) as lists, code, citations or math.
"""

from __future__ import annotations

import json
import re

# Fountain title page keys -> front matter keys. Credit ("Written by") is printed by the layout.
TITLE_KEYS = {"title": "title", "author": "author", "authors": "author", "draft date": "draft",
              "draft": "draft", "contact": "contact", "notes": "notes", "copyright": "copyright",
              "source": "source"}
_TITLE_LINE = re.compile(r"^([A-Za-z][A-Za-z ]*):(.*)$")
_SCENE = re.compile(r"^(INT\.?/EXT|EXT\.?/INT|I/E|INT|EXT|EST|INNEN/AUSSEN|INNEN|AUSSEN|AUẞEN)[.\s/]", re.I)
_SCENE_NO = re.compile(r"\s*#[\w.\-]+#\s*$")
_UNDERLINE = re.compile(r"(?<![\w\\])_(?=\S)(.+?)(?<=\S)_(?!\w)")
_MD_UNDERLINE = re.compile(r"\[([^\[\]]+)\]\{\.underline\}")


def _plain(value: str) -> str:
    """A title page value without Fountain's emphasis marks."""
    while True:
        plain = re.sub(r"(\*{1,3}|_)(.+?)\1", r"\2", value)
        if plain == value:
            return plain.strip()
        value = plain


def _yaml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _title_page(lines: list[str]) -> tuple[dict[str, list[str]], int]:
    """Read Fountain's title page: key/value lines up to the first blank line."""
    if not lines or not _TITLE_LINE.match(lines[0]) or lines[0].split(":", 1)[0].strip().upper() in ("INT", "EXT"):
        return {}, 0
    found: dict[str, list[str]] = {}
    key = None
    i = 0
    while i < len(lines) and lines[i].strip():
        line = lines[i]
        m = _TITLE_LINE.match(line)
        if m and not line.startswith((" ", "\t")):
            key = m.group(1).strip().lower()
            found[key] = [m.group(2).strip()] if m.group(2).strip() else []
        elif key is not None:
            found[key].append(line.strip())
        else:
            return {}, 0
        i += 1
    return found, i


def _comment(text: str) -> str:
    return "<!-- " + text.replace("--", "–").strip() + " -->"


def to_markdown(text: str) -> str:
    """Convert a Fountain screenplay to DocWizard Markdown with front matter."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    # Boneyard /* … */ and notes [[ … ]] don't print: comments.
    text = re.sub(r"/\*(.*?)\*/", lambda m: _comment(m.group(1)), text, flags=re.S)
    text = re.sub(r"\[\[(.*?)\]\]", lambda m: _comment(m.group(1)), text, flags=re.S)
    lines = text.split("\n")
    meta, start = _title_page(lines)
    head = ["---"]
    for key, values in meta.items():
        name = TITLE_KEYS.get(key)
        if name is None or not values:
            continue
        values = [_plain(v) for v in values]
        if name == "author" and len(values) > 1:
            head.append("author:")
            head += [f"  - {_yaml_str(v)}" for v in values]
        elif len(values) > 1:
            head.append(f"{name}: |")
            head += [f"  {v}" for v in values]
        else:
            head.append(f"{name}: {_yaml_str(values[0])}")
    if "title" not in meta:
        head.append('title: ""')
    head.append("---")
    out: list[str] = []
    blank = True  # whether the previous line was blank
    for i in range(start, len(lines)):
        raw = lines[i]
        line = raw.strip()
        if not line:
            # Two spaces on an empty line keep a speech together in Fountain.
            if raw.startswith("  ") and out and not blank:
                out.append("\\")
                continue
            out.append("")
            blank = True
            continue
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if line == "===" or re.fullmatch(r"={3,}", line):
            out.append("\\newpage")
        elif line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            title = line[level:].strip()
            # Level 1 sections print as act titles; deeper ones are only for the writer.
            out.append(f"# {title}" if level == 1 else _comment("#" * level + " " + title))
        elif line.startswith("=") and not line.startswith("=="):
            out.append(_comment(line[1:]))
        elif blank and (_SCENE.match(line) or (line.startswith(".") and not line.startswith(".."))):
            out.append(_escape(_SCENE_NO.sub("", line), cue=True))
        elif line.startswith("!"):
            out.append(_escape(line[1:]))
        elif line.startswith("~"):
            out.append("*" + _escape(line[1:].strip()) + "*")
        elif line.startswith(">"):
            out.append(line)  # forced transitions and centred text are the same in DocWizard
        elif blank and nxt and (line.startswith("@") or (line.upper() == line and re.search(r"[^\W\d_]", line))):
            out.append(_escape(line, cue=True))
        else:
            out.append(_escape(line))
        blank = False
    body = "\n".join(out).strip("\n")
    body = re.sub(r"\n{3,}", "\n\n", body)
    return "\n".join(head) + "\n\n" + body + "\n"


def _escape(line: str, cue: bool = False) -> str:
    """Keep a Fountain line from reading as Markdown syntax it isn't."""
    line = _UNDERLINE.sub(r"[\1]{.underline}", line)
    lead = "@" if cue and line.startswith("@") else ""
    line = lead + re.sub(r"(?<!\\)([@$])", r"\\\1", line[len(lead):])
    if re.match(r"^([-+*]\s|\||:::)", line):
        line = "\\" + line
    line = re.sub(r"^(\d+)([.)])(\s)", r"\1\\\2\3", line)  # "1. " would start a list
    return line


# -- export --------------------------------------------------------------------------

def _front_matter(text: str) -> tuple[dict, str]:
    """A small reader for the front matter DocWizard scripts use: scalars, block scalars, lists."""
    m = re.match(r"^---\n(.*?)\n(?:---|\.\.\.)\n?", text, re.S)
    if not m:
        return {}, text
    data: dict = {}
    key = None
    block: list[str] | None = None
    for line in m.group(1).split("\n"):
        if block is not None and (line.startswith("  ") or not line.strip()):
            block.append(line[2:])
            continue
        block = None
        kv = re.match(r"^([\w-]+):\s*(.*)$", line)
        if kv:
            key, value = kv.group(1), kv.group(2).strip()
            if value in ("|", ">", "|-", ">-"):
                block = []
                data[key] = block
            elif value:
                data[key] = _scalar(value)
            else:
                data[key] = []
        elif key and re.match(r"^\s+-\s+", line) and isinstance(data.get(key), list):
            item = re.sub(r"^\s+-\s+", "", line)
            name = re.match(r"^name:\s*(.*)$", item)
            data[key].append(_scalar(name.group(1) if name else item))
    out = {}
    for k, v in data.items():
        if isinstance(v, list):
            v = [str(x) for x in v if str(x).strip()]
            if not v:
                continue
        out[k] = v
    return out, text[m.end():]


def _scalar(value: str) -> str:
    value = re.sub(r"\s+#.*$", "", value) if not value.startswith(("'", '"')) else value
    if value.startswith('"'):
        try:
            return str(json.loads(value))
        except ValueError:
            return value.strip('"')
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    return value


def from_markdown(text: str) -> str:
    """Convert a DocWizard screenplay to Fountain."""
    meta, body = _front_matter(text.replace("\r\n", "\n"))
    page: list[str] = []

    def put(key: str, value) -> None:
        values = value if isinstance(value, list) else str(value).split("\n")
        values = [v for v in values if str(v).strip()]
        if not values:
            return
        if len(values) == 1:
            page.append(f"{key}: {values[0]}")
        else:
            page.append(f"{key}:")
            page.extend(f"    {v}" for v in values)

    if meta.get("title"):
        put("Title", meta["title"])
        put("Credit", "Written by")
    if meta.get("author"):
        put("Authors" if isinstance(meta["author"], list) and len(meta["author"]) > 1 else "Author",
            meta["author"])
    put("Draft date", meta.get("draft") or meta.get("date") or "")
    contact = [*(meta.get(k) for k in ("contact", "address", "phone", "email") if meta.get(k))]
    lines: list[str] = []
    for c in contact:
        lines += c if isinstance(c, list) else str(c).split("\n")
    put("Contact", lines)
    put("Copyright", meta.get("copyright", ""))

    out: list[str] = []
    in_comment = False
    for line in body.strip("\n").split("\n"):
        s = line.strip()
        if in_comment:
            end = s.endswith("-->")
            out.append(s[:-3].rstrip() + "*/" if end else line)
            in_comment = not end
            continue
        c = re.fullmatch(r"<!--(.*?)-->", s)
        if c:
            inner = c.group(1).strip()
            out.append(inner if re.match(r"^#{2,}\s", inner) else f"[[{inner}]]")
            continue
        if s.startswith("<!--"):
            out.append("/*" + s[4:])
            in_comment = True
            continue
        if s in ("\\newpage", "\\pagebreak"):
            out.append("===")
            continue
        if s.startswith(":::"):
            continue  # explicit blocks: their lines already read right
        h = re.match(r"^(#{1,6})\s+(.*?)\s*(?:\{[^}]*\})?\s*#*\s*$", s)
        if h:
            title = h.group(2)
            out.append(f"# {title}" if len(h.group(1)) == 1 else ("" if _SCENE.match(title) else ".") + title)
            continue
        if s == "\\":
            out.append("  ")
            continue
        line = re.sub(r"(?<![\w*\\])_(?=\S)(.+?)(?<=\S)_(?![\w*])", r"*\1*", line)  # Markdown italics
        line = _MD_UNDERLINE.sub(r"_\1_", line)
        line = re.sub(r"\\([@$\-+|])", r"\1", line)  # \* stays: Fountain escapes the same way
        out.append(line)
    body_text = "\n".join(out).strip("\n")
    return ("\n".join(page) + "\n\n" if page else "") + body_text + "\n"

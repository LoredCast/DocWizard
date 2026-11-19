"""Double-blind exports (§9.11): the same document without who wrote it.

Front matter keys that name or locate the authors are removed and ``author`` becomes
"Anonymous", so the PDF, Word and e-book metadata carry no names either. In the text,
acknowledgement and funding sections go, and so do passages marked for the camera-ready
version only: ``::: unblind`` blocks, ``[…]{.unblind}`` spans and headings with that class.
``[…]{.blind}`` spans and ``::: blind`` blocks, which hold the anonymized wording, are kept
here and removed from normal exports. LaTeX gets ``\\author{Anonymous}`` and loses
``\\thanks``, ``\\affiliation`` and its relatives and the acknowledgements.
"""

from __future__ import annotations

import re

from . import frontmatter

# Front matter that identifies the authors.
STRIP_KEYS = {
    "author", "authors", "affiliation", "affiliations", "institute", "institution", "institutions", "department",
    "faculty", "email", "emails", "orcid", "thanks", "acknowledgments", "acknowledgements", "acknowledgment",
    "acknowledgement", "funding", "grant", "grants", "corresponding", "correspondence", "address", "contact",
    "phone", "author-note", "author-notes", "authornote", "contributor", "contributors", "credit", "student-id",
    "supervisor", "instructor", "advisor", "website", "homepage", "signature", "birthplace", "place",
}
ANONYMOUS = "Anonymous"

# Headings of sections that go: acknowledgements, funding, about the authors.
_SECTION = re.compile(
    r"^(acknowledge?ments?|danksagung(en)?|dank|funding( information)?|finanzierung|förderung|"
    r"author contributions?|credit authorship contribution statement|about the authors?|"
    r"über die autor(in|innen|en)?|zu den autor(inn)?en)$", re.I)
_HIDE_CLASSES = {"unblind", "camera-ready", "acknowledgments", "acknowledgements"}
_ATX = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*(?:\{([^}]*)\})?[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^(```+|~~~+)")
_DIV = re.compile(r"^(:{3,})[ \t]*(.*?)[ \t]*$")


def _classes(attrs: str) -> set[str]:
    attrs = attrs.strip().strip("{}").strip()
    if not attrs:
        return set()
    if not attrs.startswith(("#", ".")) and "=" not in attrs and " " not in attrs:
        return {attrs}  # ::: unblind
    return {c[1:] for c in attrs.split() if c.startswith(".")}


def _front_matter(text: str) -> str:
    found = frontmatter.block(text)
    if found is None:
        return f"---\nauthor: {ANONYMOUS}\n---\n\n{text}"
    _start, close, lines = found
    drop = set()
    for key, start, end in frontmatter.entries(text):
        if key in STRIP_KEYS:
            drop.update(range(start, end))
    kept = [line for i, line in enumerate(lines[:close]) if i not in drop]
    return "\n".join([*kept, f"author: {ANONYMOUS}", *lines[close:]])


def _span_pattern(classes: set[str]) -> re.Pattern:
    names = "|".join(re.escape(c) for c in sorted(classes))
    return re.compile(r"\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\{[^}]*\.(?:" + names + r")\b[^}]*\}")


_HIDE_SPAN = _span_pattern(_HIDE_CLASSES)
_BLIND_SPAN = _span_pattern({"blind"})


def markdown(text: str, *, anonymize: bool = True) -> str:
    """The anonymized document, or with *anonymize* False the normal one without ``.blind`` passages."""
    if anonymize:
        text = _front_matter(text)
    lines = text.split("\n")
    start = frontmatter.body_start(text)
    out = lines[:start]
    fence = None
    skip_level = 0  # inside a removed section: its heading level
    div_stack: list[bool] = []  # open fenced divs: True if removed
    hide = _HIDE_CLASSES if anonymize else {"blind"}
    for line in lines[start:]:
        if fence:
            if line.startswith(fence):
                fence = None
            if not skip_level and not any(div_stack):
                out.append(line)
            continue
        m = _FENCE.match(line)
        if m:
            fence = m.group(1)
            if not skip_level and not any(div_stack):
                out.append(line)
            continue
        h = _ATX.match(line)
        if h and not any(div_stack):
            level = len(h.group(1))
            if skip_level and level > skip_level:
                continue
            skip_level = 0
            title = re.sub(r"[*_`]", "", h.group(2)).strip().rstrip(".:")
            classes = _classes(h.group(3) or "")
            if (anonymize and _SECTION.match(title)) or classes & hide:
                skip_level = level
                continue
        elif skip_level:
            continue
        d = _DIV.match(line)
        if d:
            if d.group(2):  # an opening fence
                removed = bool(_classes(d.group(2)) & hide)
                div_stack.append(removed)
                if not any(div_stack):
                    out.append(line)
                continue
            if div_stack:  # a closing fence
                removed = any(div_stack)
                div_stack.pop()
                if not removed:
                    out.append(line)
                continue
        if any(div_stack):
            continue
        out.append(line)
    result = "\n".join(out)
    result = (_HIDE_SPAN if anonymize else _BLIND_SPAN).sub("", result)
    if anonymize:
        # Blind passages are what the reviewers read: keep their text, drop the marker.
        result = _BLIND_SPAN.sub(lambda m: m.group(1), result)
    return result


def _remove_command(text: str, name: str, replacement: str | None = None) -> str:
    """Remove ``\\name[opt]{arg}`` (balanced braces), or put *replacement* in its place."""
    out, i = [], 0
    pattern = re.compile(r"\\" + name + r"\*?\s*(\[[^\]]*\])?\s*\{")
    while True:
        m = pattern.search(text, i)
        if not m:
            out.append(text[i:])
            break
        depth, j = 1, m.end()
        while j < len(text) and depth:
            if text[j] == "\\":
                j += 2
                continue
            depth += {"{": 1, "}": -1}.get(text[j], 0)
            j += 1
        out.append(text[i:m.start()])
        if replacement is not None:
            out.append(replacement)
        i = j
    return "".join(out)


_LATEX_ACK = re.compile(r"\\(?:section|subsection|chapter)\*?\{\s*(acknowledge?ments?|danksagung|funding)\s*\}"
                        r".*?(?=\\(?:section|chapter|bibliography|printbibliography|appendix|end\{document\})|\Z)",
                        re.I | re.S)


def latex(text: str) -> str:
    text = _remove_command(text, "author", "\\author{" + ANONYMOUS + "}")
    for name in ("thanks", "affiliation", "affil", "institute", "address", "email", "orcid", "orcidlink",
                 "IEEEauthorblockA", "IEEEauthorblockN", "authornote", "corresponding"):
        text = _remove_command(text, name)
    text = re.sub(r"\\begin\{acks\}.*?\\end\{acks\}", "", text, flags=re.S)
    text = re.sub(r"\\begin\{acknowledge?ments?\}.*?\\end\{acknowledge?ments?\}", "", text, flags=re.S | re.I)
    return _LATEX_ACK.sub("", text)


def snapshot_texts(texts: dict[str, str], inputs: list[str], anonymize: bool) -> dict[str, str]:
    """The texts a render sees: inputs anonymized, or without their ``.blind`` passages."""
    out = dict(texts)
    for path in inputs:
        text = texts.get(path)
        if text is None:
            continue
        if path.endswith((".md", ".markdown")):
            changed = markdown(text, anonymize=anonymize)
        elif anonymize and path.endswith(".tex"):
            changed = latex(text)
        else:
            continue
        out[path] = changed
    return out


def has_blind(text: str) -> bool:
    return ".blind" in text or re.search(r"^:::+\s*\{?\.?blind\b", text, re.M) is not None

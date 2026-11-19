"""The check before submitting (§9.11): limits, cross-references, citations, open review
work and anonymity, for the documents a build uses.

Pure functions over the build's texts and a few counts from the database, so the API can
gather them and the tests can call them directly. Word counts follow the editor's rules
(web/app/workers/stats.js) and filters/wordcount.lua: body text without front matter,
code, comments, math, citation keys and footnotes.
"""

from __future__ import annotations

import json
import re

from . import anonymize, frontmatter

LABEL_KINDS = ("fig", "tbl", "eq", "sec")

_CODE = re.compile(r"^(```+|~~~+)[^\n]*\n.*?^\1[^\n]*$", re.M | re.S)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_DISPLAY_MATH = re.compile(r"\$\$.*?\$\$", re.S)
_INLINE_MATH = re.compile(r"\$[^$\n]+\$")
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_NOTE_INLINE = re.compile(r"\^\[((?:[^\[\]]|\[[^\]]*\])*)\]")
_NOTE_DEF = re.compile(r"^\[\^[^\]]+\]:[^\n]*(\n {4}[^\n]*)*", re.M)
_NOTE_REF = re.compile(r"\[\^[^\]]+\]")
_CITE_KEY = re.compile(r"-?@[\w][\w:.#$%&\-+?<>~/]*")
_WORD = re.compile(r"[^\W_][\w'’\-]*")

# @key in text: not part of an email address or a word.
_CITATION = re.compile(r"(?<![\w.@])-?@([\w][\w:.#$%&\-+?<>~/]*[\w])|(?<![\w.@])-?@\{([^}]+)\}")
_LABEL_DEF = re.compile(r"\{[^}]*#((?:fig|tbl|eq|sec):[\w:.\-]+)[^}]*\}")


def _strip(text: str) -> str:
    start = frontmatter.body_start(text)
    s = "\n".join(text.replace("\r", "").split("\n")[start:])
    s = _COMMENT.sub(" ", s)
    s = _CODE.sub(" ", s)
    s = _DISPLAY_MATH.sub(" ", s)
    s = _INLINE_MATH.sub(" ", s)
    return _INLINE_CODE.sub(" ", s)


def _count(text: str) -> int:
    s = _CITE_KEY.sub(" ", text)
    s = re.sub(r"\{[#.][^}]*\}", " ", s)
    s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"^\s{0,3}(#{1,6}|[-*+]|\d+[.)]|>|:::+.*|\|)", " ", s, flags=re.M)
    s = re.sub(r"[*_~`|#>\\\[\](){}]", " ", s)
    return len(_WORD.findall(s))


def words(text: str) -> tuple[int, int]:
    """(body words, footnote words) of a Markdown document."""
    body = _strip(text)
    notes = 0

    def inline(m):
        nonlocal notes
        notes += _count(m.group(1))
        return " "

    def definition(m):
        nonlocal notes
        notes += _count(re.sub(r"^\[\^[^\]]+\]:", "", m.group(0)))
        return " "

    body = _NOTE_INLINE.sub(inline, body)
    body = _NOTE_DEF.sub(definition, body)
    body = _NOTE_REF.sub(" ", body)
    return _count(body), notes


def _lines_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _scannable(text: str) -> str:
    """The text with code and comments blanked out, keeping every offset."""
    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))
    return _CODE.sub(blank, _COMMENT.sub(blank, text))


def labels(texts: dict[str, str]) -> tuple[dict, dict]:
    """Defined and referenced cross-reference labels: {label: [(path, line)]}."""
    defined: dict[str, list] = {}
    used: dict[str, list] = {}
    for path, text in texts.items():
        scan = _scannable(text)
        for m in _LABEL_DEF.finditer(scan):
            defined.setdefault(m.group(1), []).append((path, _lines_of(text, m.start())))
        for m in _CITATION.finditer(scan):
            key = m.group(1) or m.group(2)
            if key.split(":", 1)[0] in LABEL_KINDS and ":" in key:
                used.setdefault(key, []).append((path, _lines_of(text, m.start())))
    return defined, used


def citations(texts: dict[str, str]) -> dict[str, list]:
    """Cited keys (not cross-references): {key: [(path, line)]}."""
    out: dict[str, list] = {}
    for path, text in texts.items():
        start = frontmatter.body_start(text)
        offset = len("\n".join(text.split("\n")[:start]))
        scan = _scannable(text)
        for m in _CITATION.finditer(scan, offset):
            key = m.group(1) or m.group(2)
            if ":" in key and key.split(":", 1)[0] in LABEL_KINDS:
                continue
            out.setdefault(key, []).append((path, _lines_of(text, m.start())))
    return out


def bibliography_keys(path: str, text: str) -> set[str]:
    """The entry keys of a .bib, CSL JSON or RIS file."""
    if path.endswith(".bib"):
        return {m.group(1) for m in re.finditer(r"@(?!comment|preamble|string)\w+\s*[{(]\s*([^,\s]+)\s*,", text, re.I)}
    if path.endswith(".json"):
        try:
            data = json.loads(text)
        except ValueError:
            return set()
        items = data if isinstance(data, list) else data.get("items", []) if isinstance(data, dict) else []
        return {str(i["id"]) for i in items if isinstance(i, dict) and "id" in i}
    if path.endswith(".ris"):
        return {m.group(1).strip() for m in re.finditer(r"^ID  - (.+)$", text, re.M)}
    return set()


def _bib_paths(first: str, texts: dict[str, str]) -> list[str]:
    entries = frontmatter.value(first, "bibliography")
    if entries is None:
        return []
    if isinstance(entries, str):
        entries = [entries]
    out = []
    for e in entries:
        e = e.strip().lstrip("./")
        if e.startswith("zotero:"):
            out.append(f"_zotero/{e[len('zotero:'):]}.json")
        else:
            out.append(e)
    return [p for p in out if p in texts]


def _item(path: str, line: int, text: str) -> dict:
    return {"path": path, "line": line, "text": text}


def _check(id_: str, title: str, status: str, summary: str, items: list | None = None, **extra) -> dict:
    return {"id": id_, "title": title, "status": status, "summary": summary, "items": (items or [])[:100],
            "more": max(0, len(items or []) - 100), **extra}


def run(texts: dict[str, str], inputs: list[str], *, review: dict[str, dict] | None = None,
        pages: int | None = None, gettext=None) -> dict:
    """Every check for a build of *inputs*. *review* is {path: {"comments", "suggestions",
    "conflicts"}}; *pages* the rendered PDF's page count, if known."""
    _ = gettext or (lambda message, **values: message.format(**values) if values else message)
    md = {p: texts[p] for p in inputs if p in texts and p.endswith((".md", ".markdown"))}
    first = next(iter(md.values()), "")
    checks = []

    # Length: words and pages against the limits in the front matter.
    body = notes = 0
    for text in md.values():
        b, n = words(text)
        body, notes = body + b, notes + n
    word_limit = frontmatter.number(first, "word-limit")
    if word_limit:
        over = body - word_limit
        checks.append(_check("words", _("Word limit"), "fail" if over > 0 else "ok",
                             _("{n} words, {over} over the limit of {limit}.", n=f"{body:,}", over=f"{over:,}",
                               limit=f"{word_limit:,}") if over > 0 else
                             _("{n} words of {limit}.", n=f"{body:,}", limit=f"{word_limit:,}"),
                             words=body, notes=notes, limit=word_limit))
    else:
        checks.append(_check("words", _("Word limit"), "info",
                             _("{n} words, {notes} in footnotes. Add word-limit: to the front matter to check "
                               "against a limit.", n=f"{body:,}", notes=f"{notes:,}"), words=body, notes=notes))
    abstract = frontmatter.value(first, "abstract")
    abstract_limit = frontmatter.number(first, "abstract-word-limit")
    if abstract_limit and isinstance(abstract, str):
        n = _count(abstract)
        checks.append(_check("abstract", _("Abstract length"), "fail" if n > abstract_limit else "ok",
                             _("The abstract has {n} words; the limit is {limit}.", n=n, limit=abstract_limit)))
    page_limit = frontmatter.number(first, "page-limit")
    if page_limit and pages is not None:
        checks.append(_check("pages", _("Page limit"), "fail" if pages > page_limit else "ok",
                             _("{n} pages of {limit}.", n=pages, limit=page_limit) if pages <= page_limit else
                             _("{n} pages, {over} over the limit of {limit}.", n=pages, over=pages - page_limit,
                               limit=page_limit), pages=pages, limit=page_limit))
    elif page_limit:
        checks.append(_check("pages", _("Page limit"), "info",
                             _("The limit is {limit} pages. Count them in the PDF.", limit=page_limit),
                             limit=page_limit, needsPdf=True))
    elif pages is not None:
        checks.append(_check("pages", _("Page limit"), "info",
                             _("The PDF has {n} pages. Add page-limit: to the front matter to check against a "
                               "limit.", n=pages), pages=pages))

    # Cross-references.
    defined, used = labels(md)
    dangling = [_item(p, line, _("@{label} refers to nothing", label=label))
                for label, where in sorted(used.items()) if label not in defined for p, line in where]
    unused = [_item(where[0][0], where[0][1], _("#{label} is never referred to", label=label))
              for label, where in sorted(defined.items()) if label not in used and not label.startswith("sec:")]
    twice = [_item(where[1][0], where[1][1], _("#{label} is defined more than once", label=label))
             for label, where in sorted(defined.items()) if len(where) > 1]
    status = "fail" if dangling or twice else "warn" if unused else "ok"
    summary = _("Every reference has a target.") if status == "ok" else \
        _("{dangling} broken, {unused} never referred to.", dangling=len(dangling) + len(twice), unused=len(unused))
    checks.append(_check("labels", _("Figures, tables and sections"), status, summary, dangling + twice + unused))

    # Citations against the bibliography.
    cited = citations(md)
    nocite = frontmatter.value(first, "nocite") or ""
    nocite_text = " ".join(nocite) if isinstance(nocite, list) else str(nocite)
    bib_paths = _bib_paths(first, texts)
    known: set[str] = set()
    for p in bib_paths:
        known |= bibliography_keys(p, texts[p])
    if not bib_paths:
        checks.append(_check("citations", _("Citations"), "info" if not cited else "warn",
                             _("No bibliography is set.") if not cited else
                             _("{n} sources are cited, but no bibliography is set.", n=len(cited))))
    else:
        missing = [_item(where[0][0], where[0][1], _("@{key} is not in the bibliography", key=key))
                   for key, where in sorted(cited.items()) if key not in known]
        listed = set(re.findall(r"@([\w][\w:.#$%&\-+?<>~/]*[\w])", nocite_text))
        all_listed = "@*" in nocite_text
        uncited = [] if all_listed else [
            _item(bib_paths[0], 0, _("{key} is in the bibliography but never cited", key=key))
            for key in sorted(known - set(cited) - listed)]
        status = "fail" if missing else "warn" if uncited else "ok"
        checks.append(_check("citations", _("Citations"), status,
                             _("{cited} sources cited, all in the bibliography.", cited=len(cited)) if status == "ok"
                             else _("{missing} not in the bibliography, {uncited} entries never cited.",
                                    missing=len(missing), uncited=len(uncited)), missing + uncited))

    # Review work still open.
    review = review or {}
    open_comments = [_item(p, 0, _("{n} open comments", n=r["comments"]) if r["comments"] != 1 else
                           _("1 open comment")) for p, r in review.items() if r.get("comments")]
    open_suggestions = [_item(p, 0, _("{n} open suggestions", n=r["suggestions"]) if r["suggestions"] != 1 else
                              _("1 open suggestion")) for p, r in review.items() if r.get("suggestions")]
    conflicts = [_item(p, 0, _("{n} conflicts to resolve", n=r["conflicts"]) if r["conflicts"] != 1 else
                       _("1 conflict to resolve")) for p, r in review.items() if r.get("conflicts")]
    n_comments = sum(r.get("comments", 0) for r in review.values())
    n_suggestions = sum(r.get("suggestions", 0) for r in review.values())
    checks.append(_check("comments", _("Comments"), "warn" if n_comments else "ok",
                         _("{n} unresolved.", n=n_comments) if n_comments else _("None unresolved."), open_comments))
    checks.append(_check("suggestions", _("Tracked changes"), "warn" if n_suggestions else "ok",
                         _("{n} suggestions not accepted or rejected.", n=n_suggestions) if n_suggestions
                         else _("None open."), open_suggestions))
    if conflicts:
        checks.append(_check("conflicts", _("Conflicts"), "fail", _("Outside edits clash with the text."),
                             conflicts))

    # Anonymity: what an anonymized export removes and names left in the text.
    names = frontmatter.author_names(first)
    removed = [k for k, _s, _e in frontmatter.entries(first) if k in anonymize.STRIP_KEYS]
    mentions = []
    for name in names:
        surname = name.split()[-1] if name.split() else name
        if len(surname) < 3:
            continue
        pattern = re.compile(r"(?<![\w])" + re.escape(surname) + r"(?![\w])", re.I)
        for p, text in md.items():
            body_text = "\n".join(text.split("\n")[frontmatter.body_start(text):])
            offset = len(text) - len(body_text)
            for m in pattern.finditer(_scannable(body_text)):
                mentions.append(_item(p, _lines_of(text, offset + m.start()),
                                      _("“{name}” appears in the text", name=surname)))
    ack = any(re.search(r"^#{1,6}\s+(acknowledge?ments?|danksagung|funding)\b", t, re.I | re.M) for t in md.values())
    parts = []
    if removed:
        parts.append(_("Anonymize removes: {keys}.", keys=", ".join(removed)))
    if ack:
        parts.append(_("It also removes the acknowledgements."))
    if mentions:
        parts.append(_("Author names stay where the text mentions them; check these, or mark them with "
                       "[…]{.unblind}."))
    checks.append(_check("anonymity", _("Double-blind review"), "warn" if mentions else "info",
                         " ".join(parts) or _("Nothing names the authors."), mentions))
    return {"checks": checks, "words": body, "notes": notes, "pages": pages, "inputs": inputs}


def pdf_pages(data: bytes) -> int | None:
    """The page count of a PDF from its page tree root, when it is stored uncompressed."""
    counts = [int(m.group(1)) for m in re.finditer(rb"/Type\s*/Pages\b[^>]*?/Count\s+(\d+)", data)]
    counts += [int(m.group(1)) for m in re.finditer(rb"/Count\s+(\d+)[^>]*?/Type\s*/Pages\b", data)]
    return max(counts) if counts else None

"""Living editions (§14.4): a whole project published as a small static site.

The project's HTML render, one page with every chapter, becomes an index page (title,
contents, how to cite it, downloads) and a page per top-level section, with the layout's
styles and template kept around each. Pages get a table of contents (a side column on wide
screens, a disclosure on narrow ones), previous and next links, a light/dark reading switch,
and citations and footnotes that show their text on hover or focus. Footnotes move to the
page that refers to them; links between chapters point to the right page.

Everything is HTML and CSS: the site's CSP allows no scripts, and nothing here needs one.
"""

from __future__ import annotations

import html as htmllib
import re
from dataclasses import dataclass, field

_TAG = re.compile(r"<!--.*?-->|<(/?)([a-zA-Z][a-zA-Z0-9:-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>", re.S)
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_RAW = {"script", "style", "textarea", "title"}
_ATTR = re.compile(r"""([^\s=/>]+)(?:\s*=\s*("[^"]*"|'[^']*'|[^\s>]+))?""")


@dataclass
class Element:
    tag: str
    attrs: dict
    start: int        # where the start tag begins
    inner: int        # where the content begins
    end: int = -1     # where the end tag ends
    inner_end: int = -1
    parent: Element | None = None
    children: list = field(default_factory=list)

    @property
    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())


def parse_attrs(text: str) -> dict:
    out = {}
    for m in _ATTR.finditer(text):
        value = m.group(2) or ""
        if value[:1] in "\"'":
            value = value[1:-1]
        out[m.group(1).lower()] = htmllib.unescape(value)
    return out


def tree(page: str) -> Element:
    """The page's elements with their offsets. Tolerates what pandoc writes, not any HTML."""
    root = Element("#root", {}, 0, 0, len(page), len(page))
    stack = [root]
    pos = 0
    while True:
        m = _TAG.search(page, pos)
        if m is None:
            break
        pos = m.end()
        if m.group(0).startswith("<!--"):
            continue
        closing, tag, rest = m.group(1), m.group(2).lower(), m.group(3)
        if closing:
            for i in range(len(stack) - 1, 0, -1):
                if stack[i].tag == tag:
                    for el in stack[i:]:
                        el.end, el.inner_end = m.end(), m.start()
                    del stack[i:]
                    break
            continue
        el = Element(tag, parse_attrs(rest), m.start(), m.end(), parent=stack[-1])
        stack[-1].children.append(el)
        if tag in _VOID or rest.rstrip().endswith("/"):
            el.end = el.inner_end = m.end()
            continue
        if tag in _RAW:
            close = re.compile(r"</" + tag + r"\s*>", re.I).search(page, pos)
            el.inner_end = close.start() if close else len(page)
            el.end = close.end() if close else len(page)
            pos = el.end
            continue
        stack.append(el)
    for el in stack[1:]:
        el.end = el.inner_end = len(page)
    return root


def walk(el: Element):
    for child in el.children:
        yield child
        yield from walk(child)


def text_of(fragment: str) -> str:
    """Plain text: block elements become spaces, inline ones vanish."""
    fragment = re.sub(r"</?(?:p|div|li|ul|ol|br|hr|h\d|blockquote|section|table|tr|td|th|figure|figcaption)\b[^>]*>",
                      " ", fragment, flags=re.I)
    return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", "", fragment)).split())


def _esc(text: str) -> str:
    return htmllib.escape(text, quote=True)


@dataclass
class Chapter:
    file: str
    title: str
    html: str
    ids: set
    sections: list  # [(id, title)] of level-2 sections


def _slug(text: str, n: int) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "chapter"
    return f"{n:02d}-{base}.html"


CSS = """
.dw-mode { position: absolute; opacity: 0; width: 1px; height: 1px; }
.dw-bar { display: flex; flex-wrap: wrap; gap: .5rem 1.25rem; align-items: baseline; font: 14px/1.4 system-ui, sans-serif;
  margin: 0 0 1.5rem; padding: .5rem 0; border-bottom: 1px solid #8884; }
.dw-bar .dw-home { font-weight: 600; }
.dw-bar label { cursor: pointer; margin-left: auto; }
.dw-mode:focus-visible + .dw-bar label { outline: 2px solid #4da3ff; outline-offset: 2px; }
.dw-toc-narrow summary { cursor: pointer; }
.dw-toc ol, .dw-toc-narrow ol { margin: .25rem 0; padding-left: 1.25rem; }
.dw-toc li, .dw-toc-narrow li { margin: .2rem 0; }
.dw-toc [aria-current="page"], .dw-toc-narrow [aria-current="page"] { font-weight: 600; }
.dw-toc { display: none; }
@media (min-width: 84rem) {
  .dw-toc { display: block; position: fixed; top: 1rem; left: 1rem; width: 16rem; max-height: calc(100vh - 2rem);
    overflow: auto; font: 14px/1.4 system-ui, sans-serif; }
  .dw-toc-narrow { display: none; }
}
.dw-pager { display: flex; justify-content: space-between; gap: 1rem; margin-top: 3rem; padding-top: .75rem;
  border-top: 1px solid #8884; font: 14px/1.4 system-ui, sans-serif; }
.dw-foot { font: 13px/1.4 system-ui, sans-serif; opacity: .8; margin-top: 1rem; }
.dw-ref { position: relative; }
.dw-pop { display: none; position: absolute; left: 0; top: 1.5em; z-index: 10; width: min(24rem, 80vw);
  background: #fff; color: #1a1a1a; border: 1px solid #888; padding: .5em .75em; font: 14px/1.45 system-ui, sans-serif;
  text-align: left; text-indent: 0; font-style: normal; font-weight: normal; white-space: normal; }
.dw-ref:hover > .dw-pop, .dw-ref:focus-within > .dw-pop { display: block; }
.dw-cite textarea { width: 100%; font: 13px/1.4 ui-monospace, monospace; resize: vertical; }
.dw-downloads { display: flex; flex-wrap: wrap; gap: .5rem 1.25rem; padding: 0; list-style: none; }
html:has(.dw-mode:checked) { filter: invert(1) hue-rotate(180deg); background: #fff; }
html:has(.dw-mode:checked) :is(img, svg, video, picture) { filter: invert(1) hue-rotate(180deg); }
@media (prefers-color-scheme: dark) {
  html:not(:has(.dw-mode:checked)) { filter: invert(1) hue-rotate(180deg); background: #fff; }
  html:not(:has(.dw-mode:checked)) :is(img, svg, video, picture) { filter: invert(1) hue-rotate(180deg); }
}
@media print { .dw-bar, .dw-toc, .dw-toc-narrow, .dw-pager, .dw-foot { display: none; } }
"""


def _pop_footnotes(page: str, notes: dict[str, str]) -> str:
    """Footnote marks get their note's text beside them, shown on hover and focus."""
    def repl(m):
        attrs = parse_attrs(m.group(1))
        target = attrs.get("href", "")[1:]
        note = notes.get(target)
        if not note:
            return m.group(0)
        return f'<span class="dw-ref">{m.group(0)}<span class="dw-pop" role="note">{_esc(note)}</span></span>'
    return re.sub(r'<a\b([^>]*class="footnote-ref"[^>]*)>.*?</a>', repl, page, flags=re.S)


def _pop_citations(page: str, entries: dict[str, str]) -> str:
    def repl(m):
        attrs = parse_attrs(m.group(1))
        keys = attrs.get("data-cites", "").split()
        texts = [entries[k] for k in keys if k in entries]
        if not texts:
            return m.group(0)
        pop = "".join(f"<span>{_esc(t)}</span><br>" for t in texts)[:-4]
        return (f'<span class="dw-ref" tabindex="0"><span{m.group(1)}>{m.group(2)}</span>'
                f'<span class="dw-pop" role="note">{pop}</span></span>')
    return re.sub(r'<span\b([^>]*class="citation"[^>]*)>(.*?)</span>(?!</span>)', repl, page, flags=re.S)


def _rewrite_links(page: str, here: str, where: dict[str, str]) -> str:
    def repl(m):
        target = htmllib.unescape(m.group(2))
        file = where.get(target)
        if file is None or file == here:
            return m.group(0)
        return f'{m.group(1)}"{_esc(file)}#{m.group(2)}"'
    return re.sub(r'(\bhref=)"#([^"]+)"', repl, page)


@dataclass
class Edition:
    pages: dict  # file name -> HTML
    chapters: list


def build(page: str, *, title: str, lang: str = "en", cite: dict | None = None, downloads: list | None = None,
          report_url: str | None = None, gettext=None) -> Edition:
    """Split one rendered page into the edition's pages. *cite* is {"text", "bibtex"};
    *downloads* [(label, file)]; *report_url* the typo form, ``{page}`` filled in per page."""
    _ = gettext or (lambda message, **values: message.format(**values) if values else message)
    root = tree(page)
    body = next((e for e in walk(root) if e.tag == "body"), None)
    if body is None or page.find("</head>") < 0:
        raise ValueError("The rendered page has no head or body.")

    def top_sections(el):
        for child in el.children:
            if child.tag == "section" and "level1" in child.classes and child.attrs.get("id") != "footnotes":
                yield child
            elif child.tag not in ("section", "header", "nav", "footer"):
                yield from top_sections(child)

    chapters_el = list(top_sections(body))
    footnotes_el = next((e for e in walk(body) if e.tag == "section" and e.attrs.get("id") == "footnotes"), None)
    # Note texts and bibliography entries, for the hover pops.
    notes: dict[str, str] = {}
    note_items: dict[str, str] = {}
    if footnotes_el is not None:
        ol = next((c for c in footnotes_el.children if c.tag == "ol"), None)
        for li in (ol.children if ol else []):
            if li.tag == "li" and li.attrs.get("id"):
                item = page[li.start:li.end]
                note_items[li.attrs["id"]] = item
                notes[li.attrs["id"]] = text_of(re.sub(r'<a\b[^>]*class="footnote-back".*?</a>', "", item, flags=re.S))
    entries = {e.attrs["id"][4:]: text_of(page[e.inner:e.inner_end]) for e in walk(body)
               if "csl-entry" in e.classes and e.attrs.get("id", "").startswith("ref-")}

    if not chapters_el:
        single = _pop_citations(_pop_footnotes(page, notes), entries)
        return Edition({"index.html": _decorate(single, single.find("</head>"), "index.html", [], title, _, report_url,
                                                extra_end=_index_extras(cite, downloads, _))}, [])

    region_start, region_end = chapters_el[0].start, chapters_el[-1].end
    chapters: list[Chapter] = []
    for i, sec in enumerate(chapters_el):
        end = chapters_el[i + 1].start if i + 1 < len(chapters_el) else region_end
        fragment = page[sec.start:end]
        h1 = next((c for c in sec.children if c.tag in ("h1", "h2")), None)
        name = text_of(page[h1.inner:h1.inner_end]) if h1 else _("Chapter {n}", n=i + 1)
        ids = set(re.findall(r'\bid="([^"]+)"', fragment))
        subs = [(c.attrs.get("id", ""), text_of(page[h.inner:h.inner_end]))
                for c in sec.children if c.tag == "section" and "level2" in c.classes and c.attrs.get("id")
                for h in [next((x for x in c.children if x.tag in ("h2", "h3")), None)] if h]
        chapters.append(Chapter(_slug(name, i + 1), name, fragment, ids, subs))

    # The page around the chapters: everything before and after them, without the endnotes.
    before = page[:region_start]
    after = page[region_end:]
    if footnotes_el is not None and footnotes_el.start >= region_end:
        a, b = footnotes_el.start - region_end, footnotes_el.end - region_end
        after = after[:a] + after[b:]
    where: dict[str, str] = {}
    for ch in chapters:
        for i_ in ch.ids:
            where[i_] = ch.file
    for i_ in re.findall(r'\bid="([^"]+)"', before):
        where.setdefault(i_, "index.html")

    pages: dict[str, str] = {}
    toc = [(ch.file, ch.title) for ch in chapters]
    # The index: the title block and what comes before the first chapter, the contents, citing, downloads.
    contents = "<nav class=\"dw-contents\" aria-labelledby=\"dw-contents\"><h2 id=\"dw-contents\">" + _esc(_("Contents")) + \
        "</h2><ol>" + "".join(f'<li><a href="{_esc(f)}">{_esc(t)}</a></li>' for f, t in toc) + "</ol></nav>"
    index = before + contents + _index_extras(cite, downloads, _) + after
    index = _rewrite_links(_pop_citations(_pop_footnotes(index, notes), entries), "index.html", where)
    pages["index.html"] = _decorate(index, index.find("</head>"), "index.html", toc, title, _, report_url)
    # Chapter pages: the template around the chapters, one chapter, its notes.
    body_open = page.find(">", body.start) + 1
    wrapper_prefix = _wrappers(page, body, chapters_el[0])
    for n, ch in enumerate(chapters):
        refs = re.findall(r'href="#(fn\d+)"', ch.html)
        own = "".join(note_items[r] for r in dict.fromkeys(refs) if r in note_items)
        notes_html = (f'<section class="footnotes" role="doc-endnotes"><hr><ol>{_numbered(own)}</ol></section>'
                      if own else "")
        prev_link = f'<a rel="prev" href="{_esc(chapters[n - 1].file)}">← {_esc(chapters[n - 1].title)}</a>' if n else \
            f'<a rel="prev" href="index.html">← {_esc(_("Contents"))}</a>'
        next_link = f'<a rel="next" href="{_esc(chapters[n + 1].file)}">{_esc(chapters[n + 1].title)} →</a>' \
            if n + 1 < len(chapters) else "<span></span>"
        pager = f'<nav class="dw-pager" aria-label="{_esc(_("Chapters"))}">{prev_link}{next_link}</nav>'
        shell = page[:body_open] + wrapper_prefix[0] + ch.html + notes_html + pager + wrapper_prefix[1] + \
            page[body.inner_end:]
        shell = _rewrite_links(_pop_citations(_pop_footnotes(shell, notes), entries), ch.file, where)
        shell = re.sub(r"<title>.*?</title>", f"<title>{_esc(ch.title)} – {_esc(title)}</title>", shell, count=1,
                       flags=re.S)
        pages[ch.file] = _decorate(shell, shell.find("</head>"), ch.file, toc, title, _, report_url,
                                   subs=ch.sections)
    return Edition(pages, chapters)


def _wrappers(page: str, body: Element, first: Element) -> tuple[str, str]:
    """The start and end tags of the template's elements around the chapters (a <main>, say)."""
    chain = []
    el = first.parent
    while el is not None and el is not body:
        chain.append(el)
        el = el.parent
    chain.reverse()
    opening = "".join(page[e.start:e.inner] for e in chain)
    closing = "".join(f"</{e.tag}>" for e in reversed(chain))
    return opening, closing


def _numbered(items: str) -> str:
    return re.sub(r'<li id="fn(\d+)"', lambda m: f'<li id="fn{m.group(1)}" value="{m.group(1)}"', items)


def _index_extras(cite: dict | None, downloads: list | None, _) -> str:
    out = ""
    if downloads:
        out += (f'<section class="dw-download" aria-labelledby="dw-dl"><h2 id="dw-dl">{_esc(_("Download"))}</h2>'
                '<ul class="dw-downloads">' + "".join(f'<li><a href="{_esc(f)}" download>{_esc(label)}</a></li>'
                                                     for label, f in downloads) + "</ul></section>")
    if cite:
        out += (f'<section class="dw-cite" aria-labelledby="dw-cite"><h2 id="dw-cite">{_esc(_("Cite this work"))}</h2>'
                f'<textarea readonly rows="3" aria-label="{_esc(_("Citation"))}">{_esc(cite["text"])}</textarea>'
                f'<details><summary>BibTeX</summary><textarea readonly rows="7" aria-label="BibTeX">'
                f'{_esc(cite["bibtex"])}</textarea></details></section>')
    return out


def _decorate(page: str, head_end: int, here: str, toc: list, title: str, _, report_url: str | None,
              subs: list | None = None, extra_end: str = "") -> str:
    """Add the edition's styles, the bar with the reading switch, the contents and the footer."""
    page = page[:head_end] + f"<style>{CSS}</style>\n" + page[head_end:]
    items = []
    for f, t in toc:
        current = ' aria-current="page"' if f == here else ""
        sub = ""
        if f == here and subs:
            sub = "<ol>" + "".join(f'<li><a href="#{_esc(i)}">{_esc(s)}</a></li>' for i, s in subs) + "</ol>"
        items.append(f'<li><a href="{_esc(f)}"{current}>{_esc(t)}</a>{sub}</li>')
    toc_list = f"<ol>{''.join(items)}</ol>" if items else ""
    bar = ('<input type="checkbox" id="dw-mode" class="dw-mode">'
           f'<div class="dw-bar"><a class="dw-home" href="index.html">{_esc(title)}</a>'
           f'<label for="dw-mode">{_esc(_("Light or dark"))}</label></div>')
    if toc_list:
        bar += (f'<nav class="dw-toc" aria-label="{_esc(_("Contents"))}">{toc_list}</nav>'
                f'<details class="dw-toc-narrow"><summary>{_esc(_("Contents"))}</summary>{toc_list}</details>')
    foot = ""
    if report_url:
        foot = (f'<p class="dw-foot"><a href="{_esc(report_url.format(page=here))}">'
                f'{_esc(_("Found a mistake? Report a typo"))}</a></p>')
    m = re.search(r"<body\b[^>]*>", page)
    if m:
        page = page[:m.end()] + bar + page[m.end():]
    close = page.rfind("</body>")
    if close >= 0:
        page = page[:close] + extra_end + foot + page[close:]
    else:
        page += extra_end + foot
    return page


def cite_text(meta: dict, url: str) -> dict:
    """"Cite this work": a plain reference and BibTeX, from the title, authors and year."""
    authors = meta.get("authors") or []
    year = meta.get("year") or ""
    title = meta.get("title") or ""
    names = ", ".join(authors[:-1]) + (" and " if len(authors) > 1 else "") + (authors[-1] if authors else "")
    text = f"{names + '. ' if names else ''}{f'({year}). ' if year else ''}{title}. {url}".strip()
    key = re.sub(r"[^a-z0-9]", "", (authors[0].split()[-1] if authors else title.split()[0] if title else "work").lower())
    key = (key or "work") + str(year)
    fields = [("title", "{" + title + "}")]
    if authors:
        fields.insert(0, ("author", "{" + " and ".join(authors) + "}"))
    if year:
        fields.append(("year", "{" + str(year) + "}"))
    fields.append(("url", "{" + url + "}"))
    bibtex = "@misc{" + key + ",\n" + ",\n".join(f"  {k} = {v}" for k, v in fields) + "\n}"
    return {"text": text, "bibtex": bibtex}

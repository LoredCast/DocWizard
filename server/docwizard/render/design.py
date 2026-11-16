"""Layout designs: typed settings that decide how a layout renders (§9.3).

A layout's ``design`` is a flat object of settings such as ``text.font``,
``page.margin-top`` or ``heading.1.size``. Layouts inherit their parent's
design and override single keys. The layout editor edits these settings, so
a layout needs no template code of its own.

The settings reach the output through generated files that are written next
to the layout's templates when the layout is cached:

- ``typst/design.typ``: a Typst dictionary, loaded by the Typst template as
  the pandoc partial ``$design()$``. The template itself is static code.
- ``html/design.css``: CSS for the HTML preview and published pages.
- ``html/cover.html``: the title block's fields for the HTML template, as the
  pandoc partial ``$cover()$``.

Every value is validated against the schema below and written as a literal
of the target language, so a design can't inject template or Typst code.
"""

from __future__ import annotations

import copy
import hashlib
import html
import json
import re
from pathlib import Path

# -- fonts -------------------------------------------------------------------
# Typst runs with --ignore-system-fonts: it sees its embedded fonts, the fonts the
# worker image installs (deploy/fetch_tools.py) and a layout's own fonts/ folder.
# The CSS stacks make the HTML preview look close on machines without the font.
FONTS = {
    "Libertinus Serif": {"label": "Libertinus Serif", "kind": "serif",
                         "css": "'Libertinus Serif', 'Linux Libertine O', 'Linux Libertine', Georgia, serif"},
    "New Computer Modern": {"label": "New Computer Modern (LaTeX look)", "kind": "serif",
                            "css": "'New Computer Modern', 'Latin Modern Roman', 'CMU Serif', serif"},
    "Tinos": {"label": "Tinos (Times New Roman metrics)", "kind": "serif",
              "css": "Tinos, 'Times New Roman', Times, 'Liberation Serif', serif"},
    "Noto Serif": {"label": "Noto Serif", "kind": "serif",
                   "css": "'Noto Serif', 'Droid Serif', Georgia, serif"},
    "Noto Sans": {"label": "Noto Sans (Arial-like sans)", "kind": "sans",
                  "css": "'Noto Sans', Arial, 'Helvetica Neue', Helvetica, sans-serif"},
    "Carlito": {"label": "Carlito (Calibri metrics)", "kind": "sans",
                "css": "Carlito, Calibri, 'Segoe UI', sans-serif"},
    "DejaVu Sans Mono": {"label": "DejaVu Sans Mono", "kind": "mono",
                         "css": "'DejaVu Sans Mono', Menlo, Consolas, monospace"},
    "Cousine": {"label": "Cousine (Courier New metrics)", "kind": "mono",
                "css": "Cousine, 'Courier New', Courier, monospace"},
}
FALLBACK_FONT = {"serif": "Libertinus Serif", "sans": "Noto Sans", "mono": "DejaVu Sans Mono"}
SHARED_FONTS_DIR = "/usr/local/share/docwizard/fonts"


def font_css(name: str, kind: str = "serif") -> str:
    if name in FONTS:
        return FONTS[name]["css"]
    generic = {"serif": "serif", "sans": "sans-serif", "mono": "monospace"}[kind]
    return f"'{name}', {generic}"


# -- schema ------------------------------------------------------------------
_LENGTH = re.compile(r"^(?:0|\d{1,3}(?:\.\d{1,3})?(?:mm|cm|in|pt|em))$")
_FONT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .\-]{0,62}$")
_COLOR = re.compile(r"^[0-9a-fA-F]{6}$")
_LANG = re.compile(r"^[a-z]{2,3}(?:-[A-Z]{2})?$")

PAPERS = {"a4": "A4", "us-letter": "US Letter", "a5": "A5", "iso-b5": "B5"}
NUMBERING = {
    "": "None",
    "1": "1, 1.1, 1.1.1",
    "1.": "1., 1.1., 1.1.1.",
    "1.a": "1, 1.a, 1.a.i",
    "I.1": "I, I.1, I.1.1",
    "I.A.1": "I., A., 1. (one level each, Physical Review)",
    "trad": "I., 1., a) (traditional)",
    "ieee": "I., A., 1), a) (IEEE)",
}
CASES = {"normal": "As written", "upper": "UPPERCASE", "smallcaps": "Small caps"}
WEIGHTS = {"bold": "Bold", "semibold": "Semibold", "regular": "Regular"}
STYLES = {"normal": "Upright", "italic": "Italic"}
ALIGNS = {"left": "Left", "center": "Centred"}
RUN_IN = {"": "On a line of its own", "period": "Run into the text, ending with a full stop",
          "colon": "Run into the text, ending with a colon"}
# Terms DocWizard writes in the document's language (filters/locales.json).
BIB_TERMS = {"bibliography": "Bibliography", "references": "References", "works-cited": "Works Cited",
             "reference-list": "Reference List", "sources": "Sources"}
DATE_FORMATS = {"long": "In the language's long form (November 16, 2026; 16. November 2026)",
                "day-month-year": "Day, month, year (16 November 2026)",
                "numeric": "Numeric, in the language's order (11/16/2026; 16.11.2026)",
                "iso": "ISO (2026-11-16)", "as-written": "As written in the front matter"}

# Front matter fields a title block or title page can show, in the editor's order.
COVER_KEYS = {
    "author": "Authors",
    "affiliation": "Authors' affiliations",
    "email": "Authors' email addresses",
    "date": "Date",
    "paper-type": "Type of paper (paper-type)",
    "course": "Course (course)",
    "instructor": "Instructor (instructor)",
    "supervisor": "Supervisor (supervisor)",
    "semester": "Semester or term (semester)",
    "department": "Department or institute (department)",
    "faculty": "Faculty (faculty)",
    "institution": "Institution (institution)",
    "student-id": "Student ID (student-id)",
    "program": "Degree programme (program)",
    "place": "Place (place)",
    "place-date": "Place and date",
    "keywords": "Keywords",
    "abstract": "Abstract (here instead of after the fields)",
    "byline": "Byline (“by” and the authors, in the document's language)",
    "address": "Postal address (address)",
    "phone": "Telephone (phone)",
    "word-count": "Word count, rounded (“about 80,000 words”)",
    "genre": "Genre or format (genre)",
    "draft": "Draft or version (draft)",
    "contact": "Contact or agent (contact)",
    "submission": "Submitted for (submission)",
    "birthplace": "Place of birth (birthplace)",
}
COVER_STYLES = {"normal": "Regular", "bold": "Bold", "italic": "Italic", "small": "Small", "large": "Large"}


def _field(key, ftype, default, label, group, **extra):
    return {"key": key, "type": ftype, "default": default, "label": label, "group": group, **extra}


def _heading_fields(level: int, size: float, weight: str, above: str, below: str) -> list[dict]:
    g = f"heading-{level}"
    extra = []
    if level >= 3:
        extra = [
            _field(f"heading.{level}.indent", "length", "0", "Indent", g),
            _field(f"heading.{level}.run-in", "enum", "", "Placement", g, values=RUN_IN,
                   help="APA levels 4 and 5 and IEEE's third level run into their paragraph."),
        ]
    return [*extra,
        _field(f"heading.{level}.size", "number", size, "Size (× body text)", g, min=0.7, max=3, step=0.05),
        _field(f"heading.{level}.weight", "enum", weight, "Weight", g, values=WEIGHTS),
        _field(f"heading.{level}.style", "enum", "normal", "Style", g, values=STYLES),
        _field(f"heading.{level}.case", "enum", "normal", "Letter case", g, values=CASES),
        _field(f"heading.{level}.align", "enum", "left", "Alignment", g, values=ALIGNS),
        _field(f"heading.{level}.above", "length", above, "Space above", g),
        _field(f"heading.{level}.below", "length", below, "Space below", g),
    ]


GROUPS = [
    ("page", "Page", "Paper, margins, columns and page numbers."),
    ("text", "Body text", "Font, size, line spacing and paragraphs."),
    ("headings", "Headings", "Font and numbering for all headings."),
    ("heading-1", "Heading level 1", ""),
    ("heading-2", "Heading level 2", ""),
    ("heading-3", "Heading level 3", ""),
    ("heading-4", "Heading level 4 and below", ""),
    ("cover", "Title and title page", "What appears with the title, and whether it gets a page of its own."),
    ("declaration", "Declaration page", "A page after the title page, for example a declaration of authorship."),
    ("toc", "Table of contents", ""),
    ("quote", "Block quotations", ""),
    ("footnote", "Footnotes", ""),
    ("cite", "Citations and bibliography", ""),
    ("caption", "Figures and tables", ""),
    ("script", "Scripts", "Screenplays and two-column A/V scripts: where scene headings, characters, dialogue and "
                          "transitions sit."),
]

FIELDS: list[dict] = [
    # Page
    _field("page.paper", "enum", "a4", "Paper size", "page", values=PAPERS),
    _field("page.margin-top", "length", "2.5cm", "Top margin", "page"),
    _field("page.margin-bottom", "length", "2.5cm", "Bottom margin", "page"),
    _field("page.margin-left", "length", "2.5cm", "Left margin", "page"),
    _field("page.margin-right", "length", "2.5cm", "Right margin", "page"),
    _field("page.columns", "enum", "1", "Columns", "page", values={"1": "One column", "2": "Two columns"}),
    _field("page.column-gap", "length", "1.5em", "Space between columns", "page"),
    _field("page.number-format", "enum", "1", "Page numbers", "page",
           values={"1": "1, 2, 3", "1.": "1., 2., 3. (screenplays)", "- 1 -": "- 1 -", "1 / N": "1 / 12",
                   "i": "i, ii, iii", "none": "None"}),
    _field("page.number-position", "enum", "bottom-center", "Page number position", "page",
           values={"bottom-center": "Bottom, centred", "bottom-right": "Bottom right", "bottom-left": "Bottom left",
                   "top-right": "Top right", "top-center": "Top, centred"}),
    _field("page.number-distance", "length", "", "Page number distance from the page edge", "page", optional=True,
           help="Empty: set by the margin."),
    _field("page.number-first", "boolean", True, "Number on the first page", "page",
           help="Off: the first page (after a title page, the first page of text) shows no number."),
    _field("page.number-label", "enum", "none", "Before the page number", "page",
           values={"none": "Nothing", "surname": "First author's surname (MLA: Doe 4)",
                   "surname-title": "Surname / short title / (manuscripts: Doe / Silvertide / 4)"},
           help="The short title is short-title: in the front matter, else the title."),
    _field("page.front-numbering", "enum", "", "Front matter page numbers", "page",
           values={"": "Like the text", "i": "i, ii, iii, then 1 for the text", "I": "I, II, III, then 1 for the text"},
           help="Title page, declaration, contents and lists before the first heading of the text."),
    _field("page.back-numbering", "boolean", False, "Bibliography and appendix continue the front matter numbers",
           "page", help="As some German universities ask: text 1, 2, 3; bibliography and appendix go on from the "
                        "Roman numbers of the front matter."),
    _field("page.header", "enum", "none", "Running header", "page",
           values={"none": "None", "title": "Title", "author": "Authors", "author-title": "Authors and title"}),
    _field("page.header-rule", "boolean", False, "Line under the header", "page"),
    # Body text
    _field("text.font", "font", "Libertinus Serif", "Font", "text", kind="serif"),
    _field("text.size", "number", 11, "Font size (pt)", "text", min=8, max=16, step=0.5),
    _field("text.leading", "number", 1.25, "Line spacing", "text", min=0.8, max=3, step=0.05,
           help="1 is single spacing, 1.5 one-and-a-half, 2 double, as in word processors."),
    _field("text.paragraph-spacing", "length", "0.6em", "Space between paragraphs", "text",
           help="0 for none; with an indent, most styles use none."),
    _field("text.indent", "length", "0", "First-line indent", "text"),
    _field("text.justify", "boolean", True, "Justified text", "text"),
    _field("text.hyphenate", "boolean", True, "Hyphenation", "text"),
    _field("text.lang", "lang", "", "Default language", "text",
           help="Used when the document doesn't set lang:, for hyphenation, quotation marks and citation terms. "
                "For example en-GB or de-AT."),
    _field("text.indent-first", "boolean", False, "Indent the first paragraph after a heading", "text",
           help="APA, MLA and manuscripts indent every paragraph; many book styles don't indent the first."),
    _field("text.quotes", "enum", "language", "Quotation marks", "text",
           values={"language": "The language's own („…“ in German, «…» in French)",
                   "single": "Single first (‘…“…”…’), as MHRA asks",
                   "double": "Double first (“…‘…’…”)"},
           help="Single and double first apply to the document's main language; other passages keep theirs."),
    _field("text.date-format", "enum", "long", "Dates", "text", values=DATE_FORMATS,
           help="How an ISO date in the front matter (date: 2026-11-16) is written, in the document's language."),
    _field("text.scene-break", "enum", "rule", "Scene break (*** or ---)", "text",
           values={"rule": "A short line", "#": "#, centred (manuscripts)", "* * *": "* * *, centred",
                   "blank": "A blank line"}),
    _field("text.code-font", "font", "DejaVu Sans Mono", "Code font", "text", kind="mono"),
    _field("text.link-color", "color", "", "Link colour", "text", help="Hex colour such as 1a4fa0; empty for none."),
    # Headings
    _field("heading.font", "font", "", "Heading font", "headings", kind="serif", help="Empty: the body font."),
    _field("heading.numbering", "enum", "", "Numbering", "headings", values=NUMBERING),
    _field("heading.chapter-break", "boolean", False, "Start level 1 headings on a new page", "headings"),
    *_heading_fields(1, 1.4, "bold", "1.4em", "0.7em"),
    *_heading_fields(2, 1.2, "bold", "1.2em", "0.6em"),
    *_heading_fields(3, 1.05, "bold", "1em", "0.5em"),
    *_heading_fields(4, 1, "bold", "1em", "0.4em"),
    # Title and title page
    _field("cover.page", "boolean", False, "Title page", "cover",
           help="On: the title gets a page of its own. Off: the title block opens the first page."),
    _field("cover.align", "enum", "center", "Alignment", "cover", values=ALIGNS),
    _field("cover.position", "enum", "third", "Position on the title page", "cover",
           values={"top": "Top", "third": "A third down", "center": "Centred"}),
    _field("cover.title-align", "enum", "", "Title alignment", "cover",
           values={"": "Like the fields", "left": "Left", "center": "Centred"}),
    _field("cover.fields-first", "boolean", False, "Fields above the title", "cover",
           help="MLA puts the name, instructor, course and date above the title."),
    _field("cover.offset", "length", "0", "Space above the title block", "cover",
           help="Without a title page: how far down the first page the title starts (manuscripts: a third down)."),
    _field("cover.corner-left", "cover", [], "Top left of the first page", "cover",
           help="Manuscripts: the contact block. Single-spaced."),
    _field("cover.corner-right", "cover", [], "Top right of the first page", "cover",
           help="Manuscripts: the word count."),
    _field("cover.title-size", "number", 1.6, "Title size (× body text)", "cover", min=1, max=3, step=0.05),
    _field("cover.title-weight", "enum", "bold", "Title weight", "cover", values=WEIGHTS),
    _field("cover.title-case", "enum", "normal", "Title letter case", "cover", values=CASES),
    _field("cover.subtitle-size", "number", 1.2, "Subtitle size (× body text)", "cover", min=0.8, max=2.5, step=0.05),
    _field("cover.subtitle-weight", "enum", "regular", "Subtitle weight", "cover", values=WEIGHTS),
    _field("cover.title-colon", "boolean", False, "Colon after the main title", "cover",
           help="Adds a colon after the title when the document has a subtitle, as Turabian asks."),
    _field("cover.fields", "cover", [{"key": "author"}, {"key": "date"}], "Fields under the title", "cover",
           help="Shown in this order when the document's front matter has them."),
    _field("cover.field-spacing", "length", "0.4em", "Space between title fields", "cover"),
    _field("cover.abstract", "boolean", True, "Show the abstract", "cover"),
    _field("cover.abstract-page", "boolean", False, "Abstract on a page of its own", "cover",
           help="After the title page, under a centred heading (APA)."),
    _field("cover.abstract-separator", "enum", "space", "Between the abstract heading and text", "cover",
           values={"space": "A space", "em-dash": "An em dash (IEEE: Abstract—)", "break": "A line break"}),
    _field("cover.repeat-title", "boolean", False, "Repeat the title on the first page of text", "cover",
           help="APA repeats the title, centred and bold, above the text."),
    _field("cover.abstract-title", "text", "", "Abstract heading", "cover", help="Empty: “Abstract” in the "
                                                                                  "document's language."),
    _field("cover.counted", "boolean", True, "Count the title page", "cover",
           help="On: the first page after the title page is page 2. Off: it is page 1."),
    # Declaration
    _field("declaration.enabled", "boolean", False, "Add a declaration page", "declaration"),
    _field("declaration.title", "text", "Declaration", "Heading", "declaration"),
    _field("declaration.text", "longtext", "I declare that I wrote this paper myself and used only the sources and "
           "aids I have named.", "Text", "declaration",
           help="Plain text. A document can replace it with declaration: in its front matter."),
    _field("declaration.signature", "text", "Place, date, signature", "Signature line", "declaration"),
    # Table of contents
    _field("toc.enabled", "boolean", False, "Table of contents", "toc"),
    _field("toc.title", "text", "", "Heading", "toc", help="Empty: “Contents” in the document's language."),
    _field("toc.depth", "number", 3, "Levels", "toc", min=1, max=4, step=1),
    _field("toc.page", "boolean", False, "On a page of its own", "toc"),
    _field("toc.figures", "boolean", False, "List of figures", "toc",
           help="After the contents, headed in the document's language."),
    _field("toc.tables", "boolean", False, "List of tables", "toc"),
    # Block quotations
    _field("quote.size", "number", 1, "Size (× body text)", "quote", min=0.7, max=1.2, step=0.05),
    _field("quote.leading", "number", 0, "Line spacing", "quote", min=0, max=3, step=0.05,
           help="0: the same as the body text."),
    _field("quote.indent-left", "length", "2em", "Left indent", "quote"),
    _field("quote.indent-right", "length", "2em", "Right indent", "quote"),
    _field("quote.italic", "boolean", False, "Italic", "quote"),
    _field("quote.spacing", "length", "", "Space before and after", "quote", optional=True,
           help="Empty: the paragraph spacing."),
    # Footnotes
    _field("footnote.size", "number", 0.85, "Size (× body text)", "footnote", min=0.6, max=1.2, step=0.05),
    _field("footnote.leading", "number", 1, "Line spacing", "footnote", min=0, max=3, step=0.05,
           help="0: the same as the body text."),
    _field("footnote.indent", "length", "1em", "Indent", "footnote"),
    _field("footnote.number", "enum", "super", "Number in the note", "footnote",
           values={"super": "Superscript (¹Text)", "super-space": "Superscript and a space (¹ Text)",
                   "period": "Number and full stop (1. Text)"}),
    _field("footnote.rule", "boolean", True, "Line above the footnotes", "footnote"),
    _field("footnote.spacing", "length", "0.5em", "Space between footnotes", "footnote"),
    # Citations and bibliography
    _field("cite.style", "citation-style", "chicago-author-date", "Citation style", "cite"),
    _field("cite.link", "boolean", True, "Link citations to the bibliography", "cite"),
    _field("bib.heading", "enum", "none", "Bibliography heading", "cite", values={"none": "None", **BIB_TERMS},
           help="Written in the document's language: Bibliography becomes Literaturverzeichnis in German."),
    _field("bib.heading-author-date", "enum", "", "Heading with an author-date style", "cite",
           values={"": "The same", **BIB_TERMS},
           help="Used when the chosen citation style puts author and year in the text."),
    _field("bib.title", "text", "", "Fixed bibliography heading", "cite",
           help="Replaces the heading above in every language. Empty: the heading above, unless the document "
                "sets reference-section-title."),
    _field("bib.title-author-date", "text", "", "Fixed heading with an author-date style", "cite",
           help="Empty: the heading with an author-date style above."),
    _field("bib.heading-style", "enum", "heading", "Heading style", "cite",
           values={"heading": "Like a level 1 heading", "center": "Centred, like a level 1 heading",
                   "center-plain": "Centred, regular weight (MLA)"}),
    _field("bib.page-break", "boolean", False, "Bibliography on a new page", "cite"),
    _field("bib.size", "number", 1, "Size (× body text)", "cite", min=0.7, max=1.2, step=0.05),
    _field("bib.leading", "number", 0, "Line spacing", "cite", min=0, max=3, step=0.05,
           help="0: the same as the body text."),
    _field("bib.hanging", "length", "2em", "Hanging indent", "cite"),
    _field("bib.spacing", "length", "0.4em", "Extra space between entries", "cite"),
    # Figures and tables
    _field("caption.figure", "text", "", "Figure label", "caption", help="Empty: “Figure” in the document's language."),
    _field("caption.table", "text", "", "Table label", "caption", help="Empty: “Table” in the document's language."),
    _field("caption.table-numbering", "enum", "1", "Table numbers", "caption", values={"1": "1, 2, 3", "I": "I, II, III"}),
    _field("caption.separator", "enum", ": ", "After the number", "caption",
           values={": ": "Colon (Figure 1: …)", ". ": "Full stop (Figure 1. …)", " – ": "Dash (Figure 1 – …)"}),
    _field("caption.size", "number", 0.9, "Size (× body text)", "caption", min=0.7, max=1.2, step=0.05),
    _field("caption.align", "enum", "center", "Caption alignment", "caption", values={"center": "Centred", "left": "Left"}),
    _field("caption.leading", "number", 0, "Caption line spacing", "caption", min=0, max=3, step=0.05,
           help="0: the same as the body text."),
    _field("caption.table-position", "enum", "top", "Table captions", "caption", values={"top": "Above", "bottom": "Below"}),
    _field("caption.figure-position", "enum", "bottom", "Figure captions", "caption",
           values={"top": "Above", "bottom": "Below"}),
    _field("caption.label-weight", "enum", "regular", "Label weight", "caption", values=WEIGHTS),
    _field("caption.break", "boolean", False, "Caption text on a line of its own", "caption",
           help="APA: “Figure 1” in bold, the title in italics on the next line. IEEE tables: “TABLE I”, "
                "then the title."),
    _field("caption.title-style", "enum", "normal", "Caption text style", "caption", values=STYLES),
    _field("caption.title-case", "enum", "normal", "Caption text letter case", "caption", values=CASES),
    _field("caption.by-chapter", "boolean", False, "Number by chapter (1.1, 1.2, 2.1)", "caption"),
    # Scripts
    _field("script.character-indent", "length", "2.2in", "Character name: indent", "script",
           help="From the left margin."),
    _field("script.dialogue-indent", "length", "1in", "Dialogue: indent", "script"),
    _field("script.dialogue-width", "length", "3.5in", "Dialogue: width", "script"),
    _field("script.parenthetical-indent", "length", "1.6in", "Parenthetical: indent", "script"),
    _field("script.parenthetical-width", "length", "2in", "Parenthetical: width", "script"),
    _field("script.scene-weight", "enum", "regular", "Scene heading weight", "script", values=WEIGHTS),
    _field("script.scene-numbers", "boolean", False, "Number the scenes", "script",
           help="Shooting scripts number scenes; spec scripts don't."),
    _field("script.scene-split", "boolean", False, "Interior or exterior and time of day flush right", "script",
           help="For “Kitchen – interior / day” headings: the part after the last dash goes to the right "
                "margin, as German Drehbücher do."),
    _field("script.video-label", "text", "VIDEO", "A/V script: left column heading", "script"),
    _field("script.audio-label", "text", "AUDIO", "A/V script: right column heading", "script"),
    _field("script.video-width", "number", 0.45, "A/V script: left column share", "script", min=0.2, max=0.8,
           step=0.05),
    _field("script.row-numbers", "boolean", True, "A/V script: number the rows", "script"),
    _field("script.end-mark", "text", "", "End mark", "script",
           help="Centred after the last line, for example THE END or ENDE. Empty: none."),
]
FIELD = {f["key"]: f for f in FIELDS}
DEFAULTS = {f["key"]: f["default"] for f in FIELDS}

# Classic layout options that override a design setting. Their default comes from the design.
OPTION_KEYS = {
    "papersize": "page.paper",
    "fontsize": "text.size",
    "linestretch": "text.leading",
    "section-numbering": "heading.numbering",
    "toc": "toc.enabled",
    "columns": "page.columns",
    "titlepage": "cover.page",
    "citation-style": "cite.style",
}
_SECTION_NUMBERING = {"1.1": "1", "1.a": "1.a", "I.1": "I.1", "": ""}


def option_value(option: str, design: dict):
    """The value a classic layout option has when it isn't set: the design's."""
    value = design[OPTION_KEYS[option]]
    if option == "fontsize":
        return f"{value:g}pt"
    if option == "section-numbering":
        return {v: k for k, v in _SECTION_NUMBERING.items()}.get(value, value)
    return value


def design_value(option: str, value):
    """Translate a classic option value into the design setting it overrides."""
    if option == "fontsize" and isinstance(value, str) and value.endswith("pt"):
        try:
            return float(value[:-2])
        except ValueError:
            return None
    if option == "section-numbering":
        return _SECTION_NUMBERING.get(value, value)
    return value


class DesignError(ValueError):
    pass


def _check(key: str, value, styles: set[str] | None):
    spec = FIELD.get(key)
    if spec is None:
        raise DesignError(f"Unknown design setting '{key}'.")
    kind, label = spec["type"], spec["label"]
    if kind == "enum":
        if value not in spec["values"]:
            raise DesignError(f"{label}: choose one of the offered values.")
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise DesignError(f"{label} must be a number.")
        if value < spec["min"] or value > spec["max"]:
            raise DesignError(f"{label} must be between {spec['min']:g} and {spec['max']:g}.")
        value = round(float(value), 3)
        value = int(value) if value.is_integer() and spec.get("step", 1) >= 1 else value
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise DesignError(f"{label} must be on or off.")
    elif kind == "length":
        if spec.get("optional") and value == "":
            return value
        if not isinstance(value, str) or not _LENGTH.match(value.strip()):
            raise DesignError(f"{label} must be a length such as 2.5cm, 1in, 12pt or 1.5em.")
        value = value.strip()
    elif kind == "font":
        if value != "" and (not isinstance(value, str) or not _FONT.match(value)):
            raise DesignError(f"{label}: font names use letters, digits, spaces, dots and hyphens.")
    elif kind == "color":
        if value != "" and (not isinstance(value, str) or not _COLOR.match(value.lstrip("#"))):
            raise DesignError(f"{label} must be a six-digit hex colour such as 1a4fa0.")
        value = value.lstrip("#").lower()
    elif kind == "lang":
        if value != "" and (not isinstance(value, str) or not _LANG.match(value)):
            raise DesignError(f"{label} must be a language tag such as en-GB or de-AT.")
    elif kind in ("text", "longtext"):
        limit = 200 if kind == "text" else 4000
        if not isinstance(value, str) or len(value) > limit:
            raise DesignError(f"{label} must be text up to {limit} characters.")
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        if kind == "text":
            value = value.replace("\n", " ").strip()
    elif kind == "citation-style":
        if not isinstance(value, str) or (styles is not None and value not in styles):
            raise DesignError(f"{label}: unknown citation style.")
    elif kind == "cover":
        value = _check_cover(value)
    return value


def _check_cover(items) -> list[dict]:
    if not isinstance(items, list) or len(items) > 30:
        raise DesignError("The title fields must be a list of at most 30 entries.")
    out = []
    for item in items:
        if not isinstance(item, dict):
            raise DesignError("Each title field must be an object.")
        style = item.get("style", "normal")
        if style not in COVER_STYLES:
            raise DesignError("Unknown style for a title field.")
        if "key" in item:
            if item["key"] not in COVER_KEYS:
                raise DesignError(f"Unknown title field '{item['key']}'.")
            label = item.get("label", "")
            if not isinstance(label, str) or len(label) > 80:
                raise DesignError("A title field's label must be text up to 80 characters.")
            entry = {"key": item["key"]}
            if label.strip():
                entry["label"] = label.strip()
        elif "text" in item:
            text = item["text"]
            if not isinstance(text, str) or not text.strip() or len(text) > 200:
                raise DesignError("Fixed text in the title must be up to 200 characters.")
            entry = {"text": text.strip()}
        elif "space" in item:
            space = item["space"]
            if not isinstance(space, str) or not (space == "fill" or _LENGTH.match(space)):
                raise DesignError("A gap in the title must be a length such as 2em or 1cm, or fill.")
            entry = {"space": space}
        else:
            raise DesignError("A title field needs key, text or space.")
        if style != "normal" and "space" not in entry:
            entry["style"] = style
        out.append(entry)
    return out


def validate(design, styles: set[str] | None = None) -> dict:
    """Check a (partial) design; returns the cleaned settings. Raises DesignError."""
    if design is None:
        return {}
    if not isinstance(design, dict):
        raise DesignError("The design must be an object.")
    return {key: _check(key, value, styles) for key, value in design.items()}


def complete(design: dict | None) -> dict:
    """The full design: defaults overridden by *design*. Invalid keys are dropped."""
    out = copy.deepcopy(DEFAULTS)
    for key, value in (design or {}).items():
        if key in FIELD:
            try:
                out[key] = _check(key, value, None)
            except DesignError:
                continue
    return out


def schema(styles: dict[str, str]) -> dict:
    """The schema for the layout editor."""
    fields = []
    for f in FIELDS:
        f = dict(f)
        if f["type"] == "citation-style":
            f["values"] = dict(styles)
        if f["type"] == "font":
            f["fonts"] = {name: info["label"] for name, info in FONTS.items()
                          if info["kind"] == f.get("kind") or (f.get("kind") == "serif" and info["kind"] == "sans")}
        fields.append(f)
    return {"groups": [{"id": g, "label": label, "help": h} for g, label, h in GROUPS], "fields": fields,
            "coverKeys": COVER_KEYS, "coverStyles": COVER_STYLES, "options": OPTION_KEYS}


# -- generated files -----------------------------------------------------------
def _typ_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t") + '"'


def _typ_space(value: str) -> str:
    return "1fr" if value == "fill" else ("0pt" if value == "0" else value)


def _typ(value, kind: str) -> str:
    if kind == "boolean":
        return "true" if value else "false"
    if kind == "number":
        return repr(float(value)) if isinstance(value, float) else str(value)
    if kind == "length":
        return "none" if value == "" else "0pt" if value == "0" else value
    if kind == "cover":
        parts = []
        for item in value:
            pairs = ", ".join(f"{k}: {_typ_space(v) if k == 'space' else _typ_str(v)}" for k, v in item.items())
            parts.append(f"({pairs})")
        return "(" + ", ".join(parts) + ("," if len(parts) == 1 else "") + ")"
    return _typ_str(str(value))


def typst_design(design: dict) -> str:
    """The Typst partial: a dictionary named dw. Pandoc reads it as template text, so $ is doubled."""
    lines = ["// Generated from the layout's design settings. Edit the layout instead.", "#let dw = ("]
    for f in FIELDS:
        key = f["key"].replace(".", "-")
        lines.append(f'  "{key}": {_typ(design[f["key"]], f["type"])},')
    lines.append(")")
    return "\n".join(lines).replace("$", "$$") + "\n"


def _css_len(value: str) -> str:
    return "0" if value == "0" else value


_ROMAN = "upper-roman"


def _counter_css(numbering: str) -> list[str]:
    """CSS counters that match the Typst heading numbering."""
    if not numbering:
        return []
    fmt = {
        "1": ("decimal", "decimal", "decimal", ".", ""),
        "1.": ("decimal", "decimal", "decimal", ".", "."),
        "1.a": ("decimal", "lower-alpha", "lower-roman", ".", ""),
        "I.1": (_ROMAN, "decimal", "decimal", ".", ""),
    }
    out = ["body { counter-reset: dwh1; }", "h1:not(.title) { counter-reset: dwh2; }", "h2 { counter-reset: dwh3; }"]
    sel = {1: "h1:not(.title):not(.unnumbered)", 2: "h2:not(.unnumbered)", 3: "h3:not(.unnumbered)"}
    for lvl in (1, 2, 3):
        out.append(f"{sel[lvl]} {{ counter-increment: dwh{lvl}; }}")
    if numbering in fmt:
        a, b, c, sep, end = fmt[numbering]
        parts = [f"counter(dwh1, {a})", f"counter(dwh2, {b})", f"counter(dwh3, {c})"]
        for lvl in (1, 2, 3):
            content = f' "{sep}" '.join(parts[:lvl])
            out.append(f'{sel[lvl]}::before {{ content: {content} "{end}\\a0\\a0"; }}')
    elif numbering == "I.A.1":
        styles = {1: (_ROMAN, "."), 2: ("upper-alpha", "."), 3: ("decimal", ".")}
        for lvl, (style, end) in styles.items():
            out.append(f'{sel[lvl]}::before {{ content: counter(dwh{lvl}, {style}) "{end}\\a0\\a0"; }}')
    elif numbering == "trad":
        styles = {1: (_ROMAN, "."), 2: ("decimal", "."), 3: ("lower-alpha", ")")}
        for lvl, (style, end) in styles.items():
            out.append(f'{sel[lvl]}::before {{ content: counter(dwh{lvl}, {style}) "{end}\\a0\\a0"; }}')
    return out


def _case_css(case: str) -> str:
    return {"upper": "text-transform: uppercase; letter-spacing: 0.02em;",
            "smallcaps": "font-variant: small-caps;"}.get(case, "")


_WEIGHT_CSS = {"bold": "700", "semibold": "600", "regular": "400"}


def css_design(design: dict) -> str:
    d = design
    body_font = font_css(d["text.font"] or "Libertinus Serif")
    head_font = font_css(d["heading.font"]) if d["heading.font"] else "inherit"
    size = d["text.size"]
    # Typst's leading is the gap between lines; CSS line-height includes the font. Word's "single"
    # spacing is about 1.15 times the font size, so the factor keeps 1.5 and 2 close to Word's.
    line = round(0.65 * d["text.leading"] + 1.0, 3)
    indent = _css_len(d["text.indent"])
    par_space = _css_len(d["text.paragraph-spacing"])
    out = [
        "/* Generated from the layout's design settings. Edit the layout instead. */",
        f"body {{ font-family: {body_font}; font-size: {size:g}pt; line-height: {line};"
        f" text-align: {'justify' if d['text.justify'] else 'left'}; hyphens: {'auto' if d['text.hyphenate'] else 'manual'};"
        f" max-width: {'52rem' if d['page.columns'] == '2' else '42rem'}; }}",
        f"p {{ margin: 0 0 {par_space}; }}",
    ]
    if indent != "0":
        out.append(f"p + p {{ text-indent: {indent}; }}")
        if d["text.indent-first"]:
            out.append(f"section > p, article > p, p:first-child {{ text-indent: {indent}; }}"
                       " .title-block p, .abstract p, figure p, .footnotes p { text-indent: 0; }")
    out.append(f"pre, code {{ font-family: {font_css(d['text.code-font'] or 'DejaVu Sans Mono', 'mono')}; }}")
    if d["text.link-color"]:
        out.append(f"a {{ color: #{d['text.link-color']}; }}")
    else:
        out.append("a { color: inherit; }")
    for lvl in (1, 2, 3, 4):
        p = f"heading.{lvl}."
        sel = {1: "h1:not(.title)", 2: "h2", 3: "h3", 4: "h4, h5, h6"}[lvl]
        indent = f" padding-left: {_css_len(d[p + 'indent'])};" if lvl >= 3 and d[p + "indent"] != "0" else ""
        out.append(
            f"{sel} {{ font-family: {head_font}; font-size: {d[p + 'size']:g}em; font-weight: {_WEIGHT_CSS[d[p + 'weight']]};"
            f" font-style: {d[p + 'style']}; text-align: {d[p + 'align']}; margin: {_css_len(d[p + 'above'])} 0"
            f" {_css_len(d[p + 'below'])}; line-height: 1.25;{indent} {_case_css(d[p + 'case'])} }}")
        if lvl >= 3:
            out.append(f".run-in-{lvl} {{ font-weight: {_WEIGHT_CSS[d[p + 'weight']]}; font-style: {d[p + 'style']};"
                       f" {_case_css(d[p + 'case'])} }}")
    out += _counter_css(d["heading.numbering"])
    # Title block
    align = d["cover.align"]
    out += [
        f".title-block {{ text-align: {align}; margin-bottom: 2.5em; }}",
        f".title-block .title {{ font-family: {head_font}; font-size: {d['cover.title-size']:g}em;"
        f" font-weight: {_WEIGHT_CSS[d['cover.title-weight']]}; margin: 0 0 0.3em; {_case_css(d['cover.title-case'])} }}",
        f".title-block .subtitle {{ font-size: {d['cover.subtitle-size']:g}em;"
        f" font-weight: {_WEIGHT_CSS[d['cover.subtitle-weight']]}; margin: 0 0 1em; }}",
        ".title-block .cover-field { margin: 0.2em 0; }",
        ".cover-bold { font-weight: 700; } .cover-italic { font-style: italic; }"
        " .cover-small { font-size: 0.85em; } .cover-large { font-size: 1.2em; }",
    ]
    if d["cover.title-align"]:
        out.append(f".title-block .title, .title-block .subtitle {{ text-align: {d['cover.title-align']}; }}")
    out.append(".corners { display: flex; justify-content: space-between; line-height: 1.2; margin-bottom: 2em; }"
               " .corners p { margin: 0; text-indent: 0; } .corners .corner-right { text-align: right; }")
    if d["cover.offset"] != "0":
        out.append(f".title-block {{ margin-top: {_css_len(d['cover.offset'])}; }}")
    if d["cover.repeat-title"] and d["cover.page"]:
        out.append(".title-block + .repeat-title, .repeat-title { display: block; text-align: center; font-weight: 700;"
                   " margin: 0 0 1em; }")
    else:
        out.append(".repeat-title { display: none; }")
    if d["cover.title-colon"]:
        out.append('.title-block .title:has(+ .subtitle)::after { content: ":"; }')
    if d["cover.page"]:
        out.append(".title-block { padding: 3em 0 4em; border-bottom: 1px solid #ccc; margin-bottom: 3em; }")
    # Quotes, footnotes, bibliography, captions
    q_line = round(0.65 * d["quote.leading"] + 1.0, 3) if d["quote.leading"] else "inherit"
    q_space = _css_len(d["quote.spacing"]) if d["quote.spacing"] else "1em"
    out.append(f"blockquote {{ margin: {q_space} {_css_len(d['quote.indent-right'])} {q_space}"
               f" {_css_len(d['quote.indent-left'])};"
               f" font-size: {d['quote.size']:g}em; line-height: {q_line};"
               f" font-style: {'italic' if d['quote.italic'] else 'normal'}; }}")
    f_line = round(0.65 * d["footnote.leading"] + 1.0, 3) if d["footnote.leading"] else "inherit"
    out.append(f".footnotes {{ font-size: {d['footnote.size']:g}em; line-height: {f_line}; }}")
    if not d["footnote.rule"]:
        out.append(".footnotes hr { display: none; }")
    b_line = round(0.65 * d["bib.leading"] + 1.0, 3) if d["bib.leading"] else "inherit"
    hang = _css_len(d["bib.hanging"])
    out.append(f"#refs {{ font-size: {d['bib.size']:g}em; line-height: {b_line}; text-align: left; }}")
    out.append(f".csl-entry {{ padding-left: {hang}; text-indent: -{hang}; margin-bottom: {_css_len(d['bib.spacing'])}; }}"
               if hang != "0" else f".csl-entry {{ padding-left: 0; text-indent: 0; margin-bottom: {_css_len(d['bib.spacing'])}; }}")
    c_line = round(0.65 * d["caption.leading"] + 1.0, 3) if d["caption.leading"] else "inherit"
    out.append(f"figcaption, caption {{ font-size: {d['caption.size']:g}em; text-align: {d['caption.align']};"
               f" line-height: {c_line}; }}")
    out.append(f"caption {{ caption-side: {d['caption.table-position']}; }}")
    if d["caption.figure-position"] == "top":
        out.append("figure { display: flex; flex-direction: column; } figure figcaption { order: -1; }")
    if d["caption.title-style"] == "italic":
        out.append("figcaption, caption { font-style: italic; }")
    # Bibliography heading
    if d["bib.heading-style"] != "heading":
        weight = "400" if d["bib.heading-style"] == "center-plain" else _WEIGHT_CSS[d["heading.1.weight"]]
        out.append(f"#bibliography > h1, h1#bibliography {{ text-align: center; font-weight: {weight}; }}")
    # Scene breaks
    brk = d["text.scene-break"]
    if brk == "blank":
        out.append("hr { border: 0; margin: 1em 0; }")
    elif brk != "rule":
        out.append(f"hr {{ border: 0; width: auto; text-align: center; margin: 0.5em 0; overflow: visible; }}"
                   f" hr::after {{ content: {json.dumps(brk)}; }}")
    # Scripts (filters/screenplay.lua, avscript.lua)
    out += [
        ".scene-heading p { margin: 2em 0 1em; display: flex; justify-content: space-between; gap: 1em; font-weight:"
        f" {_WEIGHT_CSS[d['script.scene-weight']]}; }}",
        ".action { margin: 0 0 1em; text-indent: 0; }",
        f".character {{ margin: 1em 0 0 {_css_len(d['script.character-indent'])}; text-transform: uppercase;"
        " text-indent: 0; }",
        f".parenthetical {{ margin: 0 0 0 {_css_len(d['script.parenthetical-indent'])};"
        f" max-width: {_css_len(d['script.parenthetical-width'])}; text-indent: 0; }}",
        f".dialogue {{ margin: 0 0 1em {_css_len(d['script.dialogue-indent'])};"
        f" max-width: {_css_len(d['script.dialogue-width'])}; text-indent: 0; }}",
        ".dialogue p, .action p, .parenthetical p, .character p { margin: 0; text-indent: 0; }",
        ".transition { text-align: right; text-transform: uppercase; margin: 1em 0; }",
        ".centered { text-align: center; }",
        "table.av { width: 100%; border-collapse: collapse; } table.av td, table.av th { border: 1px solid #999;"
        " vertical-align: top; text-align: left; padding: 0.4em 0.6em; } table.av td p { text-indent: 0; }",
        ".end-mark { text-align: center; margin-top: 2em; }",
    ]
    return "\n".join(out) + "\n"


def _tmpl_text(s: str) -> str:
    """Literal text inside a pandoc template that outputs HTML."""
    return html.escape(s, quote=True).replace("$", "$$")


def _html_fields(items: list[dict], design: dict) -> list[str]:
    lines = []
    for item in items:
        style = item.get("style", "normal")
        cls = "cover-field" + (f" cover-{style}" if style != "normal" else "")
        label = _tmpl_text(item["label"]) + " " if item.get("label") else ""
        if "space" in item:
            height = "3em" if item["space"] == "fill" else _css_len(item["space"])
            lines.append(f'<div class="cover-space" style="height:{height}"></div>')
        elif "text" in item:
            lines.append(f'<p class="{cls}">{_tmpl_text(item["text"])}</p>')
        else:
            key = item["key"]
            if key == "author":
                lines.append(f'$for(author)$<p class="{cls} author">{label}$if(author.name)$$author.name$$else$$author$'
                             f'$endif$</p>$endfor$')
            elif key in ("affiliation", "email"):
                lines.append(f'$for(author)$$if(author.{key})$<p class="{cls}">{label}$author.{key}$</p>$endif$'
                             f'$endfor$')
                if key == "email":
                    lines.append(f'$if(email)$<p class="{cls}">{label}$email$</p>$endif$')
            elif key == "keywords":
                lines.append(f'$if(keywords)$<p class="{cls}">{label}$for(keywords)$$keywords$$sep$, $endfor$</p>$endif$')
            elif key == "place-date":
                lines.append(f'$if(date)$<p class="{cls}">{label}$if(place)$$place$, $endif$$date$</p>$endif$')
            elif key == "byline":
                lines.append(f'$if(author)$<p class="{cls}">{label}$dw-terms.by$ $for(author)$$if(author.name)$'
                             f'$author.name$$else$$author$$endif$$sep$, $endfor$</p>$endif$')
            elif key == "abstract":
                if design["cover.abstract"] and not design["cover.abstract-page"]:
                    lines.append(_html_abstract(design))
            else:
                lines.append(f'$if({key})$<p class="{cls}">{label}${key}$</p>$endif$')
    return lines


def _html_abstract(design: dict) -> str:
    heading = (_tmpl_text(design["cover.abstract-title"]) if design["cover.abstract-title"]
               else "$if(abstract-title)$$abstract-title$$endif$")
    sep = {"em-dash": "—", "break": "<br>"}.get(design["cover.abstract-separator"], " ")
    return f'$if(abstract)$<div class="abstract"><span class="abstract-title">{heading}</span>{sep}$abstract$</div>$endif$'


def html_cover(design: dict) -> str:
    """The pandoc partial with the title block's fields, in the design's order."""
    lines = ["$-- Generated from the layout's design settings. Edit the layout instead."]
    if not design["cover.fields-first"]:
        lines += _html_fields(design["cover.fields"], design)
    has_abstract = any(item.get("key") == "abstract" for item in design["cover.fields"])
    if design["cover.abstract"] and not has_abstract:
        lines.append(_html_abstract(design))
    return "\n".join(lines) + "\n"


def html_cover_top(design: dict) -> str:
    """The partial before the title: the corners (manuscripts) and fields that go above the title (MLA)."""
    lines = ["$-- Generated from the layout's design settings. Edit the layout instead."]
    left, right = design["cover.corner-left"], design["cover.corner-right"]
    if left or right:
        lines.append('<div class="corners"><div class="corner-left">')
        lines += _html_fields(left, design)
        lines.append('</div><div class="corner-right">')
        lines += _html_fields(right, design)
        lines.append("</div></div>")
    if design["cover.fields-first"]:
        lines += _html_fields(design["cover.fields"], design)
    return "\n".join(lines) + "\n"


def typst_overrides(variables: dict) -> str:
    """Pandoc template code that lets the classic options override the design.

    Front matter and project options arrive as metadata. Booleans are only read when the
    layout declares them: then the metadata always has a value, its default included.
    """
    lines = ["#let dw = dw + ("]
    lines.append('$if(papersize)$  "page-paper": "$papersize$",$endif$')
    lines.append('$if(fontsize)$  "text-size": ($fontsize$) / 1pt,$endif$')
    lines.append('$if(linestretch)$  "text-leading": $linestretch$,$endif$')
    lines.append('$if(columns)$  "page-columns": "$columns$",$endif$')
    if "section-numbering" in variables:
        lines.append('  "heading-numbering": dw-section-numbering("$section-numbering$"),')
    if "titlepage" in variables:
        lines.append('  "cover-page": $if(titlepage)$true$else$false$endif$,')
    lines.append(")")
    return "\n".join(lines) + "\n"


def html_options(variables: dict) -> str:
    """Pandoc template code for the HTML head: the classic options as CSS overrides."""
    return ("$if(fontsize)$<style>body { font-size: $fontsize$; }</style>$endif$\n"
            "$if(linestretch)$<style>body { line-height: calc(0.65 * $linestretch$ + 1); }</style>$endif$\n")


def generated_files(design: dict, variables: dict | None = None) -> dict[str, str]:
    variables = variables or {}
    return {"typst/design.typ": typst_design(design) + typst_overrides(variables),
            "html/design.css": css_design(design), "html/cover.html": html_cover(design),
            "html/cover-top.html": html_cover_top(design),
            "html/options.html": html_options(variables)}


GENERATOR_HASH = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def digest(design: dict) -> str:
    return hashlib.sha256(json.dumps(design, sort_keys=True).encode()).hexdigest()

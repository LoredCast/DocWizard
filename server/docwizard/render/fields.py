"""Front matter fields: which ones a layout shows, what they mean and how to fill them in (§9.4).

A layout doesn't declare its fields by hand. They follow from its design: the keys in
``cover.fields`` and the first page's corners, the running header, the label before the
page numbers and the declaration page. So every layout, built in or made in the layout
editor, lists exactly the fields it prints, and the list stays right when a design changes.

The editor's Fields panel, its front matter autocomplete and the layout gallery read this.
Texts are English source strings; the editor translates them with the page's catalog.
"""

from __future__ import annotations

# key: (label, help, snippet, example). In a snippet, ${} marks where the cursor goes and
# {today} becomes today's date.
_F = {
    "title": ("Title", "The document's title.",
              'title: "${}"', 'title: "The Politics of Infrastructure"'),
    "subtitle": ("Subtitle", "Shown under the title, or after it with a colon where the style guide asks for one.",
                 'subtitle: "${}"', 'subtitle: "Trams and the Growth of Vienna"'),
    "author": ("Author", "Your name. For several authors, write a list: author: [Jane Doe, John Roe].",
               'author: "${}"', 'author: "Jane Doe"'),
    "affiliation": ("Affiliation", "Where each author works or studies. Write the authors as a list, with name: and "
                    "affiliation: for each.",
                    'author:\n  - name: "${}"\n    affiliation: ""',
                    "author:\n  - name: Jane Doe\n    affiliation: Example University"),
    "email": ("Email address", "Your email address. With several authors, give each their own email: under "
              "author:, next to name:.", 'email: "${}"', "email: jane.doe@example.org"),
    "date": ("Date", "Written as 2026-11-17 (or 2026-11), it is formatted for the document's language and the "
             "layout. Anything else is printed as written.", "date: {today}${}", "date: 2026-11-17"),
    "paper-type": ("Type of paper", "What kind of paper it is: term paper, seminar paper, bachelor's thesis, …",
                   'paper-type: "${}"', 'paper-type: "Seminar paper"'),
    "course": ("Course", "The course or seminar the paper is written for.",
               'course: "${}"', 'course: "Seminar on Urban Studies"'),
    "instructor": ("Instructor", "Who teaches the course.", 'instructor: "${}"', 'instructor: "Prof. Maria Muster"'),
    "supervisor": ("Supervisor", "Who supervises the paper or thesis.",
                   'supervisor: "${}"', 'supervisor: "Prof. Maria Muster"'),
    "semester": ("Semester", "The semester or term, or your semester of study where the style guide asks for it.",
                 'semester: "${}"', 'semester: "Winter semester 2026/27"'),
    "department": ("Department", "The department or institute.",
                   'department: "${}"', 'department: "Department of Philosophy"'),
    "faculty": ("Faculty", "The faculty or school within the university.",
                'faculty: "${}"', 'faculty: "Faculty of Arts"'),
    "institution": ("Institution", "The university, college or school.",
                    'institution: "${}"', 'institution: "University of Vienna"'),
    "student-id": ("Student ID", "Your student or matriculation number. Keep the quotes, so leading zeros stay.",
                   'student-id: "${}"', 'student-id: "01234567"'),
    "program": ("Degree programme", "Your degree programme, or its code where the style guide asks for one.",
                'program: "${}"', 'program: "BA Philosophy"'),
    "place": ("Place", "Where you wrote the paper. Layouts that print “Place, date” put it before the date.",
              'place: "${}"', "place: Vienna"),
    "keywords": ("Keywords", "A list of keywords.", "keywords: [${}]", "keywords: [infrastructure, cities, trams]"),
    "abstract": ("Abstract", "A short summary. Start with abstract: | and indent the paragraphs under it.",
                 "abstract: |\n  ${}", "abstract: |\n  This paper argues that …"),
    "address": ("Postal address", "Your postal address. For several lines, start each one with a bar, as in the "
                "example.", "address: |\n  | ${}", "address: |\n  | 12 Example Street\n  | 1010 Vienna"),
    "phone": ("Telephone", "Your telephone number.", 'phone: "${}"', 'phone: "+43 1 234 5678"'),
    "word-count": ("Word count", "Counted for you and rounded (“about 2,500 words”). Set it only to print "
                   "something else.", 'word-count: "${}"', 'word-count: "about 2,500 words"'),
    "genre": ("Genre or format", "The kind of work: short story, novel, feature film, …",
              'genre: "${}"', 'genre: "Short story"'),
    "draft": ("Draft or version", "Which draft this is, often with its date.",
              'draft: "${}"', 'draft: "Second draft, November 2026"'),
    "contact": ("Contact or agent", "Who to contact about the work, such as your agent.",
                'contact: "${}"', 'contact: "Represented by Example Agency"'),
    "submission": ("Submitted for", "What the paper is submitted for: the degree, module or examination.",
                   'submission: "${}"', "submission: \"Master's thesis in Value Chain Management\""),
    "birthplace": ("Place of birth", "Your place of birth, where the examination office asks for it.",
                   'birthplace: "${}"', "birthplace: Kassel"),
    "short-title": ("Short title", "A shorter title for the running header and the page numbers. Without it, the "
                    "title is used.", 'short-title: "${}"', 'short-title: "Silvertide"'),
    "declaration": ("Declaration", "Your own wording for the declaration page. Without it, the layout's standard "
                    "text is used.", "declaration: |\n  ${}", "declaration: |\n  I declare that …"),
    "thanks": ("Acknowledgements", "A footnote on the title, for thanks or funding.",
               'thanks: "${}"', 'thanks: "I thank the city archive for its help."'),
    "lang": ("Language", "The document's main language, such as en-US, en-GB, de-DE or de-AT. It sets hyphenation, "
             "quotation marks, dates and the terms in the bibliography.", "lang: ${}", "lang: en-GB"),
    "otherlangs": ("Other languages", "Other languages the document uses. Mark their passages with [texte]{lang=fr} "
                   "or ::: {lang=fr}.", "otherlangs: [${}]", "otherlangs: [fr, la]"),
    "bibliography": ("Bibliography", "Where your sources are: .json, .bib or .ris files in this project, or a "
                     "Zotero library as zotero:name.", "bibliography: ${}",
                     "bibliography:\n  - references.json\n  - zotero:thesis"),
    "csl": ("Citation style", "Usually set with the layout option. Here: a bundled style such as apa, or a .csl "
            "file in this project.", "csl: ${}", "csl: apa"),
    "nocite": ("Sources not cited", "Sources to list in the bibliography without citing them. @* lists every "
               "source.", "nocite: |\n  ${}", "nocite: |\n  @doe2020, @roe2021"),
}

# Fields every document can use, whatever its layout prints.
GENERAL_KEYS = ["lang", "otherlangs", "bibliography", "csl", "nocite", "thanks", "keywords", "short-title"]

# A title block entry and the front matter fields it is filled from.
_SOURCES = {"byline": ["author"], "place-date": ["place", "date"]}

# Fields written per author, inside author:. An email address can also stand on its own.
_NESTED = {"affiliation": False, "email": True}
# Filled in for the author when it is missing.
_AUTO = {"word-count"}


def entry(key: str) -> dict:
    label, help_, snippet, example = _F[key]
    out = {"key": key, "label": label, "help": help_, "snippet": snippet, "example": example}
    if key in _NESTED:
        out["in"] = "author"
        out["topLevel"] = _NESTED[key]
    if key in _AUTO:
        out["auto"] = True
    return out


def shown(design: dict) -> list[dict]:
    """The fields a layout prints, in the order they appear, with where each one goes.

    Each item is ``{"key", "where": [{"at": …, "label"?: …, "as"?: …}]}``. ``at`` is
    ``cover`` (the title block or title page), ``corner-left``, ``corner-right``,
    ``header``, ``page-number`` or ``declaration``. ``label`` is the text the layout
    writes before the value; ``as`` names the title block entry it feeds (``byline``,
    ``place-date``).
    """
    found: dict[str, list[dict]] = {}

    def add(key: str, at: str, label: str | None = None, via: str | None = None) -> None:
        if key not in _F:
            return
        place = {"at": at}
        if label:
            place["label"] = label
        if via:
            place["as"] = via
        places = found.setdefault(key, [])
        if place not in places:
            places.append(place)

    add("title", "cover")
    add("subtitle", "cover")

    def items(setting: str, at: str) -> None:
        for item in design.get(setting) or []:
            key = item.get("key") if isinstance(item, dict) else None
            if not key:
                continue
            for source in _SOURCES.get(key, [key]):
                add(source, at, item.get("label"), key if key in _SOURCES else None)

    items("cover.corner-left", "corner-left")
    items("cover.fields", "cover")
    items("cover.corner-right", "corner-right")
    if design.get("cover.abstract") and "abstract" not in found:
        add("abstract", "cover")

    header = design.get("page.header", "none")
    if header in ("title", "author-title"):
        add("title", "header")
    if header in ("author", "author-title"):
        add("author", "header")
    number_label = design.get("page.number-label", "none")
    if number_label in ("surname", "surname-title"):
        add("author", "page-number", via="surname")
    if number_label == "surname-title":
        add("short-title", "page-number")
    if design.get("declaration.enabled"):
        add("declaration", "declaration")
    return [{"key": key, "where": where} for key, where in found.items()]


def describe(design: dict) -> dict:
    """Everything the editor needs: the fields this layout prints, the fields every document
    can use, and the texts for every field DocWizard knows, so the editor can also say when a
    field in the document isn't printed by this layout."""
    printed = shown(design)
    keys = {item["key"] for item in printed}
    return {
        "shown": printed,
        "titlePage": bool(design.get("cover.page")),
        "fieldsFirst": bool(design.get("cover.fields-first")),
        "general": [key for key in GENERAL_KEYS if key not in keys],
        "catalog": {key: entry(key) for key in _F},
    }


def for_layout(layout) -> dict:
    """:func:`describe` for a layout. A layout with templates of its own doesn't read the
    design settings, so what it prints is only in its README."""
    out = describe(layout.design)
    if not layout.designable:
        out["shown"] = []
        out["general"] = list(GENERAL_KEYS)
        out["ownTemplates"] = True
    return out


# Not left empty in a new document: already there, counted for you, or optional extras.
_STARTER_SKIP = {"title", "subtitle", "author", "date", "abstract", "affiliation", "word-count", "short-title",
                 "declaration"}


def starter_lines(layout) -> list[str]:
    """Empty front matter lines for the fields a layout prints beyond title, author and date,
    so a new document shows what there is to fill in."""
    return [f"{key}: []" if key == "keywords" else f'{key}: ""'
            for key in shown_keys(layout) if key not in _STARTER_SKIP]


def shown_keys(layout) -> list[str]:
    """The front matter keys a layout prints, for the layout gallery."""
    return [] if not layout.designable else [item["key"] for item in shown(layout.design)]

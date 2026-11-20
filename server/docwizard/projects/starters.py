"""The files a new project starts with, chosen by its layout (§9.3, §9.4).

A layout names its kind of starter in its manifest (`"starter"`), and custom layouts inherit it
from their parent. Without one, script layouts start as a script and every other layout as a paper:

- `paper`: `paper.md` and `references.json`
- `thesis`, `book`: the title page and abstract in `thesis.md` (`book.md`), a file per chapter
  in `chapters/`, `references.json`, and the files as the project's build inputs, in order
- `novel`: `novel.md` with the front matter, a file per chapter in `chapters/`, no bibliography
- `story`: one `story.md`, no bibliography
- `screenplay`: `script.md`, written after Fountain's rules
- `avscript`: `script.md` with VIDEO and AUDIO rows

The text is in the interface language, or in German when the layout itself is German.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import files as project_files

KINDS = ("paper", "thesis", "book", "novel", "story", "screenplay", "avscript")

REFERENCES = """[
  {
    "id": "doe2020",
    "type": "book",
    "title": "An Example Book",
    "author": [{"family": "Doe", "given": "Jane"}],
    "issued": {"date-parts": [[2020]]},
    "publisher": "Example Press",
    "publisher-place": "Berlin"
  }
]
"""

# Every string a starter writes, by language.
TEXT = {
    "en": {
        "untitled": "Untitled",
        "lang": "en-US",
        "write": "Write here. Cite sources like this [@doe2020, p. 4], and add them to\n"
                 "`references.json` or a `.bib` file in this project.",
        "references": "References",
        "introduction": "Introduction",
        "background": "Background",
        "conclusion": "Conclusion",
        "first-chapter": "The First Chapter",
        "chapter-one": "Chapter One",
        "chapter-two": "Chapter Two",
        "abstract": "One paragraph on the question, how you went about it and what you found.",
        "chapters": "The chapters are the files in chapters/. They print in the order of the\n"
                    "project's build inputs, after this file; add a chapter there to include it.",
        "chapter-body": "Each chapter starts on a new page.",
        "next-chapter": "Add the next chapter as chapters/03-….md and to the build inputs.",
        "novel-body": "The story begins here. Leave a blank line between paragraphs; the layout\n"
                      "indents them. A line with only # on it is a scene break.",
        "story-body": "The story begins here. Leave a blank line between paragraphs; the layout\n"
                      "indents them.\n\n#\n\nA line with only # on it is a scene break.",
        "screenplay": """EXT. HARBOUR - DAWN

Gulls circle above the empty quay. MARA (30s), soaked through, climbs out of a dinghy.

TOM
(without looking up)
You came back.

MARA
I said I would.

CUT TO:

INT. HARBOUR OFFICE - CONTINUOUS

<!-- A scene heading starts with INT. or EXT.; a name in capitals over a line is
dialogue; capitals ending in TO: are a transition. The Outline lists the scenes. -->

> THE END <
""",
        "avscript": """# Opening

VIDEO: Wide shot of the harbour at dawn. Gulls over the water.

AUDIO: SFX: gulls, a distant engine.

VIDEO: Close on a rope being untied.

AUDIO: NARRATOR (V.O.): Every morning the boats go out.

<!-- VIDEO: starts a new row with its picture, AUDIO: fills in the sound beside it.
A paragraph without either continues the cell above; a heading starts a sequence. -->
""",
    },
    "de": {
        "untitled": "Ohne Titel",
        "lang": "de-DE",
        "write": "Schreib hier. Quellen zitierst du so [@doe2020, S. 4]; trag sie in\n"
                 "`references.json` oder eine `.bib`-Datei in diesem Projekt ein.",
        "references": "Literatur",
        "introduction": "Einleitung",
        "background": "Grundlagen",
        "conclusion": "Fazit",
        "first-chapter": "Das erste Kapitel",
        "chapter-one": "Kapitel eins",
        "chapter-two": "Kapitel zwei",
        "abstract": "Ein Absatz zur Fragestellung, zum Vorgehen und zu den Ergebnissen.",
        "chapters": "Die Kapitel sind die Dateien in chapters/. Sie folgen in der Reihenfolge\n"
                    "der Build-Eingaben des Projekts auf diese Datei; ein neues Kapitel dort eintragen.",
        "chapter-body": "Jedes Kapitel beginnt auf einer neuen Seite.",
        "next-chapter": "Das nächste Kapitel als chapters/03-….md anlegen und in die Build-Eingaben aufnehmen.",
        "novel-body": "Hier beginnt die Geschichte. Zwischen Absätzen eine Leerzeile lassen; das\n"
                      "Layout rückt sie ein. Eine Zeile nur mit # ist ein Szenenwechsel.",
        "story-body": "Hier beginnt die Geschichte. Zwischen Absätzen eine Leerzeile lassen; das\n"
                      "Layout rückt sie ein.\n\n#\n\nEine Zeile nur mit # ist ein Szenenwechsel.",
        "screenplay": """.HAFEN – AUSSEN / MORGENGRAUEN

Möwen kreisen über dem leeren Kai. MARA (30), durchnässt, klettert aus einem Beiboot.

TOM
(ohne aufzusehen)
Du bist zurückgekommen.

MARA
Ich hab's versprochen.

SCHNITT AUF:

.HAFENBÜRO – INNEN / MORGEN

<!-- Ein Punkt am Zeilenanfang macht einen Szenenkopf; ein Name in Versalien über einer
Zeile ist Dialog; Versalien mit AUF: am Ende sind ein Übergang. Die Gliederung listet die Szenen. -->
""",
        "avscript": """# Auftakt

BILD: Totale auf den Hafen im Morgengrauen. Möwen über dem Wasser.

TON: Geräusch: Möwen, ein ferner Motor.

BILD: Nah auf ein Tau, das gelöst wird.

TON: SPRECHER (OFF): Jeden Morgen fahren die Boote hinaus.

<!-- BILD: beginnt eine neue Zeile mit dem Bild, TON: ergänzt den Ton daneben.
Ein Absatz ohne beides setzt die Zelle darüber fort; eine Überschrift beginnt eine Sequenz. -->
""",
    },
}


@dataclass
class Starter:
    kind: str
    files: list[tuple[str, str]] = field(default_factory=list)  # (path, text); the first one opens
    assets: list[tuple[str, bytes]] = field(default_factory=list)  # (path, bytes) for bundled binary files
    build: list[str] | None = None  # the project's build inputs, in order

    @property
    def main(self) -> str | None:
        return self.files[0][0] if self.files else None


def kind_of(layout) -> str:
    if layout is None:
        return "paper"
    kind = layout.manifest.get("starter")
    if kind == "latex-bundle" or kind in KINDS:
        return kind
    filters = layout.manifest.get("filters") or []
    if "screenplay" in filters:
        return "screenplay"
    if "avscript" in filters:
        return "avscript"
    return "paper"


def language(layout, ui_lang: str) -> str:
    """German for a German interface, and for layouts written for German (Drehbuch, Normseite)."""
    if ui_lang == "de":
        return "de"
    try:
        tag = str(layout.design.get("text.lang") or "") if layout is not None else ""
    except Exception:  # noqa: BLE001 - a broken design shouldn't stop a project from being made
        tag = ""
    return "de" if tag.lower().startswith("de") else "en"


def _bib_heading(layout) -> bool:
    """Whether the layout leaves the bibliography without a heading of its own (`bib.heading`
    "none"), so the starter names it with `reference-section-title`."""
    try:
        return layout is None or layout.design.get("bib.heading", "none") == "none"
    except Exception:  # noqa: BLE001
        return True


def _front(s: dict, author: str, date: str, fields: list[str] | tuple, *, bib: bool, refs_title: bool,
           abstract: bool = False) -> str:
    lines = ["---", f'title: "{s["untitled"]}"', f'author: "{_yaml(author)}"', f"date: {date}"]
    lines += list(fields)
    lines.append(f"lang: {s['lang']}")
    if abstract:
        lines += ["abstract: |", f"  {s['abstract']}"]
    if bib:
        lines.append("bibliography: references.json")
        if refs_title:
            lines.append(f'reference-section-title: "{s["references"]}"')
    lines.append("---")
    return "\n".join(lines) + "\n"


def _yaml(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _comment(text: str) -> str:
    return f"<!-- {text} -->\n"


def _latex_bundle_starter(layout) -> Starter:
    """Bundled files from an installed LaTeX template's starter/ folder."""
    out = Starter("latex-bundle")
    main_rel = layout.manifest.get("starterMain") or "main.tex"
    docs: list[tuple[str, str]] = []
    layout_files = getattr(layout, "files", None) or {}
    for key in sorted(layout_files):
        if not key.startswith("starter/"):
            continue
        rel = key.removeprefix("starter/")
        try:
            rel = project_files.normalize(rel)
            raw = layout_files[key].read_bytes()
        except (OSError, project_files.PathError):
            continue
        kind = project_files.kind_for(rel)
        if kind is not None:
            try:
                text, _ = project_files.decode_text(raw)
            except UnicodeDecodeError:
                if kind in ("latex", "bib"):
                    text = raw.decode("latin-1").replace("\r\n", "\n").replace("\r", "\n")
                else:
                    out.assets.append((rel, raw))
                    continue
            docs.append((rel, text))
        else:
            out.assets.append((rel, raw))
    docs.sort(key=lambda item: (item[0] != main_rel, item[0].lower()))
    out.files = docs
    if docs and docs[0][0].lower().endswith(".tex"):
        out.build = [docs[0][0]]
    return out


def for_layout(layout, ui_lang: str, author: str, date: str, fields: list[str] | tuple = ()) -> Starter:
    """The files a new project with *layout* starts with. *fields* are front matter lines for what
    the layout prints besides title, author and date (render/fields.py)."""
    kind = kind_of(layout)
    if kind == "latex-bundle" and layout is not None:
        bundle = _latex_bundle_starter(layout)
        if bundle.files:
            return bundle
        kind = "paper"
    lang = language(layout, ui_lang)
    s = TEXT[lang]
    refs_title = _bib_heading(layout)
    out = Starter(kind)

    if kind == "paper":
        body = f"# {s['introduction']}\n\n{s['write']}\n"
        out.files = [("paper.md", _front(s, author, date, fields, bib=True, refs_title=refs_title) + "\n" + body),
                     ("references.json", REFERENCES)]
    elif kind in ("thesis", "book"):
        main = f"{kind}.md"
        front = _front(s, author, date, fields, bib=True, refs_title=refs_title, abstract=kind == "thesis")
        if kind == "thesis":
            chapters = [("01-" + slug(s["introduction"]), s["introduction"], s["write"]),
                        ("02-" + slug(s["background"]), s["background"], s["chapter-body"]),
                        ("03-" + slug(s["conclusion"]), s["conclusion"], s["next-chapter"].replace("03-", "04-"))]
        else:
            chapters = [("01-" + slug(s["introduction"]), s["introduction"], s["write"]),
                        ("02-" + slug(s["first-chapter"]), s["first-chapter"], s["next-chapter"])]
        out.files = [(main, front + "\n" + _comment(s["chapters"]))]
        out.files += [(f"chapters/{name}.md", f"# {title}\n\n{body}\n") for name, title, body in chapters]
        out.files.append(("references.json", REFERENCES))
        out.build = [path for path, _ in out.files if path.endswith(".md")]
    elif kind == "novel":
        front = _front(s, author, date, fields, bib=False, refs_title=False)
        out.files = [("novel.md", front + "\n" + _comment(s["chapters"])),
                     ("chapters/01-" + slug(s["chapter-one"]) + ".md", f"# {s['chapter-one']}\n\n{s['novel-body']}\n"),
                     ("chapters/02-" + slug(s["chapter-two"]) + ".md",
                      f"# {s['chapter-two']}\n\n{s['next-chapter']}\n")]
        out.build = [path for path, _ in out.files]
    elif kind == "story":
        front = _front(s, author, date, fields, bib=False, refs_title=False)
        out.files = [("story.md", front + "\n" + s["story-body"] + "\n")]
    else:  # screenplay, avscript
        front = _front(s, author, date, fields, bib=False, refs_title=False)
        out.files = [("script.md", front + "\n" + s[kind])]
    return out


def slug(title: str) -> str:
    out = []
    for ch in title.lower():
        if ch.isalnum() and ch.isascii():
            out.append(ch)
        elif ch in "äöüß":
            out.append({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}[ch])
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-") or "chapter"

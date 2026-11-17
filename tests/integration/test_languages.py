"""Document languages (§9.8) and the script filters (§9.3): the Lua filters run on their own
through pandoc, and once through the real renderer with a project's language."""

import json
import os
import shutil
import subprocess

import pytest

from docwizard.render import languages
from docwizard.render.layouts import REPO_LAYOUTS
from tests.integration.test_collab import new_project
from tests.integration.test_render import HAS_TYPST, _pandoc_ok, _path, rapp, rclient, wait_job  # noqa: F401

FILTERS = REPO_LAYOUTS.parent / "filters"
needs_pandoc = pytest.mark.skipif(not _pandoc_ok(), reason="needs pandoc")


def pandoc(text, *filters, to="json", meta=None):
    exe = shutil.which("pandoc", path=_path())
    args = [exe, "-f", "markdown", "-t", to]
    for f in filters:
        args += ["-L", str(FILTERS / f"{f}.lua")]
    for k, v in (meta or {}).items():
        args += ["-M", f"{k}={v}"]
    env = dict(os.environ, LUA_PATH=f"{FILTERS}/?.lua")
    out = subprocess.run(args, input=text, capture_output=True, text=True, env=env, check=True).stdout
    return json.loads(out) if to == "json" else out


def meta_text(value):
    if value["t"] == "MetaString":
        return value["c"]
    if value["t"] == "MetaInlines":
        return "".join(i["c"] if i["t"] == "Str" else " " for i in value["c"])
    if value["t"] == "MetaList":
        return [meta_text(v) for v in value["c"]]
    raise AssertionError(value)


# -- languages ---------------------------------------------------------------------
def test_language_tags_normalize():
    assert languages.normalize("de-at") == "de-AT"
    assert languages.normalize("de_CH") == "de-CH"
    assert languages.normalize("German") == "de"
    assert languages.normalize(" EN-gb ") == "en-GB"
    assert languages.normalize("") is None
    for bad in ("de AT", "x" * 40, "de-AT\"); #panic(", "123"):
        assert languages.normalize(bad) is None, bad
    choices = dict(languages.choices())
    assert {"de-AT", "en-US", "fr"} <= set(choices)
    assert choices["de-AT"] == "Deutsch (Österreich) · German (Austria)"
    assert languages.full_support("de-AT") and not languages.full_support("fi")


@needs_pandoc
def test_dates_terms_and_other_languages():
    doc = pandoc("---\nlang: de-at\ndate: 2026-01-05\n---\n\nText [Mot]{lang=fr-CH}.\n\n"
                 "::: {lang=en-GB}\nHello.\n:::\n", "lang")
    m = doc["meta"]
    assert meta_text(m["lang"]) == "de-AT"
    assert meta_text(m["date"]) == "5. Jänner 2026"  # Austrian month names
    assert meta_text(m["date-iso"]) == "2026-01-05"
    assert meta_text(m["toc-title"]) == "Inhaltsverzeichnis"
    assert meta_text(m["abstract-title"]) == "Zusammenfassung"
    assert meta_text(m["otherlangs"]) == ["fr-CH", "en-GB"]
    terms = m["dw-terms"]["c"]
    assert meta_text(terms["figure"]) == "Abbildung" and meta_text(terms["and"]) == "und"


@needs_pandoc
def test_date_formats_and_bibliography_heading():
    m = pandoc("---\nlang: en-GB\ndate: 2026-03-07\n---\n\nText.\n", "lang",
               meta={"dw-date-format": "numeric", "dw-bib-heading": "references"})["meta"]
    assert meta_text(m["date"]) == "07/03/2026"
    assert meta_text(m["reference-section-title"]) == "References"
    m = pandoc("---\nlang: en-US\ndate: 2026-03-07\n---\n\nText.\n", "lang")["meta"]
    assert meta_text(m["date"]) == "March 7, 2026"
    m = pandoc("---\nlang: fr\ndate: 2026-03\nreference-section-title: Sources\n---\n\nText.\n", "lang",
               meta={"dw-bib-heading": "bibliography"})["meta"]
    assert meta_text(m["date"]) == "mars 2026"
    assert meta_text(m["reference-section-title"]) == "Sources"  # the document's own title wins
    # Text that isn't an ISO date stays as written.
    m = pandoc("---\nlang: de\ndate: Sommer 2026\n---\n\nText.\n", "lang")["meta"]
    assert meta_text(m["date"]) == "Sommer 2026"


@needs_pandoc
def test_quotes_follow_the_language_of_each_passage():
    text = ('---\nlang: de\n---\n\n"Er sagte \'nein\'" und [«oui»]{lang=fr-CH} und "[quoi]{lang=fr}".\n\n'
            '::: {lang=en-GB}\n"Hello"\n:::\n\n::: {lang=fr}\n"Bonjour"\n:::\n')
    out = pandoc(text, "lang", "quotes", to="plain")
    assert "„Er sagte ‚nein‘“" in out
    assert "„quoi“" in out
    assert "‘Hello’" in out
    assert "« Bonjour »" in out
    # A layout can swap the pairs for its main language (Oxford: single quotes first).
    out = pandoc('---\nlang: en-GB\n---\n\n"a \'b\'"\n', "lang", "quotes", to="plain",
                 meta={"dw-quotes": "double"})
    assert "“a ‘b’”" in out


@needs_pandoc
def test_typst_scopes_language_passages():
    out = pandoc("---\nlang: de\n---\n\nText [mot]{lang=fr-CH}.\n\n::: {lang=en}\nHello.\n:::\n",
                 "lang", "quotes", to="typst")
    assert '#text(lang: "fr", region: "CH")[mot]' in out
    assert '#[\n#set text(lang: "en")' in out


@needs_pandoc
def test_cross_references_use_the_language():
    text = ("---\nlang: de\n---\n\nSiehe @fig:a und @tbl:b.\n\n![Netz](x.png){#fig:a}\n\n"
            "| a |\n|---|\n| 1 |\n\n: Werte {#tbl:b}\n")
    out = pandoc(text, "lang", "crossref", to="plain")
    assert "Abbildung 1 und Tabelle 1" in out


@needs_pandoc
def test_word_count_is_rounded_in_the_language():
    words = " ".join(["Wort"] * 4260)
    m = pandoc(f"---\nlang: de\n---\n\n{words}\n", "lang", "wordcount")["meta"]
    assert meta_text(m["word-count"]) == "ca. 4.300 Wörter"
    m = pandoc(f"---\nlang: en\n---\n\n{words}\n", "lang", "wordcount")["meta"]
    assert meta_text(m["word-count"]) == "about 4,300 words"


# -- scripts -------------------------------------------------------------------------
SCREENPLAY = """# ACT ONE

EXT. HARBOUR - DAWN

Gulls circle above the quay.

MARA
(quietly)
You came back.

@McCLANE
Yippee.

CUT TO:

> THE END <
"""


@needs_pandoc
def test_screenplay_elements():
    out = pandoc(SCREENPLAY, "screenplay", to="html")
    for cls in ("act", "scene-heading", "action", "character", "parenthetical", "dialogue", "transition", "centered"):
        assert f'class="{cls}"' in out, cls
    assert "McCLANE" in out and "@" not in out  # a forced character cue, not a citation
    out = pandoc(SCREENPLAY, "screenplay", to="typst", meta={"dw-scene-numbers": "true", "dw-scene-split": "true"})
    assert '#dw-scene([EXT. HARBOUR], right: [DAWN], number: "1")' in out
    assert '#dw-speech((cue: [MARA], parts: ((kind: "parenthetical", body: [\\(quietly)]),' \
           '(kind: "dialogue", body: [You came back.]),)))' in " ".join(out.split())
    assert "#dw-transition([CUT TO:])" in out


SPEECHES = """INT. KITCHEN - NIGHT

MARA
Where were you?

She waits.

MARA
I asked you something.

BRICK
Out.

STEEL ^
Nowhere.

EXT. YARD - DAY

MARA
Morning.
"""


@needs_pandoc
def test_continued_speeches_and_dual_dialogue():
    out = pandoc(SPEECHES, "screenplay", to="html")
    # Mara speaks again after action in the same scene: (CONT'D); not in the next scene.
    assert out.count("CONT") == 1 and "MARA (CONT" in out
    assert 'class="dual-dialogue"' in out and out.index("BRICK") < out.index("STEEL") and "^" not in out
    out = pandoc(SPEECHES, "screenplay", to="typst")
    assert out.count("#dw-dual(") == 1 and "(cue: [BRICK]" in out and "(cue: [STEEL]" in out
    assert out.count("#dw-speech(") == 3  # Brick and Steel speak in the dual block
    # The mark is a setting; empty turns it off.
    out = pandoc(SPEECHES, "screenplay", to="html", meta={"dw-contd": "(FORTS.)"})
    assert "MARA (FORTS.)" in out
    assert "CONT" not in pandoc(SPEECHES, "screenplay", to="html", meta={"dw-contd": ""})


@needs_pandoc
def test_av_script_rows():
    text = ("VIDEO: Harbour at dawn.\n\nAUDIO: Gulls.\n\nNARRATOR: Every morning.\n\n"
            "BILD: Hands untie a rope.\n\n## Office\n\nVIDEO: Rain on glass.\n\nTON: Rain.\n")
    out = pandoc(text, "avscript", to="typst")
    assert out.count("#dw-av(") == 2 and "#dw-av(start: 3," in out  # numbered through the script
    out = pandoc(text, "avscript", to="html", meta={"dw-av-video": "BILD", "dw-av-audio": "TON"})
    assert out.count('class="av"') == 2 and "<th>BILD</th>" in out
    assert "Every morning." in out and "<td>3</td>" in out


@needs_pandoc
def test_run_in_headings():
    text = "# Top\n\n### Third level\n\nText follows.\n"
    out = pandoc(text, "runin", to="html", meta={"dw-run-in-3": "period"})
    assert '<span id="third-level" class="run-in run-in-3">Third level.</span>\nText follows.' in out
    out = pandoc(text, "runin", to="typst", meta={"dw-run-in-3": "period"})
    assert "#dw-runin(3)[Third level] <third-level>Text follows." in out
    assert "<h3" in pandoc(text, "runin", to="html")  # without the setting, nothing changes


# -- projects ------------------------------------------------------------------------
@needs_pandoc
async def test_project_language_renders_and_keys_the_cache(rapp, rclient):
    pid = await new_project(rclient)
    r = await rclient.post(f"/api/projects/{pid}/documents",
                           json={"path": "brief.md", "text": "---\ntitle: Brief\ndate: 2026-05-01\n---\n\n\"Hallo\".\n"})
    assert r.status == 201, r.text
    doc = r.json()["id"]
    assert (await rclient.get(f"/api/projects/{pid}")).json()["lang"] is None

    async def render():
        r = await rclient.post(f"/api/projects/{pid}/export", json={"target": "html", "doc": doc})
        data = await wait_job(rclient, r.json()["job"])
        assert data["status"] == "ok", data
        return data["url"], (await rclient.get(data["url"])).text

    url_en, html = await render()
    assert "May 1, 2026" in html and "“Hallo”" in html
    r = await rclient.request("PATCH", f"/api/projects/{pid}", json={"lang": "de-at"})
    assert r.status == 200, r.text
    assert (await rclient.get(f"/api/projects/{pid}")).json()["lang"] == "de-AT"
    url_de, html = await render()
    assert url_de != url_en
    assert "1. Mai 2026" in html and "„Hallo“" in html and 'lang="de-AT"' in html
    assert (await rclient.request("PATCH", f"/api/projects/{pid}", json={"lang": "de AT"})).status == 400
    r = await rclient.request("PATCH", f"/api/projects/{pid}", json={"lang": None})
    assert r.status == 200 and (await rclient.get(f"/api/projects/{pid}")).json()["lang"] is None

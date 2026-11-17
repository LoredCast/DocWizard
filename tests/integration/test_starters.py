"""New projects start with files that fit their layout (projects/starters.py)."""

import re

import pytest

from docwizard.projects import service as svc
from docwizard.projects import starters

from .test_layouts import open_editor


async def create(client, layout, lang="en"):
    client.csrf = (await client.get("/api/me")).json()["csrf"]
    r = await client.post("/api/projects", json={"name": layout, "layout": layout},
                          headers={"Accept-Language": lang})
    assert r.status == 201, r.text
    return r.json()


async def paths_of(app, pid):
    rows = await app.db.fetchall("SELECT path FROM documents WHERE project_id = ? AND deleted_at IS NULL"
                                 " ORDER BY path", (pid,))
    return [r["path"] for r in rows]


async def text_of(app, doc_id):
    return await app.db.read(lambda c: svc.load_state(c, doc_id).text)


async def test_a_paper_starts_with_a_bibliography(app, client):
    made = await create(client, "term-paper")
    assert made["mainPath"] == "paper.md"
    assert await paths_of(app, made["id"]) == ["paper.md", "references.json"]
    text = await text_of(app, made["mainDocument"])
    assert "# Introduction" in text and "[@doe2020" in text
    # The term paper's bibliography has no heading of its own, so the starter names it, once.
    assert 'reference-section-title: "References"' in text and "# References" not in text
    # Layouts that title the bibliography themselves don't get a second heading.
    apa = await create(client, "apa-student")
    assert "reference-section-title" not in await text_of(app, apa["mainDocument"])


async def test_a_screenplay_starts_as_a_script(app, client):
    made = await create(client, "screenplay")
    assert made["mainPath"] == "script.md"
    assert await paths_of(app, made["id"]) == ["script.md"]
    text = await text_of(app, made["mainDocument"])
    assert "EXT. HARBOUR - DAWN" in text and "\nTOM\n(without looking up)\nYou came back.\n" in text
    assert "bibliography" not in text and "# Introduction" not in text
    # The editor treats it as a script.
    snap = await open_editor(client, made["id"], "script.md")
    assert snap["layout"]["script"] is True
    # The project page links the new script, and the form takes you to it.
    project = await client.get(f"/p/{made['id']}")
    assert "script.md" in project.text


async def test_the_german_drehbuch_starts_in_german(app, client):
    made = await create(client, "drehbuch")
    text = await text_of(app, made["mainDocument"])
    assert ".HAFEN – AUSSEN / MORGENGRAUEN" in text and "lang: de-DE" in text and "SCHNITT AUF:" in text


async def test_an_av_script_starts_with_rows(app, client):
    made = await create(client, "av-script")
    text = await text_of(app, made["mainDocument"])
    assert "VIDEO: Wide shot" in text and "AUDIO: SFX" in text


async def test_a_thesis_starts_with_chapters(app, client):
    made = await create(client, "thesis")
    pid = made["id"]
    assert made["mainPath"] == "thesis.md"
    assert await paths_of(app, pid) == ["chapters/01-introduction.md", "chapters/02-background.md",
                                        "chapters/03-conclusion.md", "references.json", "thesis.md"]
    project = (await client.get(f"/api/projects/{pid}")).json()
    assert project["buildInputs"] == ["thesis.md", "chapters/01-introduction.md", "chapters/02-background.md",
                                      "chapters/03-conclusion.md"]
    main = await text_of(app, made["mainDocument"])
    assert "abstract: |" in main and "bibliography: references.json" in main and "chapters/" in main
    # The thesis's own fields are there to fill in.
    assert 'institution: ""' in main or 'supervisor: ""' in main


async def test_fiction_has_no_bibliography(app, client):
    novel = await create(client, "manuscript-novel")
    assert await paths_of(app, novel["id"]) == ["chapters/01-chapter-one.md", "chapters/02-chapter-two.md",
                                                "novel.md"]
    assert "bibliography" not in await text_of(app, novel["mainDocument"])
    story = await create(client, "manuscript-story")
    assert await paths_of(app, story["id"]) == ["story.md"]
    normseite = await create(client, "normseite")
    assert "Hier beginnt die Geschichte" in await text_of(app, normseite["mainDocument"])


async def test_the_new_project_form_opens_the_starter(app, client):
    home = await client.get("/")
    csrf = re.search(r'name="_csrf" value="([^"]+)"', home.text).group(1)
    r = await client.post("/projects", form={"_csrf": csrf, "name": "Film", "layout": "screenplay", "starter": "1"})
    assert r.status == 303
    location = dict((k.lower(), v) for k, v in r.headers)["location"]
    assert location.endswith("/script.md")


@pytest.mark.parametrize("kind", starters.KINDS)
def test_every_kind_has_both_languages(kind):
    class Fake:
        manifest = {"starter": kind}
        design = {"bib.heading": "none", "text.lang": ""}

    for lang in ("en", "de"):
        plan = starters.for_layout(Fake(), lang, 'Ann "A" Lee', "2026-11-17")
        assert plan.kind == kind and plan.files
        front = plan.files[0][1]
        assert front.startswith("---\n") and 'author: "Ann \\"A\\" Lee"' in front
        assert (f"lang: {'de-DE' if lang == 'de' else 'en-US'}") in front
        if plan.build:
            assert plan.build[0] == plan.main and all(p in dict(plan.files) for p in plan.build)

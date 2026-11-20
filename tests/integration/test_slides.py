"""Slide decks through the real worker (§9.5, §9.7) and companion decks made from a paper (§6.3)."""

import io
import re
import zipfile

import pytest

from docwizard.projects.deck import companion_deck, first_sentence
from tests.integration.test_collab import new_project, open_editor
from tests.integration.test_render import HAS_TYPST, _pandoc_ok, rapp, rclient, wait_job  # noqa: F401

DECK = """---
title: Coral reefs under heat stress
subtitle: What the 2024 bleaching tells us
author: Ana Lima
---

# Background

## Why reefs matter

- A quarter of marine species
- Food for 500 million people

::: notes
Start with the human angle.
:::

## Two views

:::::: columns
::: column
Before
:::
::: column
After
:::
::::::

. . .

Later point.

---

A slide without a title.
"""

PAPER = """---
title: "Heat stress and coral bleaching"
author: Ana Lima
bibliography: refs.bib
---

# Introduction

Coral reefs cover less than 1% of the ocean floor, e.g. in the tropics. They host a quarter of species [@smith2020].

# Methods

## Sites

We surveyed 12 sites in 2024. Each was visited twice.

![Map of the sites](figures/map.png){#fig:map}

# References

::: {#refs}
:::
"""

BIB = "@article{smith2020, author={Smith, Jane}, title={Reefs}, journal={Nature}, year={2020}}\n"


def test_first_sentences_keep_abbreviations():
    assert first_sentence("Reefs grow slowly, e.g. in cold water. Then more.") == \
        "Reefs grow slowly, e.g. in cold water."
    long = " ".join(["word"] * 40) + "."
    assert first_sentence(long).endswith("…") and len(first_sentence(long).split()) == 25


def test_companion_deck_structure():
    text, paths = companion_deck(PAPER, "paper")
    assert text.startswith('---\ntitle: "Heat stress and coral bleaching"\nauthor: Ana Lima\nbibliography: refs.bib\n'
                           "slide-level: 2\n---")
    assert "## Introduction\n\n- Coral reefs cover less than 1% of the ocean floor, e.g. in the tropics.\n" in text
    assert "::: notes\nCoral reefs cover" in text and "[@smith2020]" in text
    assert "# Methods\n" in text and "## Sites\n" in text
    assert "## Map of the sites\n\n![Map of the sites](figures/map.png){#fig:map}" in text
    assert "References" not in text and "#refs" not in text
    assert paths == {"refs.bib", "figures/map.png"}


pytestmark_render = pytest.mark.skipif(not (_pandoc_ok() and HAS_TYPST), reason="needs pandoc and typst")


async def _export(client, pid, doc_id, target, options=None):
    body = {"doc": doc_id, "target": target}
    if options is not None:
        body["options"] = options
    r = await client.post(f"/api/projects/{pid}/export", json=body)
    assert r.status == 202, r.text
    data = await wait_job(client, r.json()["job"])
    assert data["status"] == "ok", (target, data)
    return await client.get(data["url"])


def _pages(pdf: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page[^s]", pdf))


@pytestmark_render
async def test_slides_render_as_pdf_handout_web_deck_and_pptx(rapp, rclient):
    pid = await new_project(rclient)
    r = await rclient.request("PATCH", f"/api/projects/{pid}", json={"layout": "slides"})
    assert r.status == 200, r.text
    await rclient.post(f"/api/projects/{pid}/documents", json={"path": "deck.md", "text": DECK})
    snap = await open_editor(rclient, pid, "deck.md")
    assert snap["layout"]["slides"] is True
    doc = snap["doc"]["id"]
    # Title, section, two slides and the untitled one: one page each.
    pdf = (await _export(rclient, pid, doc, "pdf-typst")).body
    assert pdf.startswith(b"%PDF") and _pages(pdf) == 5
    handout = (await _export(rclient, pid, doc, "pdf-typst", {"handout": True})).body
    assert handout.startswith(b"%PDF") and _pages(handout) == 2
    html = (await _export(rclient, pid, doc, "html")).text
    for n in range(1, 6):
        assert f'id="slide-{n}"' in html
    assert 'id="slide-6"' not in html
    assert '<div class="notes">' in html and "Start with the human angle." in html
    assert 'class="columns"' in html and ". . ." not in html
    assert "<script" not in html
    pptx = (await _export(rclient, pid, doc, "pptx")).body
    names = zipfile.ZipFile(io.BytesIO(pptx)).namelist()
    assert any(n.startswith("ppt/notesSlides/") for n in names)
    assert len([n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]) >= 4


@pytestmark_render
async def test_make_slides_from_a_paper(rapp, rclient):
    pid = await new_project(rclient)
    await rclient.post(f"/api/projects/{pid}/documents", json={"path": "refs.bib", "text": BIB})
    await rclient.post(f"/api/projects/{pid}/documents", json={"path": "heat.md", "text": PAPER})
    png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
           b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x35\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82")
    r = await rclient.request("PUT", f"/api/projects/{pid}/assets/figures/map.png", body=png)
    assert r.status in (200, 201), r.text
    snap = await open_editor(rclient, pid, "heat.md")
    r = await rclient.post(f"/api/documents/{snap['doc']['id']}/companion-deck")
    assert r.status == 201, r.text
    made = r.json()
    project = (await rclient.get(f"/api/projects/{made['project']}")).json()
    assert project["layout"] == "slides"
    paths = {f["path"] for f in project["tree"]}
    assert {"slides.md", "refs.bib", "figures/map.png"} <= paths
    html = (await _export(rclient, made["project"], made["document"], "html")).text
    assert "Smith, Jane. 2020" in html  # the references slide
    assert 'class="slide content-slide references"' in html
    assert "figures/map.png" in html

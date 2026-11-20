"""The check before submitting and double-blind exports (§9.11)."""

import io
import zipfile

from docwizard.render import anonymize, frontmatter, preflight
from tests.integration.test_collab import new_project, open_editor
from tests.integration.test_render import HAS_TYPST, rapp, rclient, wait_job  # noqa: F401

PAPER = """---
title: On Trams
author:
  - name: Jane Doe
    affiliation: Example University
    email: jane@example.org
thanks: Funded by the Tram Society.
word-limit: 20
page-limit: 1
bibliography: refs.bib
---

# Introduction {#sec:intro}

As [our earlier work]{.unblind}[earlier work]{.blind} showed [@doe2020, p. 3], see @fig:map and @tbl:gone.

![A map](map.png){#fig:map}

Doe also said so.

::: unblind
The code is at example.org/jdoe.
:::

## Acknowledgments

Thanks to my colleagues at Example University.

# Method

Trams run on rails.
"""
BIB = "@book{doe2020, title={Trams}}\n@article{roe2019, title={Buses}}\n"


def test_front_matter_reader():
    assert frontmatter.author_names(PAPER) == ["Jane Doe"]
    assert frontmatter.number(PAPER, "word-limit") == 20
    assert frontmatter.value(PAPER, "bibliography") == "refs.bib"
    assert frontmatter.value("---\nx: [a, 'b']\n---\n", "x") == ["a", "b"]
    assert frontmatter.author_names("---\nauthor: [Ann Lee, Bo Chen]\n---\n") == ["Ann Lee", "Bo Chen"]


def test_anonymized_markdown_keeps_the_argument_and_drops_the_authors():
    out = anonymize.markdown(PAPER)
    for gone in ("Jane Doe", "Example University", "jane@example.org", "Tram Society", "our earlier work",
                 "example.org/jdoe", "Acknowledgments", "colleagues"):
        assert gone not in out, gone
    assert "author: Anonymous" in out and "As earlier work showed" in out and "Trams run on rails." in out
    assert "word-limit: 20" in out
    normal = anonymize.markdown(PAPER, anonymize=False)
    assert "our earlier work" in normal and "[earlier work]{.blind}" not in normal and "Jane Doe" in normal
    tex = anonymize.latex(r"\author{Jane \thanks{Grant 1}}\begin{document}\section{A}x"
                          r"\section*{Acknowledgements} thanks \section{B}y\end{document}")
    assert tex == r"\author{Anonymous}\begin{document}\section{A}x\section{B}y\end{document}"


def test_checks():
    report = preflight.run({"paper.md": PAPER, "refs.bib": BIB}, ["paper.md"],
                           review={"paper.md": {"comments": 1, "suggestions": 2, "conflicts": 0}}, pages=2)
    checks = {c["id"]: c for c in report["checks"]}
    assert checks["words"]["status"] == "fail" and checks["words"]["words"] > 20
    assert checks["pages"]["status"] == "fail" and checks["pages"]["summary"] == "2 pages, 1 over the limit of 1."
    assert [i["text"] for i in checks["labels"]["items"]] == ["@tbl:gone refers to nothing"]
    assert [i["text"] for i in checks["citations"]["items"]] == ["roe2019 is in the bibliography but never cited"]
    assert checks["comments"]["status"] == "warn" and checks["suggestions"]["summary"].startswith("2 suggestions")
    anon = checks["anonymity"]
    assert anon["status"] == "warn" and "author, thanks" in anon["summary"]
    assert {i["line"] for i in anon["items"]} == {19}  # "Doe also said so."; the citation key is not a name
    assert preflight.pdf_pages(b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>") == 2


async def test_preflight_api(client, app):
    pid = await new_project(client)
    snap = await open_editor(client, pid)
    room = app.rooms.loaded(snap["doc"]["id"]) or await app.rooms.get(snap["doc"]["id"])
    await room.call(lambda: room.apply_text(PAPER, "test"))
    await client.post(f"/api/projects/{pid}/documents", json={"path": "refs.bib"})
    bib_doc = await app.db.fetchone("SELECT id FROM documents WHERE project_id = ? AND path = 'refs.bib'", (pid,))
    bib_room = await app.rooms.get(bib_doc["id"])
    await bib_room.call(lambda: bib_room.apply_text(BIB, "test"))
    r = await client.get(f"/api/projects/{pid}/preflight?doc={snap['doc']['id']}")
    assert r.status == 200, r.text
    checks = {c["id"]: c for c in r.json()["checks"]}
    assert checks["citations"]["status"] == "warn" and checks["labels"]["status"] == "fail"
    assert checks["pages"]["needsPdf"] is True


async def test_anonymized_export_and_page_count(rapp, rclient):  # noqa: F811
    pid = await new_project(rclient)
    snap = await open_editor(rclient, pid)
    room = await rapp.rooms.get(snap["doc"]["id"])
    text = PAPER.replace("bibliography: refs.bib\n", "").replace("![A map](map.png){#fig:map}", "")
    await room.call(lambda: room.apply_text(text, "test"))
    r = await rclient.post(f"/api/projects/{pid}/export", json={"target": "docx", "anonymize": True})
    job = await wait_job(rclient, r.json()["job"])
    assert job["status"] == "ok", job
    data = (await rclient.get(job["url"])).body
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        everything = "".join(z.read(n).decode("utf-8", "replace") for n in z.namelist() if n.endswith(".xml"))
    assert "Jane Doe" not in everything and "Example University" not in everything
    assert "Anonymous" in everything and "earlier work" in everything
    # A normal export shows the camera-ready wording.
    r = await rclient.post(f"/api/projects/{pid}/export", json={"target": "docx"})
    job = await wait_job(rclient, r.json()["job"])
    with zipfile.ZipFile(io.BytesIO((await rclient.get(job["url"])).body)) as z:
        body = z.read("word/document.xml").decode()
    assert "our earlier work" in body and "Example University" in body and ">earlier work<" not in body
    if HAS_TYPST:
        await room.call(lambda: room.apply_text("---\ntitle: Short\npage-limit: 1\n---\n\nOne paragraph.\n", "test"))
        report = (await rclient.get(f"/api/projects/{pid}/preflight?pages=1")).json()
        assert report["pages"] == 1, report.get("pdfError")
        assert {c["id"]: c for c in report["checks"]}["pages"]["status"] == "ok"

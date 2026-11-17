"""Layout designs, the built-in style-guide layouts and the layout editor's API (§9.3)."""

import json
import re
import shutil
import subprocess

import pytest

from docwizard.render import design as designs
from docwizard.render import jobs
from docwizard.render.layouts import REPO_LAYOUTS, Layouts
from tests.asgi_client import Client
from tests.integration.test_collab import new_project, open_editor
from tests.integration.test_render import HAS_TYPST, _pandoc_ok, _path, rapp, rclient, wait_job  # noqa: F401

NEW_LAYOUTS = ("oxford", "uni-wien-philosophie", "aps", "turabian", "mla", "apa-student", "mhra", "ieee", "screenplay",
               "drehbuch", "av-script", "manuscript-story", "manuscript-novel", "normseite", "book-manuscript",
               "goettingen-vcm", "wuerzburg-germanistik")


# -- designs -------------------------------------------------------------------
def test_design_validation():
    assert designs.validate({"text.size": 12, "page.margin-top": " 2cm ", "text.link-color": "#1A4FA0"}) == \
        {"text.size": 12.0, "page.margin-top": "2cm", "text.link-color": "1a4fa0"}
    for bad in ({"text.size": 40}, {"page.margin-top": "2cm; }"}, {"text.font": 'X") #panic('},
                {"nonsense": 1}, {"page.paper": "a0"}, {"cover.fields": [{"key": "password"}]},
                {"cover.fields": [{"space": "1em; x"}]}, {"text.justify": "yes"}, {"text.lang": "de AT"}):
        with pytest.raises(designs.DesignError):
            designs.validate(bad)
    with pytest.raises(designs.DesignError):
        designs.validate({"cite.style": "nope"}, styles={"apa"})
    full = designs.complete({"text.size": 99, "text.font": "Tinos"})
    assert full["text.size"] == designs.DEFAULTS["text.size"] and full["text.font"] == "Tinos"


def test_generated_files_escape_user_text():
    d = designs.complete({"declaration.text": 'Quote " and \\ and $x$ and #eval("1")',
                          "cover.fields": [{"text": "<script>$x$</script>"}, {"key": "course", "label": "A & B"}]})
    files = designs.generated_files(d)
    typ = files["typst/design.typ"]
    assert '"declaration-text": "Quote \\" and \\\\ and $$x$$ and #eval(\\"1\\")"' in typ
    cover = files["html/cover.html"]
    assert "&lt;script&gt;$$x$$&lt;/script&gt;" in cover and "A &amp; B" in cover and "<script>" not in cover


def test_builtin_layouts_resolve_with_design_defaults(tmp_path):
    layouts = Layouts([REPO_LAYOUTS], tmp_path)
    ids = {lay.id for lay in layouts.list()}
    assert {"default", "term-paper", "article", *NEW_LAYOUTS} <= ids
    for lay in layouts.list():
        assert lay.designable, lay.id
        designs.validate(lay.manifest.get("design", {}))  # every shipped design is valid
        for name, spec in lay.variables.items():
            if spec.get("type") == "enum":
                assert spec["default"] in spec["values"], (lay.id, name)
    wien = layouts.get("uni-wien-philosophie")
    assert wien.design["text.font"] == "Tinos" and wien.design["declaration.enabled"]
    assert wien.variables["citation-style"]["default"] == "uni-wien-philosophie"
    assert wien.option_defaults()["fontsize"] == "12pt" and wien.option_defaults()["toc"] is True
    aps = layouts.get("aps")
    assert aps.design["heading.numbering"] == "I.A.1" and aps.design["caption.figure"] == "FIG."
    # Generated files land next to the templates in the cached copy.
    cached = layouts.cached_dir(wien)
    assert '"text-font": "Tinos"' in (cached / "typst" / "design.typ").read_text()
    assert "Matrikelnummer:" in (cached / "html" / "cover.html").read_text()
    assert "font-family: Tinos" in (cached / "html" / "design.css").read_text()
    meta = jobs.metadata(wien, {}, ["paper.md"], cached)
    # The heading is a term, written in the document's language by lang.lua (§9.8).
    assert meta["lang"] == "de-AT" and meta["dw-bib-heading"] == "bibliography"
    assert "reference-section-title" not in meta
    assert meta["csl"] == "uni-wien-philosophie.csl"
    steps, _ = jobs.pandoc_steps(wien, cached, "pdf-typst", ["paper.md"], {}, True)
    assert "--toc" in steps[0]["args"] and designs.SHARED_FONTS_DIR in steps[1]["args"]
    steps, _ = jobs.pandoc_steps(wien, cached, "pdf-typst", ["paper.md"], {"toc": False}, True)
    assert "--toc" not in steps[0]["args"]


def test_custom_layouts_inherit_and_choose_options(tmp_path):
    layouts = Layouts([REPO_LAYOUTS], tmp_path)
    layouts.set_custom({
        "mine-1": ({"name": "Mine", "extends": "oxford", "design": {"text.size": 12}, "options": ["fontsize", "toc"]},
                   7, False),
        "orphan": ({"name": "Orphan", "extends": "gone"}, 7, True),
    })
    mine = layouts.get("mine-1")
    assert mine.custom and mine.owner == 7 and mine.parent == "oxford"
    assert mine.design["text.font"] == "Noto Serif" and mine.design["text.size"] == 12
    assert set(mine.variables) == {"fontsize", "toc"} and mine.variables["fontsize"]["default"] == "12pt"
    assert not layouts.exists("orphan")  # a missing parent drops the layout instead of failing
    assert "mine-1" not in {lay.id for lay in layouts.visible(8)}
    assert "mine-1" in {lay.id for lay in layouts.visible(7)}


def test_turabian_follows_its_manual(tmp_path):
    layouts = Layouts([REPO_LAYOUTS], tmp_path)
    tur = layouts.get("turabian")
    d = tur.design
    assert d["page.number-distance"] == "0.5in" and d["cover.title-colon"] and d["caption.align"] == "left"
    assert d["footnote.number"] == "period" and d["toc.title"] == ""
    cached = layouts.cached_dir(tur)
    meta = jobs.metadata(tur, {}, ["paper.md"], cached)
    # Turabian's 9th edition follows Chicago's 17th: full first notes, then author-title short notes.
    assert meta["csl"] == "chicago-notes-bibliography-subsequent-author-title-17th-edition.csl"
    assert meta["dw-bib-heading"] == "bibliography"
    meta = jobs.metadata(tur, {"citation-style": "chicago-author-date-17th-edition"}, ["paper.md"], cached)
    assert meta["dw-bib-heading"] == "reference-list"
    assert '"page-number-distance": 0.5in' in (cached / "typst" / "design.typ").read_text()
    assert designs.validate({"quote.spacing": ""}) == {"quote.spacing": ""}


def test_csl_json_gets_short_titles():
    refs = [{"id": "a", "type": "book", "title": "Driving While Brown: Sheriff Joe Arpaio Versus the Latino Resistance"},
            {"id": "b", "type": "book", "title": "Main: Sub", "title-short": "Own"},
            {"id": "c", "type": "book", "title": "No Subtitle"}]
    out = json.loads(jobs.with_short_titles(json.dumps(refs)))
    assert out[0]["title-short"] == "Driving While Brown" and out[1]["title-short"] == "Own" and "title-short" not in out[2]
    for text in ('{"title": "A: B"}', "not json", '[{"title": "A: B"}]'):
        assert jobs.with_short_titles(text) == text


CHICAGO_REFS = [
    {"id": "yu", "type": "book", "author": [{"family": "Yu", "given": "Charles"}], "title": "Interior Chinatown",
     "publisher": "Pantheon Books", "publisher-place": "New York", "issued": {"date-parts": [[2020]]}},
    {"id": "aubrecht", "type": "book", "editor": [{"family": "Aubrecht", "given": "Katie"},
     {"family": "Kelly", "given": "Christine"}, {"family": "Rice", "given": "Carla"}],
     "title": "The Aging-Disability Nexus", "publisher": "UBC Press", "publisher-place": "Vancouver",
     "issued": {"date-parts": [[2020]]}},
]


@pytest.mark.skipif(not _pandoc_ok(), reason="needs pandoc")
@pytest.mark.parametrize("style, expected", [
    # Chicago 18th edition (13.22, 13.23): no place of publication, et al. for three or more in notes.
    ("chicago-notes-bibliography", ["Charles Yu, Interior Chinatown (Pantheon Books, 2020), 95–96.",
                                    "Yu, Interior Chinatown, 101.", "Katie Aubrecht et al., eds.",
                                    "Yu, Charles. Interior Chinatown. Pantheon Books, 2020."]),
    ("chicago-author-date", ["(Yu 2020, 95–96)", "Yu, Charles. 2020. Interior Chinatown. Pantheon Books."]),
    # Turabian 9th edition (Chicago 17th): place of publication, no ibid.
    ("chicago-notes-bibliography-subsequent-author-title-17th-edition",
     ["Charles Yu, Interior Chinatown (New York: Pantheon Books, 2020), 95–96.", "Yu, Interior Chinatown, 101.",
      "Yu, Charles. Interior Chinatown. New York: Pantheon Books, 2020."]),
])
def test_chicago_styles_match_the_manuals(tmp_path, style, expected):
    (tmp_path / "refs.json").write_text(json.dumps(CHICAGO_REFS))
    (tmp_path / "doc.md").write_text("---\nbibliography: refs.json\n---\nA.[@yu, 95-96] B.[@yu, 101] C.[@aubrecht, 44]\n")
    out = subprocess.run([shutil.which("pandoc", path=_path()), "doc.md", "--citeproc", "--csl",
                          str(REPO_LAYOUTS.parent / "csl" / f"{style}.csl"), "-t", "plain", "--wrap=none"],
                         cwd=tmp_path, capture_output=True, text=True, check=True).stdout
    for text in expected:
        assert text in out, (text, out)


# -- API -----------------------------------------------------------------------
async def test_layout_editor_api(app, client):
    client.csrf = (await client.get("/api/me")).json()["csrf"]
    schema = (await client.get("/api/layouts/schema")).json()
    keys = {f["key"] for f in schema["fields"]}
    assert {"text.font", "page.margin-left", "heading.1.size", "cover.fields", "cite.style"} <= keys
    assert "uni-wien-philosophie" in next(f for f in schema["fields"] if f["key"] == "cite.style")["values"]

    r = await client.post("/api/layouts", json={"name": "My thesis", "extends": "uni-wien-philosophie",
                                                "design": {"text.size": 11, "bib.title": "Quellen"}})
    assert r.status == 201, r.text
    lid = r.json()["id"]
    assert lid.startswith("my-thesis-") and r.json()["editable"] and not r.json()["shared"]
    got = (await client.get(f"/api/layouts/{lid}")).json()
    assert got["design"]["text.size"] == 11 and got["design"]["text.font"] == "Tinos"
    assert got["parentDesign"]["text.size"] == 12 and got["own"]["design"] == {"text.size": 11, "bib.title": "Quellen"}
    assert set(got["variables"]) == set(app.layouts.get("uni-wien-philosophie").variables)

    bad = await client.request("PATCH", f"/api/layouts/{lid}", json={"design": {"page.margin-top": "1 inch"}})
    assert bad.status == 400 and "length" in bad.json()["error"]["message"]
    loop = await client.request("PATCH", f"/api/layouts/{lid}", json={"extends": lid})
    assert loop.status == 400
    r = await client.request("PATCH", f"/api/layouts/{lid}", json={"design": {"text.size": 11.5},
                                                                    "options": ["fontsize"], "shared": True})
    assert r.status == 200, r.text
    lay = app.layouts.get(lid)
    assert lay.design["text.size"] == 11.5 and lay.design["bib.heading"] == "bibliography"
    assert list(lay.variables) == ["fontsize"] and lay.shared

    # Projects can use it, and it survives a restart of the layout registry.
    r = await client.post("/api/projects", json={"name": "Thesis", "layout": lid})
    assert r.status == 201, r.text
    await app.load_custom_layouts()
    assert app.layouts.exists(lid)

    # Pages
    gallery = await client.get("/layouts")
    assert gallery.status == 200 and "My thesis" in gallery.text and "Physical Review (APS)" in gallery.text
    page = await client.get(f"/layouts/{lid}")
    assert page.status == 200 and "layouts/editor.js" in page.text

    # Built-in layouts can't be changed or deleted.
    assert (await client.request("PATCH", "/api/layouts/oxford", json={"name": "X"})).status == 403
    assert (await client.request("DELETE", "/api/layouts/oxford")).status == 403
    assert (await client.request("DELETE", f"/api/layouts/{lid}")).status == 200
    assert not app.layouts.exists(lid)


async def test_private_layouts_belong_to_their_owner(app, client):
    from docwizard.auth import passwords, users

    client.csrf = (await client.get("/api/me")).json()["csrf"]
    r = await client.post("/api/layouts", json={"name": "Private", "extends": "default"})
    lid = r.json()["id"]
    pw = passwords.hash_password("another good password")
    await app.db.write(lambda c: users.create(c, "bob", pw, display_name="Bob"))
    bob = Client(app)
    await bob.login("bob", "another good password")
    bob.csrf = (await bob.get("/api/me")).json()["csrf"]
    assert (await bob.get(f"/api/layouts/{lid}")).status == 404
    assert lid not in {x["id"] for x in (await bob.get("/api/layouts")).json()["layouts"]}
    await client.request("PATCH", f"/api/layouts/{lid}", json={"shared": True})
    assert (await bob.get(f"/api/layouts/{lid}")).status == 200
    assert (await bob.request("PATCH", f"/api/layouts/{lid}", json={"name": "Mine now"})).status == 403
    copy = await bob.post("/api/layouts", json={"name": "Bob's copy", "extends": lid})
    assert copy.status == 201 and copy.json()["parent"] == lid
    # A layout others build on can't be deleted.
    assert (await client.request("DELETE", f"/api/layouts/{lid}")).status == 400


# -- front matter fields (§9.4) --------------------------------------------------
def _where(layouts, lid):
    from docwizard.render import fields
    return {item["key"]: item["where"] for item in fields.shown(layouts.get(lid).design)}


def test_layouts_list_the_fields_they_print(tmp_path):
    from docwizard.render import fields

    layouts = Layouts([REPO_LAYOUTS], tmp_path)
    for lay in layouts.list():
        info = fields.for_layout(lay)
        keys = [item["key"] for item in info["shown"]]
        assert keys[:2] == ["title", "subtitle"], lay.id
        assert all(key in info["catalog"] for key in keys + info["general"]), lay.id
        assert not set(keys) & set(info["general"]), lay.id
        for item in lay.design["cover.fields"]:
            if "key" in item:
                assert item["key"] in keys or item["key"] in ("byline", "place-date"), (lay.id, item)

    turabian = _where(layouts, "turabian")
    assert list(turabian) == ["title", "subtitle", "author", "course", "instructor", "date"]
    assert turabian["course"] == [{"at": "cover"}]
    # Labels the layout prints before a value, and entries made from several fields.
    wien = _where(layouts, "uni-wien-philosophie")
    assert wien["student-id"] == [{"at": "cover", "label": "Matrikelnummer:"}]
    assert wien["place"] == [{"at": "cover", "as": "place-date"}] and "date" in wien
    assert wien["declaration"] == [{"at": "declaration"}]
    # The first page's corners, the running header and the page numbers count too.
    story = _where(layouts, "manuscript-story")
    assert story["author"] == [{"at": "corner-left"}, {"at": "cover", "as": "byline"},
                               {"at": "page-number", "as": "surname"}]
    assert story["word-count"] == [{"at": "corner-right"}] and story["short-title"] == [{"at": "page-number"}]
    assert "short-title" not in _where(layouts, "turabian")
    assert {"at": "header"} in _where(layouts, "normseite")["title"]
    assert "abstract" in _where(layouts, "ieee") and "abstract" not in _where(layouts, "mla")
    assert fields.for_layout(layouts.get("turabian"))["titlePage"] is True
    # The editor shows the field texts in the interface language.
    from docwizard import i18n
    de = i18n.catalog("de")
    for entry in fields.describe(designs.complete({}))["catalog"].values():
        assert entry["label"] in de and entry["help"] in de, entry["key"]


def test_fields_follow_a_changed_design():
    from docwizard.render import fields

    design = designs.complete({"cover.fields": [{"key": "supervisor", "label": "Betreuung:"}, {"key": "byline"}],
                               "page.header": "author", "cover.abstract": False})
    shown = {item["key"]: item["where"] for item in fields.shown(design)}
    assert list(shown) == ["title", "subtitle", "supervisor", "author"]
    assert shown["author"] == [{"at": "cover", "as": "byline"}, {"at": "header"}]
    entry = fields.entry("affiliation")
    assert entry["in"] == "author" and entry["topLevel"] is False and "${}" in entry["snippet"]


async def test_editor_and_new_documents_show_the_layout_fields(app, client):
    client.csrf = (await client.get("/api/me")).json()["csrf"]
    r = await client.post("/api/projects", json={"name": "Wien", "layout": "uni-wien-philosophie"})
    assert r.status == 201, r.text
    pid = r.json()["id"]
    snap = await open_editor(client, pid)
    keys = [item["key"] for item in snap["fields"]["shown"]]
    assert {"course", "instructor", "student-id", "program", "place", "declaration"} <= set(keys)
    assert snap["fields"]["catalog"]["student-id"]["example"] == 'student-id: "01234567"'
    assert "lang" in snap["fields"]["general"]
    # The starter document has a line for each field to fill in, and they render as nothing until then.
    assert 'course: ""\ninstructor: ""' in snap["text"] and 'student-id: ""' in snap["text"]
    assert snap["text"].index("date:") < snap["text"].index("paper-type:") < snap["text"].index("lang:")
    # The default layout adds nothing beyond title, author and date.
    plain = await open_editor(client, await new_project(client))
    assert "course:" not in plain["text"] and [i["key"] for i in plain["fields"]["shown"]][:2] == ["title", "subtitle"]
    # The layout API and the gallery say which fields a layout prints.
    got = (await client.get("/api/layouts/mla")).json()
    assert [i["key"] for i in got["fields"]["shown"]] == ["title", "subtitle", "author", "instructor", "course", "date"]
    gallery = await client.get("/layouts")
    assert "Front matter fields:" in gallery.text and "<code>student-id</code>" in gallery.text


# -- rendering -------------------------------------------------------------------
@pytest.mark.skipif(not _pandoc_ok(), reason="needs pandoc")
async def test_new_layouts_render(rapp, rclient):
    if not HAS_TYPST:
        pytest.skip("needs typst")
    pid = await new_project(rclient)
    snap = await open_editor(rclient, pid)
    project = dict(await rapp.db.fetchone("SELECT * FROM projects WHERE id = ?", (pid,)))
    for lid in ("default", "term-paper", "article", *NEW_LAYOUTS):
        for target in ("pdf-typst", "html"):
            res = await rapp.renderer.render(project, target, doc_path=snap["doc"]["path"], layout_id=lid)
            assert res.status == "ok", (lid, target, res.diagnostics)
    html = (rapp.renderer.renders_dir / res.key / res.output).read_text()
    assert "design.css" in html


@pytest.mark.skipif(not _pandoc_ok(), reason="needs pandoc")
async def test_layout_editor_preview(rapp, rclient):
    if not HAS_TYPST:
        pytest.skip("needs typst")
    rclient.csrf = (await rclient.get("/api/me")).json()["csrf"]
    body = {"extends": "turabian", "design": {"text.font": "Noto Serif", "cover.fields": [{"key": "author"}]}}
    r = await rclient.post("/api/layouts/preview", json=body)
    data = r.json()
    assert r.status == 200 and data["status"] == "ok", data
    pdf = await rclient.get(data["url"])
    assert pdf.status == 200 and pdf.body.startswith(b"%PDF")
    # Draft layout copies are removed after the render; the result stays cached.
    assert not any((rapp.cfg.cache_dir / "layout-drafts").glob("*/*"))
    again = await rclient.post("/api/layouts/preview", json=body)
    assert again.json()["url"] == data["url"]
    bad = await rclient.post("/api/layouts/preview", json={"design": {"text.size": "huge"}})
    assert bad.status == 400
    # Project render URLs can't be fetched through the preview route.
    assert (await rclient.get(data["url"].replace(".pdf", ".html"))).status == 404


@pytest.mark.skipif(not _pandoc_ok() or not shutil.which("pdftotext"), reason="needs pandoc and pdftotext")
async def test_screenplay_speeches_continue_across_pages(rapp, rclient):
    """A speech that runs over a page break ends with (MORE) and goes on under the cue with (CONT'D)."""
    if not HAS_TYPST:
        pytest.skip("needs typst")
    pid = await new_project(rclient)
    lines = "\n".join(f"Line {i} of a very long speech that goes on and on." for i in range(1, 80))
    text = ("---\ntitle: Long\n---\n\nINT. HALL - NIGHT\n\n" + "She paces.\n\n" * 18 +
            "MARA\n" + lines + "\n\nBRICK\nRight.\n\nSTEEL ^\nWrong.\n")
    await rclient.post(f"/api/projects/{pid}/documents", json={"path": "film.md", "text": text})
    project = dict(await rapp.db.fetchone("SELECT * FROM projects WHERE id = ?", (pid,)))
    res = await rapp.renderer.render(project, "pdf-typst", doc_path="film.md", layout_id="screenplay")
    assert res.status == "ok", res.diagnostics
    pdf = rapp.renderer.renders_dir / res.key / res.output
    pages = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True,
                           check=True).stdout.split("\f")
    first = next(i for i, p in enumerate(pages) if "Line 1 of" in p)
    assert "(MORE)" in pages[first] and "MARA (CONT'D)" in pages[first + 1], pages[first:first + 2]
    assert "(MORE)" not in pages[-1] or "Line 79" not in pages[-1]
    # Dual dialogue: both cues on the same line.
    assert any(re.search(r"BRICK\s+STEEL", p) for p in pages)

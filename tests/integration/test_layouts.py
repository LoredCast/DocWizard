"""Layout designs, the built-in style-guide layouts and the layout editor's API (§9.3)."""

import json
import re

import pytest

from docwizard.render import design as designs
from docwizard.render import jobs
from docwizard.render.layouts import REPO_LAYOUTS, Layouts
from tests.asgi_client import Client
from tests.integration.test_collab import new_project, open_editor
from tests.integration.test_render import HAS_TYPST, _pandoc_ok, rapp, rclient, wait_job  # noqa: F401

NEW_LAYOUTS = ("oxford", "uni-wien-philosophie", "aps", "turabian")


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
    assert meta["lang"] == "de-AT" and meta["reference-section-title"] == "Literaturverzeichnis"
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
    assert lay.design["text.size"] == 11.5 and lay.design["bib.title"] == "Literaturverzeichnis"
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

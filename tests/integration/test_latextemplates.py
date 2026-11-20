"""Browsing and one-click installation of templates from latextemplates.com (§9.3)."""

from __future__ import annotations

import io
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from docwizard.auth import passwords, users
from docwizard.render import jobs, latextemplates
from docwizard.render.layouts import _ID as LAYOUT_ID_RE
from tests.asgi_client import Client


def _make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
def fake_site():
    """Serve a minimal latextemplates.com-compatible static site on loopback."""
    state = {"hits": [], "fail_index": False}

    index_payload = {
        "version": 1,
        "site": "https://latextemplates.com",
        "categories": [
            {"slug": "curricula-vitae", "title": "Curricula Vitae/Résumés", "blurb": "CVs and resumes",
             "url": "/cat/curricula-vitae/", "templates": 2, "rebuilt": 1},
            {"slug": "theses", "title": "Theses", "blurb": "Dissertation templates",
             "url": "/cat/theses/", "templates": 1, "rebuilt": 1},
            {"slug": "academic-journals", "title": "Academic Journals", "blurb": "Papers and articles",
             "url": "/cat/academic-journals/", "templates": 1, "rebuilt": 1},
        ],
        "templates": [
            {
                "slug": "deedy-resume",
                "title": "Deedy Resume",
                "category": "curricula-vitae",
                "url": "/template/deedy-resume/",
                "humanHits": 50000,
                "rebuilt": True,
                "downloadUrl": "/downloads/deedy-resume.zip",
                "engine": "xelatex",
            },
            {
                "slug": "broken-cv",
                "title": "Broken CV",
                "category": "curricula-vitae",
                "url": "/template/broken-cv/",
                "humanHits": 99999,
                "rebuilt": False,
                "downloadUrl": "/downloads/broken-cv.zip",
                "engine": "pdflatex",
            },
            {
                "slug": "masters-doctoral-thesis",
                "title": "Masters/Doctoral Thesis",
                "category": "theses",
                "url": "/template/masters-doctoral-thesis/",
                "humanHits": 80000,
                "rebuilt": True,
                "downloadUrl": "/downloads/masters-doctoral-thesis.zip",
                "engine": "pdflatex",
            },
            {
                "slug": "no-tex-archive",
                "title": "Empty Archive",
                "category": "academic-journals",
                "url": "/template/no-tex-archive/",
                "humanHits": 10,
                "rebuilt": True,
                "downloadUrl": "/downloads/no-tex-archive.zip",
                "engine": "lualatex",
            },
        ],
    }

    search_payload = [
        {"kind": "category", "title": "Curricula Vitae/Résumés", "url": "/cat/curricula-vitae/",
         "text": "CVs", "alt": ["resume", "curriculum vitae", "lebenslauf"]},
        {"kind": "template", "title": "Deedy Resume", "url": "/template/deedy-resume/",
         "context": "Curricula Vitae/Résumés",
         "text": "A clean two-column computer science CV using XeLaTeX.", "dl": True},
        {"kind": "template", "title": "Masters/Doctoral Thesis", "url": "/template/masters-doctoral-thesis/",
         "context": "Theses",
         "text": "A complete thesis layout with chapters and bibliography.", "dl": True},
    ]

    font_bytes = b"\x00\x01\x00\x00FAKEFONT\xff\xfe"
    zips = {
        "/downloads/deedy-resume.zip": _make_zip({
            "deedy-resume/main.tex": b"\\documentclass{deedy}\n\\begin{document}\nHello\\end{document}\n",
            "deedy-resume/deedy.cls": "% R\xe9sum\xe9 class\n\\ProvidesClass{deedy}\n".encode("latin-1"),
            "deedy-resume/refs.bib": b"@book{knuth, title={TeXbook}}\n",
            "deedy-resume/fonts/demo.ttf": font_bytes,
            "deedy-resume/filters/evil.lua": b"os.execute('id')\n",
            "../escape.txt": b"nope",
            "__MACOSX/._main.tex": b"junk",
        }),
        "/downloads/masters-doctoral-thesis.zip": _make_zip({
            "thesis.tex": b"\\documentclass{book}\n\\begin{document}\nHi\\end{document}\n",
        }),
        "/downloads/no-tex-archive.zip": _make_zip({
            "README.txt": b"No TeX files here.",
        }),
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["hits"].append(self.path)
            if self.path == "/index.json":
                if state["fail_index"]:
                    self.send_response(503)
                    self.end_headers()
                    return
                body = json.dumps(index_payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/search.json":
                body = json.dumps(search_payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path in zips:
                body = zips[self.path]
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", state, font_bytes
    finally:
        server.shutdown()
        server.server_close()


async def _wire_site(app, url: str) -> None:
    app.latextemplates_url = url
    await app.set_setting("allow_private_outbound", ["127.0.0.1"])


async def test_catalog_search_filters_and_caches(app, client, fake_site):
    url, state, _ = fake_site
    await _wire_site(app, url)

    # Initial load returns only compile-verified templates sorted by hits.
    r = await client.get("/api/layouts/catalog")
    assert r.status == 200
    data = r.json()
    slugs = [item["slug"] for item in data["items"]]
    assert slugs == ["masters-doctoral-thesis", "deedy-resume", "no-tex-archive"]
    assert "broken-cv" not in slugs
    assert data["total"] == 3
    assert {c["slug"]: c["count"] for c in data["categories"]} == {
        "curricula-vitae": 1, "theses": 1, "academic-journals": 1,
    }
    first_hits = len(state["hits"])

    # Category synonym search ("lebenslauf" from search.json alt) finds Deedy Resume without re-fetching.
    r = await client.get("/api/layouts/catalog?q=lebenslauf")
    assert r.status == 200
    assert [i["slug"] for i in r.json()["items"]] == ["deedy-resume"]
    assert len(state["hits"]) == first_hits

    # Engine and category filtering + pagination.
    r = await client.get("/api/layouts/catalog?engine=xelatex")
    assert [i["slug"] for i in r.json()["items"]] == ["deedy-resume"]
    r = await client.get("/api/layouts/catalog?category=theses")
    assert [i["slug"] for i in r.json()["items"]] == ["masters-doctoral-thesis"]
    r = await client.get("/api/layouts/catalog?limit=1&offset=1")
    page = r.json()
    assert page["total"] == 3 and [i["slug"] for i in page["items"]] == ["deedy-resume"]

    # Disk cache survives clearing in-memory cache and serves stale data when upstream fails.
    app._lt_catalog = None
    state["fail_index"] = True
    r = await client.get("/api/layouts/catalog?refresh=1")
    assert r.status == 200 and r.json()["total"] == 3


async def test_install_create_project_and_uninstall(app, client, fake_site):
    url, _, font_bytes = fake_site
    await _wire_site(app, url)

    pw = passwords.hash_password("correct horse battery")
    await app.db.write(lambda c: users.create(c, "alice", pw, display_name="Alice", role="user"))
    await app.db.write(lambda c: users.create(c, "bob", pw, display_name="Bob", role="user"))

    alice = Client(app)
    await alice.login("alice", "correct horse battery")
    bob = Client(app)
    await bob.login("bob", "correct horse battery")

    # Alice installs Deedy Resume in one click.
    r = await alice.post("/api/layouts/catalog/install", json={"slug": "deedy-resume"})
    assert r.status == 201
    res = r.json()
    assert res["alreadyInstalled"] is False
    lay_info = res["layout"]
    assert lay_info["id"] == "lt-deedy-resume"
    assert lay_info["engine"] == "xelatex"
    assert lay_info["uninstallable"] is True
    assert lay_info["source"]["slug"] == "deedy-resume"

    # Archive extraction strips wrapper dir, drops .lua / traversal / __MACOSX.
    starter_dir = app.cfg.data_dir / "layouts" / "lt-deedy-resume" / "starter"
    assert (starter_dir / "main.tex").is_file()
    assert (starter_dir / "deedy.cls").is_file()
    assert (starter_dir / "refs.bib").is_file()
    assert (starter_dir / "fonts" / "demo.ttf").read_bytes() == font_bytes
    assert not (starter_dir / "filters" / "evil.lua").exists()
    assert not (app.cfg.data_dir / "layouts" / "lt-deedy-resume" / "escape.txt").exists()
    assert not any("__MACOSX" in str(p) for p in starter_dir.rglob("*"))

    # Layout cache skips starter/ files so they aren't duplicated into render layout cache.
    lay = app.layouts.get("lt-deedy-resume")
    cached = app.layouts.cached_dir(lay)
    assert not (cached / "starter").exists()

    # Second install is idempotent.
    r = await alice.post("/api/layouts/catalog/install", json={"slug": "deedy-resume"})
    assert r.status == 200 and r.json()["alreadyInstalled"] is True

    # Catalog reflects installed + per-user uninstallable permissions.
    r_alice = await alice.get("/api/layouts/catalog?q=deedy")
    item_a = r_alice.json()["items"][0]
    assert item_a["installed"] is True and item_a["uninstallable"] is True
    r_bob = await bob.get("/api/layouts/catalog?q=deedy")
    item_b = r_bob.json()["items"][0]
    assert item_b["installed"] is True and item_b["uninstallable"] is False

    # /layouts page renders the installed template and the LaTeX Templates tab.
    page = await alice.get("/layouts")
    assert page.status == 200
    assert "Deedy Resume" in page.text and "tab-catalog" in page.text and "lt-deedy-resume" in page.text

    # Creating a project with the installed layout unpacks docs + binary assets and sets buildInputs.
    pr = await alice.post("/api/projects", json={"name": "My CV", "layout": "lt-deedy-resume", "starter": True})
    assert pr.status == 201
    proj = pr.json()
    assert proj["mainPath"] == "main.tex"
    info = (await alice.get(f"/api/projects/{proj['id']}")).json()
    assert info["buildInputs"] == ["main.tex"]
    doc_paths = {e["path"] for e in info["tree"] if e["type"] == "document"}
    asset_paths = {e["path"] for e in info["tree"] if e["type"] == "asset"}
    assert doc_paths == {"main.tex", "deedy.cls", "refs.bib"}
    assert asset_paths == {"fonts/demo.ttf"}
    # Binary font is materialized on disk next to the TeX sources.
    assert (app.rooms.project_dir(proj["id"]) / "fonts" / "demo.ttf").read_bytes() == font_bytes
    # LaTeX build uses the template's declared engine (xelatex).
    steps, _ = jobs.latex_steps(lay, "pdf-latex", "main.tex")
    assert steps[0]["tool"] == "latexmk" and steps[0]["args"][0] == "-xelatex"

    # Bob cannot uninstall Alice's template; built-in layouts cannot be uninstalled here.
    assert (await bob.request("DELETE", "/api/layouts/catalog/lt-deedy-resume")).status == 403
    assert (await client.request("DELETE", "/api/layouts/catalog/default")).status == 400

    # Alice (installer) can uninstall it cleanly.
    r = await alice.request("DELETE", "/api/layouts/catalog/lt-deedy-resume")
    assert r.status == 200
    assert not app.layouts.exists("lt-deedy-resume")
    assert not (app.cfg.data_dir / "layouts" / "lt-deedy-resume").exists()


async def test_install_validation_and_long_slug_id(app, client, fake_site):
    url, _, _ = fake_site
    await _wire_site(app, url)

    # Invalid slug and missing template are rejected.
    assert (await client.post("/api/layouts/catalog/install", json={"slug": "../etc/passwd"})).status == 400
    assert (await client.post("/api/layouts/catalog/install", json={"slug": "does-not-exist"})).status == 400
    # Archive without any .tex file is rejected cleanly without leaving partial folders.
    assert (await client.post("/api/layouts/catalog/install", json={"slug": "no-tex-archive"})).status == 400
    assert not app.layouts.exists("lt-no-tex-archive")

    # Very long slugs still map to valid DocWizard layout IDs (<= 63 chars).
    long_slug = "a" * 90 + "-template"
    lid = latextemplates.layout_id_for(long_slug)
    assert len(lid) <= 63 and LAYOUT_ID_RE.match(lid)

"""Rendering through the real worker with pandoc (and Typst when installed) (§9, §20).

Set DOCWIZARD_TEST_TOOLS_PATH to a directory with pandoc ≥ 3.9 and typst to
run these against specific tool versions.
"""

import asyncio
import os
import re
import shutil
import subprocess

import pytest

from tests.asgi_client import Client
from tests.integration.test_collab import connect, new_project, open_editor


def _pandoc_ok():
    exe = shutil.which("pandoc", path=_path())
    if not exe:
        return False
    out = subprocess.run([exe, "--version"], capture_output=True, text=True).stdout
    m = re.search(r"pandoc (\d+)\.(\d+)", out)
    return bool(m) and (int(m.group(1)), int(m.group(2))) >= (3, 1)


def _path():
    extra = os.environ.get("DOCWIZARD_TEST_TOOLS_PATH")
    return f"{extra}:{os.environ['PATH']}" if extra else os.environ["PATH"]


pytestmark = pytest.mark.skipif(not _pandoc_ok(), reason="needs pandoc")
HAS_TYPST = shutil.which("typst", path=_path()) is not None


@pytest.fixture
async def rapp(cfg, monkeypatch):
    import tempfile

    from docwizard.asgi import create_app
    from docwizard.auth import passwords, users

    monkeypatch.setenv("PATH", _path())
    cfg.render.spawn_worker = True
    # When the worker runs as root, jobs run as other users: their directories
    # must be reachable (pytest's tmp_path is private to root).
    shared = tempfile.mkdtemp(prefix="dw-render-")
    os.chmod(shared, 0o755)
    cfg.server.jobs_dir = os.path.join(shared, "jobs")
    cfg.server.cache_dir = os.path.join(shared, "cache")
    app = create_app(cfg)
    await app.startup()
    pw = passwords.hash_password("correct horse battery")
    await app.db.write(lambda c: users.create(c, "jane", pw, display_name="Jane Doe"))
    yield app
    await app.shutdown()
    shutil.rmtree(shared, ignore_errors=True)


@pytest.fixture
async def rclient(rapp):
    cl = Client(rapp)
    await cl.login("jane", "correct horse battery")
    return cl


async def wait_job(client, job_id):
    for _ in range(300):
        r = await client.get(f"/api/jobs/{job_id}")
        data = r.json()
        if data["status"] not in ("queued", "running"):
            return data
        await asyncio.sleep(0.1)
    raise AssertionError("export timed out")


async def test_html_preview_on_preview_origin(rapp, rclient):
    pid = await new_project(rclient)
    snap = await open_editor(rclient, pid)
    ws = await connect(rapp, rclient, snap)
    await ws.send({"t": "render", "doc": snap["doc"]["id"], "version": snap["version"], "target": "html"})
    msg = await ws.recv_until("rendered", timeout=60)
    assert msg["status"] == "ok", msg
    url = msg["url"]
    assert url.startswith(rapp.cfg.server.preview_url)
    path = url[len(rapp.cfg.preview_origin):]
    # Not served on the app origin.
    assert (await rclient.get(path)).status == 404
    preview = Client(rapp, origin=rapp.cfg.preview_origin)
    r = await preview.get(path)
    assert r.status == 200, r.text
    assert "An Example Book" in r.text and "@doe2020" not in r.text  # citeproc resolved the citation
    csp = r.header("content-security-policy")
    assert "default-src 'none'" in csp and "script" not in csp
    assert r.header("referrer-policy") == "no-referrer"
    css = re.search(r'href="(_layout/[^"]+)"', r.text).group(1)
    assert (await preview.get(path.rsplit("/", 1)[0] + "/" + css)).status == 200
    dark = await preview.get(path + "?paper=dark")
    assert "_dark.css" in dark.text
    # A forged token gets nothing.
    forged = re.sub(r"/_preview/[0-9a-f]{32}/", "/_preview/" + "0" * 32 + "/", path)
    assert (await preview.get(forged)).status == 404
    await ws.close()


async def test_exports(rapp, rclient):
    pid = await new_project(rclient)
    snap = await open_editor(rclient, pid)
    targets = ["docx", "md-resolved", "md-source", "typ-source"] + (["pdf-typst"] if HAS_TYPST else [])
    for target in targets:
        r = await rclient.post(f"/api/projects/{pid}/export", json={"doc": snap["doc"]["id"], "target": target})
        assert r.status == 202
        data = await wait_job(rclient, r.json()["job"])
        assert data["status"] == "ok", (target, data)
        out = await rclient.get(data["url"])
        assert out.status == 200 and out.body
        if target == "pdf-typst":
            assert out.body.startswith(b"%PDF") and out.header("content-type") == "application/pdf"
        elif target == "md-resolved":
            assert "Doe" in out.text and "@doe2020" not in out.text
            assert out.header("content-disposition").startswith("attachment")
        else:
            assert out.header("content-disposition").startswith("attachment")


async def test_identical_projects_get_their_own_renders(rapp, rclient):
    # Two projects with the same files must not share a cache entry: outputs are
    # served only to members of the project that owns them.
    urls = []
    for name in ("One", "Two"):
        pid = await new_project(rclient, name)
        snap = await open_editor(rclient, pid)
        r = await rclient.post(f"/api/projects/{pid}/export", json={"doc": snap["doc"]["id"], "target": "docx"})
        data = await wait_job(rclient, r.json()["job"])
        assert data["status"] == "ok", data
        assert (await rclient.get(data["url"])).status == 200
        urls.append(data["url"])
    assert urls[0] != urls[1]


async def test_term_paper_pdf_with_layout_options(rapp, rclient):
    if not HAS_TYPST:
        pytest.skip("needs typst")
    r = await rclient.post("/api/projects", json={"name": "Seminar", "layout": "term-paper"})
    pid = r.json()["id"]
    r = await rclient.request("PATCH", f"/api/projects/{pid}",
                              json={"layoutOptions": {"institution": "Example University", "linestretch": 2}})
    assert r.status == 200, r.text
    bad = await rclient.request("PATCH", f"/api/projects/{pid}", json={"layoutOptions": {"linestretch": 9}})
    assert bad.status == 400
    r = await rclient.post(f"/api/projects/{pid}/export", json={"target": "pdf-typst"})
    data = await wait_job(rclient, r.json()["job"])
    assert data["status"] == "ok", data


async def test_render_error_reports_diagnostics(rapp, rclient):
    if not HAS_TYPST:
        pytest.skip("needs typst")
    pid = await new_project(rclient)
    await rclient.post(f"/api/projects/{pid}/documents", json={"path": "broken.md", "text": "```{=typst}\n#nonsense(\n```\n"})
    r = await rclient.post(f"/api/projects/{pid}/export", json={"target": "pdf-typst"})
    snap = await open_editor(rclient, pid, "broken.md")
    r = await rclient.post(f"/api/projects/{pid}/export", json={"doc": snap["doc"]["id"], "target": "pdf-typst"})
    data = await wait_job(rclient, r.json()["job"])
    assert data["status"] == "error" and data["diagnostics"]


async def test_citation_library_from_json_and_bib(rapp, rclient):
    pid = await new_project(rclient)
    bib = "@article{smith2019,\n  author = {Smith, Ann and Lee, Bo},\n  title = {On Things},\n  journal = {J. Stuff},\n  year = {2019}\n}\n"
    await rclient.request("PUT", f"/api/projects/{pid}/assets/more.bib", body=bib.encode())
    r = await rclient.get(f"/api/projects/{pid}/citations")
    items = {i["id"]: i for i in r.json()["items"]}
    assert items["doe2020"]["label"] == "Doe 2020"
    assert items["smith2019"]["label"] == "Smith and Lee 2019"
    again = await rclient.get(f"/api/projects/{pid}/citations", headers={"if-none-match": r.header("etag")})
    assert again.status == 304


async def test_zotero_bibliography_resolves(rapp, rclient):
    import json

    pid = await new_project(rclient)
    uid = (await rapp.db.fetchone("SELECT id FROM users WHERE username = 'jane'"))["id"]
    item = {"id": "zot2021", "type": "book", "author": [{"family": "Zotfamily", "given": "Z."}],
            "issued": {"date-parts": [[2021]]}, "title": "From Zotero"}

    def seed(c):
        sid = c.execute("INSERT INTO citation_sources(owner_id, name, kind, library_type, library_id, config_enc)"
                        " VALUES (?, 'lib', 'zotero_web', 'user', '1', 'x')", (uid,)).lastrowid
        c.execute("INSERT INTO citation_items(source_id, item_key, csl_json) VALUES (?, 'K1', ?)",
                  (sid, json.dumps(item)))

    await rapp.db.write(seed)
    text = "---\ntitle: Z\nbibliography:\n  - zotero:lib\n  - references.json\n---\n\nSee [@zot2021] and [@doe2020].\n"
    await rclient.post(f"/api/projects/{pid}/documents", json={"path": "z.md", "text": text})
    snap = await open_editor(rclient, pid, "z.md")
    r = await rclient.post(f"/api/projects/{pid}/export", json={"doc": snap["doc"]["id"], "target": "md-resolved"})
    data = await wait_job(rclient, r.json()["job"])
    assert data["status"] == "ok", data
    out = (await rclient.get(data["url"])).text
    assert "Zotfamily" in out and "From Zotero" in out and "Doe" in out


async def test_citation_styles_and_article_layout(rapp, rclient):
    pid = await new_project(rclient)
    snap = await open_editor(rclient, pid)
    project = dict(await rapp.db.fetchone("SELECT * FROM projects WHERE id = ?", (pid,)))
    path = snap["doc"]["path"]
    chicago = await rapp.renderer.render(project, "md-resolved", doc_path=path)
    apa = await rapp.renderer.render(project, "md-resolved", doc_path=path, options={"citation-style": "apa"})
    ieee = await rapp.renderer.render(project, "md-resolved", doc_path=path, options={"citation-style": "ieee"})
    texts = []
    for res in (chicago, apa, ieee):
        assert res.status == "ok", res.diagnostics
        texts.append((rapp.renderer.renders_dir / res.key / res.output).read_text())
    assert "[Doe 2020, 4]" in texts[0] and "Doe, Jane. 2020." in texts[0]
    assert "[Doe, 2020, p. 4]" in texts[1] and "Doe, J. (2020)." in texts[1]
    assert "[\\[1, p. 4\\]]" in texts[2]
    assert "/cache/" not in texts[0]  # no server paths in exports
    if HAS_TYPST:
        art = await rapp.renderer.render(project, "pdf-typst", doc_path=path, layout_id="article")
        assert art.status == "ok", art.diagnostics
        one = await rapp.renderer.render(project, "pdf-typst", doc_path=path, layout_id="article",
                                         options={"columns": "1", "citation-style": "din-1505-2"})
        assert one.status == "ok", one.diagnostics

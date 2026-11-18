"""POST /api/projects/{id}/source-at: from a click in the PDF preview to the source (§6.4)."""

import json

from docwizard.projects import service as svc
from tests.asgi_client import Client


async def setup(app, client, layout="thesis"):
    client.csrf = (await client.get("/api/me")).json()["csrf"]
    made = (await client.post("/api/projects", json={"name": "T", "layout": layout})).json()
    key = "ab" * 32
    base = app.renderer.renders_dir / key
    base.mkdir(parents=True)
    (base / "meta.json").write_text(json.dumps({"status": "ok", "output": "document.pdf", "target": "pdf-typst",
                                                "project_id": made["id"], "diagnostics": []}))
    return made, key, base


async def test_words_find_the_chapter(app, client):
    made, key, _ = await setup(app, client)
    words = "Each chapter starts on a new page".split()
    r = await client.post(f"/api/projects/{made['id']}/source-at",
                          json={"key": key, "page": 4, "pages": 6, "x": 100, "y": 200, "words": words, "at": 1,
                                "doc": made["mainDocument"]})
    assert r.status == 200, r.text
    got = r.json()
    assert got["path"] == "chapters/02-background.md" and got["line"] == 3 and got["column"] == 5
    doc = await app.db.read(lambda c: svc.get_document_by_path(c, made["id"], got["path"]))
    assert got["doc"] == doc["id"]
    missing = await client.post(f"/api/projects/{made['id']}/source-at",
                                json={"key": key, "page": 1, "words": ["zebra", "quartz", "violin"], "at": 1})
    assert missing.status == 404


async def test_synctex_wins_and_points_to_project_files(app, client):
    made, key, base = await setup(app, client)
    (base / "synctex.json").write_text(json.dumps({
        "inputs": {"1": "thesis.md", "2": "nowhere.tex"},
        "pages": {"1": [["(", "1", 4, 72, 100, 400, 8, 2], ["g", "1", 5, 90, 100, 0, 0, 0],
                        ["(", "2", 9, 72, 200, 400, 8, 2]]}}))
    url = f"/api/projects/{made['id']}/source-at"
    r = await client.post(url, json={"key": key, "page": 1, "x": 120, "y": 99})
    assert r.json() == {"path": "thesis.md", "line": 5, "column": 0, "doc": made["mainDocument"]}
    # A file that isn't in the project falls back to the words, and without words, nothing.
    assert (await client.post(url, json={"key": key, "page": 1, "x": 120, "y": 199})).status == 404


async def test_only_members_and_only_their_renders(app, client):
    made, key, _ = await setup(app, client)
    other = (await client.post("/api/projects", json={"name": "Other"})).json()
    r = await client.post(f"/api/projects/{other['id']}/source-at", json={"key": key, "page": 1, "words": ["a"]})
    assert r.status == 404
    stranger = Client(app)
    r = await stranger.post(f"/api/projects/{made['id']}/source-at", json={"key": key, "page": 1})
    assert r.status in (401, 403, 404)

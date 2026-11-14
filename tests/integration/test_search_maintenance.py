"""Full-text search (§7.3) and daily maintenance (§7.6)."""

import os
import time

from docwizard.projects import files
from docwizard.search import fts_query, highlight
from tests.integration.test_collab import new_project
from tests.integration.test_sharing import make_user


def test_query_building():
    assert fts_query('Zitat "OR NEAR(') == '"zitat" "or" "near"*'
    assert fts_query("  ") is None
    assert highlight("a <b> \x02hit\x03") == "a &lt;b&gt; <mark>hit</mark>"


async def test_search_finds_text_in_my_projects_only(app, client):
    assert app.search.available
    pid = await new_project(client)
    r = await client.post(f"/api/projects/{pid}/documents", json={"path": "notes.md",
                                                                  "text": "# Notes\nPhotosynthesis in Ärzte-Studien.\n"})
    doc_id = r.json()["id"]
    data = (await client.get("/api/search?q=photosynth")).json()
    assert [x["doc"] for x in data["results"]] == [doc_id]
    assert "<mark>Photosynthesis</mark>" in data["results"][0]["snippet"]
    assert (await client.get("/api/search?q=arzte")).json()["results"]  # diacritics folded
    page = await client.get("/search?q=photosynthesis")
    assert page.status == 200 and "notes.md" in page.text

    # Edits are indexed when the document is written out.
    room = await app.rooms.get(doc_id)

    async def edit():
        await room.apply_text("# Notes\nMitochondria now.\n", "t", 1)
        await room.materialize()

    await room.call(edit)
    assert not (await client.get("/api/search?q=photosynthesis")).json()["results"]
    assert (await client.get("/api/search?q=mitochondria")).json()["results"]

    olga = await make_user(app, "olga")
    assert not (await olga.get("/api/search?q=mitochondria")).json()["results"]

    # Deleted documents drop out; a catch-up rebuilds missing entries.
    await app.db.execute("DELETE FROM doc_search")
    await app.db.execute("DELETE FROM doc_search_state")
    assert await app.search.catch_up() >= 1
    assert (await client.get("/api/search?q=mitochondria")).json()["results"]
    await client.request("DELETE", f"/api/documents/{doc_id}")
    assert not (await client.get("/api/search?q=mitochondria")).json()["results"]


async def test_maintenance_purges_old_trash_and_blobs(app, client):
    pid = await new_project(client)
    r = await client.post(f"/api/projects/{pid}/documents", json={"path": "old.md", "text": "old\n"})
    doc_id = r.json()["id"]
    await client.request("DELETE", f"/api/documents/{doc_id}")
    await app.db.execute("UPDATE documents SET deleted_at = '2000-01-01T00:00:00.000Z' WHERE id = ?", (doc_id,))
    orphan = files.put_blob(app.cfg.data_dir, b"nobody refers to me")
    fresh = files.put_blob(app.cfg.data_dir, b"new orphan")
    old = time.time() - 3 * 86400
    os.utime(files.blob_path(app.cfg.data_dir, orphan), (old, old))
    gone = await new_project(client)
    await client.request("DELETE", f"/api/projects/{gone}")
    await app.db.execute("UPDATE projects SET deleted_at = '2000-01-01T00:00:00.000Z' WHERE id = ?", (gone,))
    report = await app.maintenance.run_once()
    assert report["documents"] >= 1 and report["projects"] == 1 and report["blobs"] >= 1
    assert await app.db.fetchone("SELECT 1 FROM documents WHERE id = ?", (doc_id,)) is None
    assert await app.db.fetchone("SELECT 1 FROM projects WHERE id = ?", (gone,)) is None
    assert not files.blob_path(app.cfg.data_dir, orphan).exists()
    assert files.blob_path(app.cfg.data_dir, fresh).exists()
    assert not app.rooms.project_dir(gone).exists()
    # The live project and its files are untouched.
    assert (await client.get(f"/p/{pid}/paper.md")).status == 200

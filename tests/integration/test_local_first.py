"""Projects kept in the browser, edits sent without the editor, and project archives (§6.5)."""

import io
import zipfile

from tests.asgi_client import Client
from tests.integration.test_collab import new_project, open_editor
from tests.integration.test_sync import doc_of, edit, text_of


async def test_project_snapshots_for_offline_use(client, app):
    pid = await new_project(client)
    r = await client.post(f"/api/projects/{pid}/documents", json={"path": "notes.md", "text": "# Notes\n"})
    assert r.status in (200, 201), r.text
    r = await client.post(f"/api/projects/{pid}/offline", json={})
    assert r.status == 200, r.text
    data = r.json()
    assert data["project"]["name"] == "Paper"
    by_path = {d["snapshot"]["doc"]["path"]: d for d in data["docs"]}
    assert set(by_path) == {"paper.md", "notes.md", "references.json"}
    notes = by_path["notes.md"]
    assert notes["url"] == f"/p/{pid}/notes.md"
    snap = notes["snapshot"]
    assert snap["text"] == "# Notes\n" and snap["clientID"].startswith("c_") and snap["layout"]["id"]
    # Keeping a project offline doesn't make its documents "recent".
    home = await client.get("/")
    assert "notes.md" not in home.text

    # Only what changed comes again.
    known = {d["snapshot"]["doc"]["id"]: d["snapshot"]["version"] for d in data["docs"]}
    await edit(app, pid, "notes.md", lambda t: t + "More.\n")
    again = (await client.post(f"/api/projects/{pid}/offline", json={"known": known})).json()
    assert [d["snapshot"]["doc"]["path"] for d in again["docs"]] == ["notes.md"]
    assert len(again["unchanged"]) == 2

    # The client ID works on the socket, as an editor page's would.
    from tests.integration.test_collab import connect

    ws = await connect(app, client, snap)
    await ws.close()

    # Strangers get nothing.
    stranger = Client(app)
    assert (await stranger.post(f"/api/projects/{pid}/offline", json={})).status in (401, 403, 404)


async def test_merge_over_http_is_idempotent_and_needs_edit_rights(client, app):
    from tests.integration.test_sharing import make_user

    pid = await new_project(client)
    snap = await open_editor(client, pid)
    base = snap["text"]
    doc_id = snap["doc"]["id"]
    # Someone else edits meanwhile, in another paragraph.
    await edit(app, pid, "paper.md", lambda t: t.replace("# Introduction", "# Sources"))
    body = {"mergeId": "m_offline_1", "baseVersion": snap["version"], "base": base,
            "text": base.replace("Write here.", "Written on the plane.")}
    r = await client.post(f"/api/documents/{doc_id}/merge", json=body)
    assert r.status == 200, r.text
    out = r.json()
    assert out["conflicts"] == 0
    assert "Written on the plane." in out["text"] and "# Sources" in out["text"]
    assert out["text"] == await text_of(app, pid, "paper.md")
    # Sending it twice changes nothing.
    again = (await client.post(f"/api/documents/{doc_id}/merge", json=body)).json()
    assert again["text"] == out["text"] and again["version"] == out["version"]

    viewer = await make_user(app, "vera")
    await client.post(f"/api/projects/{pid}/members", json={"username": "vera", "role": "viewer"})
    r = await viewer.post(f"/api/documents/{doc_id}/merge", json={**body, "mergeId": "m_offline_2"})
    assert r.status == 403
    r = await client.post(f"/api/documents/{doc_id}/merge", json={"mergeId": "x", "base": "", "text": ""})
    assert r.status == 400


async def test_project_archive_is_a_plain_folder(client, app):
    pid = await new_project(client)
    await client.request("PUT", f"/api/projects/{pid}/assets/figures/plot.png", body=b"\x89PNG data")
    await edit(app, pid, "paper.md", lambda t: t + "\nUnsaved to disk yet.\n")
    r = await client.get(f"/api/projects/{pid}/archive")
    assert r.status == 200 and r.header("content-type") == "application/zip"
    assert 'filename="Paper.zip"' in r.header("content-disposition")
    with zipfile.ZipFile(io.BytesIO(r.body)) as zf:
        names = sorted(zf.namelist())
        assert names == ["Paper/figures/plot.png", "Paper/paper.md", "Paper/references.json"]
        assert zf.read("Paper/paper.md").decode() == await text_of(app, pid, "paper.md")
        assert zf.read("Paper/figures/plot.png") == b"\x89PNG data"
    assert (await doc_of(app, pid, "paper.md")) is not None
    assert (await Client(app).get(f"/api/projects/{pid}/archive")).status in (401, 403, 404)


async def test_installable_app_manifest(client, app):
    r = await Client(app).get("/manifest.webmanifest")
    assert r.status == 200 and r.header("content-type") == "application/manifest+json"
    manifest = r.json()
    assert manifest["start_url"] == "/" and manifest["display"] == "standalone"
    for icon in manifest["icons"]:
        assert (await client.get(icon["src"])).status == 200, icon["src"]
    page = await client.get("/")
    assert '<link rel="manifest" href="/manifest.webmanifest">' in page.text
    assert 'id="net-state"' in page.text


async def test_project_page_offers_offline_zip_and_sync_kinds(client, app, tmp_path):
    pid = await new_project(client)
    page = (await client.get(f"/p/{pid}")).text
    assert "data-keep-offline" in page and f"/api/projects/{pid}/archive" in page
    assert 'value="folder"' not in page  # no shared folders configured
    app.cfg.sync.folder_roots = [str(tmp_path)]
    page = (await client.get(f"/p/{pid}")).text
    assert 'value="folder"' in page and 'id="sync-folder"' in page

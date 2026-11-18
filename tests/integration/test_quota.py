"""Storage quotas and object limits (§7.7)."""

import re

from docwizard.projects import service as svc
from tests.integration.test_collab import connect, insert_at_end, new_project, open_editor
from tests.integration.test_sharing import make_user


async def usage(app, username="jane"):
    row = await app.db.fetchone("SELECT id FROM users WHERE username = ?", (username,))
    return await app.db.read(lambda c: svc.user_storage_bytes(c, row["id"]))


async def set_quota(app, quota, username="jane"):
    await app.db.execute("UPDATE users SET quota_bytes = ? WHERE username = ?", (quota, username))
    app.quotas.forget()


async def test_storage_counts_assets_documents_versions_and_trash(client, app):
    pid = await new_project(client)
    start = await usage(app)
    assert start > 0  # the starter files
    r = await client.request("PUT", f"/api/projects/{pid}/assets/figure.png", body=b"x" * 10_000)
    assert r.status == 201
    assert await usage(app) == start + 10_000
    # Trashed files still count until they are purged.
    await client.request("DELETE", f"/api/projects/{pid}/assets/figure.png")
    assert await usage(app) == start + 10_000
    # Named versions count; automatic history doesn't.
    doc = (await open_editor(client, pid))["doc"]["id"]
    r = await client.post(f"/api/documents/{doc}/versions", json={"label": "Draft"})
    assert r.status == 201
    text = await app.db.fetchone("SELECT text FROM doc_snapshots WHERE doc_id = ? AND label = 'Draft'", (doc,))
    assert await usage(app) == start + 10_000 + len(text[0].encode())
    # Another person's uploads to Jane's project count for Jane.
    olga = await make_user(app, "olga")
    await client.post(f"/api/projects/{pid}/members", json={"username": "olga", "role": "editor"})
    olga.csrf = (await olga.get("/api/me")).json()["csrf"]
    before = await usage(app)
    assert (await olga.request("PUT", f"/api/projects/{pid}/assets/olga.bin", body=b"o" * 500)).status == 201
    assert await usage(app) == before + 500 and await usage(app, "olga") == 0


async def test_uploads_and_documents_over_quota_get_413(client, app):
    pid = await new_project(client)
    await set_quota(app, await usage(app) + 5_000)
    assert (await client.request("PUT", f"/api/projects/{pid}/assets/a.bin", body=b"a" * 4_000)).status == 201
    r = await client.request("PUT", f"/api/projects/{pid}/assets/b.bin", body=b"b" * 4_000)
    assert r.status == 413 and r.json()["error"]["code"] == "quota_exceeded"
    assert "Storage quota reached" in r.json()["error"]["message"]
    assert await app.db.fetchone("SELECT 1 FROM assets WHERE path = 'b.bin'") is None
    # Replacing a file with a smaller one still works.
    assert (await client.request("PUT", f"/api/projects/{pid}/assets/a.bin", body=b"a" * 100)).status == 201
    r = await client.post(f"/api/projects/{pid}/documents", json={"path": "big.md", "text": "x" * 9_000})
    assert r.status == 413
    r = await client.post(f"/api/projects/{pid}/documents", json={"path": "small.md", "text": "fits"})
    assert r.status == 201
    # A Word or text upload is checked the same way.
    r = await client.request("PUT", f"/api/projects/{pid}/assets/notes.md", body=b"n" * 9_000)
    assert r.status == 413
    # The project page's form shows the message instead of an error page.
    page = await client.get(f"/p/{pid}")
    csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
    await set_quota(app, 1)
    r = await client.post("/projects", form={"_csrf": csrf, "name": "More", "layout": "default", "starter": "1"})
    assert r.status == 303 and "Storage%20quota%20reached" in r.header("location")
    home = await client.get("/")
    assert "Your storage is full" in home.text
    settings = await client.get("/settings")
    assert 'id="storage"' in settings.text and "Your storage is full" in settings.text
    # An admin raises the quota (0: no limit) and writing works again right away.
    await set_quota(app, 0)
    assert (await client.request("PUT", f"/api/projects/{pid}/assets/b.bin", body=b"b" * 4_000)).status == 201
    assert "Your storage is full" not in (await client.get("/")).text


async def test_versions_and_restores_count(client, app):
    pid = await new_project(client)
    snap = await open_editor(client, pid)
    doc = snap["doc"]["id"]
    await set_quota(app, await usage(app) + 10)
    r = await client.post(f"/api/documents/{doc}/versions", json={"label": "Too much"})
    assert r.status == 413


async def test_live_edits_over_quota_are_refused_but_deleting_works(client, app):
    pid = await new_project(client)
    s = await open_editor(client, pid)
    ws = await connect(app, client, s)
    doc = s["doc"]["id"]
    await set_quota(app, await usage(app) + 50)
    await ws.send({"t": "push", "doc": doc, "version": s["version"],
                   "updates": [{"clientID": s["clientID"], "changes": insert_at_end(s["text"], "y" * 200)}]})
    err = await ws.recv_until("error")
    assert err["code"] == "quota_exceeded" and "Storage quota reached" in err["message"]
    room = app.rooms.loaded(doc)
    assert room.version == s["version"]
    # Deleting text is always allowed.
    from docwizard.collab.changeset import ChangeSet, u16len

    n = u16len(s["text"])
    cs = ChangeSet.of([(n - 5, n, "")], n).to_json()
    await ws.send({"t": "push", "doc": doc, "version": s["version"],
                   "updates": [{"clientID": s["clientID"], "changes": cs}]})
    assert (await ws.recv_until("updates"))["from"] == s["version"]
    await ws.close()


async def test_sync_pull_over_quota_stops_with_an_error(client, app, tmp_path):
    root = tmp_path / "shared"
    root.mkdir()
    app.cfg.sync.folder_roots = [str(root)]
    pid = await new_project(client)
    r = await client.request("PUT", f"/api/projects/{pid}/sync", json={"kind": "folder", "folder": "Thesis"})
    assert r.status == 201, r.text
    r = await client.post(f"/api/projects/{pid}/sync/run")
    assert "error" not in r.json()["report"]
    await set_quota(app, await usage(app) + 1_000)
    (root / "Thesis" / "scan.pdf").write_bytes(b"%PDF" + b"0" * 5_000)
    r = await client.post(f"/api/projects/{pid}/sync/run")
    report = r.json()["report"]
    assert "Storage quota reached" in report["error"]
    target = r.json()["target"]
    assert target["status"] == "error" and "Storage quota reached" in target["lastError"]
    assert await app.db.fetchone("SELECT 1 FROM assets WHERE path = 'scan.pdf'") is None


async def test_project_and_file_limits(client, app):
    app.cfg.limits.max_projects_per_user = 2
    app.cfg.limits.max_files_per_project = 3
    pid = await new_project(client, "One")
    await new_project(client, "Two")
    r = await client.post("/api/projects", json={"name": "Three"})
    assert r.status == 413 and r.json()["error"]["code"] == "project_limit"
    # The starter has two files; one more fits.
    assert len((await client.get(f"/api/projects/{pid}/tree")).json()["tree"]) == 2
    assert (await client.request("PUT", f"/api/projects/{pid}/assets/a.png", body=b"a")).status == 201
    r = await client.request("PUT", f"/api/projects/{pid}/assets/b.png", body=b"b")
    assert r.status == 413 and r.json()["error"]["code"] == "file_limit"
    # Replacing an existing file isn't a new file.
    assert (await client.request("PUT", f"/api/projects/{pid}/assets/a.png", body=b"aa")).status == 201
    # Deleting one makes room.
    await client.request("DELETE", f"/api/projects/{pid}/assets/a.png")
    assert (await client.request("PUT", f"/api/projects/{pid}/assets/b.png", body=b"b")).status == 201


async def test_admin_sees_usage_and_sets_unlimited(client, app):
    await new_project(client)
    page = await client.get("/admin/users")
    assert "0: no limit" in page.text
    row = await app.db.fetchone("SELECT id FROM users WHERE username = 'jane'")
    csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
    r = await client.post(f"/admin/users/{row['id']}", form={"_csrf": csrf, "action": "update", "email": "",
                                                             "quota_gb": "0"})
    assert r.status == 303
    assert (await app.db.fetchone("SELECT quota_bytes FROM users WHERE id = ?", (row["id"],)))[0] == 0
    assert (await app.quotas.usage(row["id"]))["limit"] is None

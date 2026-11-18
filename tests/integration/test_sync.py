"""WebDAV sync against an in-memory Nextcloud (§12)."""

import asyncio

import pytest

from tests.fakes.webdav import FakeDav
from tests.integration.test_collab import new_project

FOLDER = "Papers/Thesis"


@pytest.fixture
async def dav(app):
    server = FakeDav().start()
    app.net_resolver = lambda host, port: ["127.0.0.1"]
    await app.set_setting("allow_private_outbound", ["cloud.test"])
    yield server
    server.stop()


async def connect_nextcloud(client, pid, dav):
    r = await client.post(f"/api/projects/{pid}/sync/nextcloud", json={"server": dav.origin, "remotePath": FOLDER})
    assert r.status == 201, r.text
    flow = r.json()["flow"]
    assert r.json()["login"].startswith(dav.origin)
    assert (await client.get(f"/api/projects/{pid}/sync/nextcloud/{flow}")).json() == {"done": False}
    dav.flow_granted = True
    r = await client.get(f"/api/projects/{pid}/sync/nextcloud/{flow}")
    assert r.json()["done"], r.text
    return r.json()["target"]


async def run(client, pid):
    r = await client.post(f"/api/projects/{pid}/sync/run")
    assert r.status == 200, r.text
    report = r.json()["report"]
    assert "error" not in report, report
    return report


async def doc_of(app, pid, path):
    return await app.db.fetchone("SELECT * FROM documents WHERE project_id = ? AND path = ? AND deleted_at IS NULL",
                                 (pid, path))


async def text_of(app, pid, path):
    doc = await doc_of(app, pid, path)
    room = await app.rooms.get(doc["id"])

    async def get():
        return room.text()

    return await room.call(get)


async def edit(app, pid, path, fn):
    doc = await doc_of(app, pid, path)
    room = await app.rooms.get(doc["id"])

    async def change():
        return await room.apply_text(fn(room.text()), "test", 1)

    await room.call(change)


async def test_nextcloud_login_and_first_sync(client, app, dav):
    pid = await new_project(client)
    target = await connect_nextcloud(client, pid, dav)
    assert target["kind"] == "nextcloud" and target["username"] == "jane" and target["remotePath"] == FOLDER
    row = await app.db.fetchone("SELECT secret_enc FROM sync_targets")
    assert "app-password" not in row["secret_enc"]
    report = await run(client, pid)
    assert sorted(report["uploaded"]) == ["paper.md", "references.json"]
    assert dav.read(f"{FOLDER}/paper.md").decode() == await text_of(app, pid, "paper.md")
    # Nothing changed: nothing happens, and unchanged folders aren't listed again.
    before = len(dav.requests)
    assert not any((await run(client, pid)).values())
    assert sum(1 for m, _ in dav.requests[before:] if m == "PROPFIND") == 1
    status = (await client.get(f"/api/projects/{pid}/sync")).json()["target"]
    assert status["status"] == "ok" and status["lastError"] is None


async def test_changes_flow_both_ways(client, app, dav):
    pid = await new_project(client)
    await connect_nextcloud(client, pid, dav)
    await run(client, pid)
    # Remote edit, local unchanged.
    remote = dav.read(f"{FOLDER}/paper.md").decode().replace("Write here.", "Written in Nextcloud.")
    dav.put(f"{FOLDER}/paper.md", remote)
    assert (await run(client, pid))["downloaded"] == ["paper.md"]
    assert "Written in Nextcloud." in await text_of(app, pid, "paper.md")
    # Local edit, remote unchanged.
    await edit(app, pid, "paper.md", lambda t: t + "\nAdded here.\n")
    assert (await run(client, pid))["uploaded"] == ["paper.md"]
    assert dav.read(f"{FOLDER}/paper.md").decode().endswith("Added here.\n")
    # Both sides, different paragraphs: both kept, no conflict.
    dav.put(f"{FOLDER}/paper.md", dav.read(f"{FOLDER}/paper.md").decode().replace("# Introduction", "# Sources"))
    await edit(app, pid, "paper.md", lambda t: t.replace("Added here.", "Added here, then edited."))
    report = await run(client, pid)
    assert report["conflicts"] == []
    text = await text_of(app, pid, "paper.md")
    assert "# Sources" in text and "Added here, then edited." in text
    assert dav.read(f"{FOLDER}/paper.md").decode() == text


async def test_overlapping_edits_become_conflicts(client, app, dav):
    pid = await new_project(client)
    await connect_nextcloud(client, pid, dav)
    await run(client, pid)
    dav.put(f"{FOLDER}/paper.md", dav.read(f"{FOLDER}/paper.md").decode().replace("Write here.", "Remote words."))
    await edit(app, pid, "paper.md", lambda t: t.replace("Write here.", "Local words."))
    report = await run(client, pid)
    assert report["conflicts"] == ["paper.md"]
    text = await text_of(app, pid, "paper.md")
    assert "Local words." in text and "Remote words." not in text
    doc = await doc_of(app, pid, "paper.md")
    conflict = await app.db.fetchone("SELECT * FROM conflicts WHERE doc_id = ?", (doc["id"],))
    assert "Remote words" in conflict["theirs_text"] and conflict["source"] == "sync:nextcloud"
    version = await app.db.fetchone("SELECT label FROM doc_snapshots WHERE doc_id = ? AND label LIKE 'Remote%'",
                                    (doc["id"],))
    assert version["label"].startswith("Remote version, Nextcloud")
    # The remote text is also a conflict copy, on both sides after the next run.
    copies = await app.db.fetchall("SELECT path FROM documents WHERE path LIKE 'paper (conflict %).md'")
    assert len(copies) == 1
    await run(client, pid)
    assert "Remote words." in dav.read(f"{FOLDER}/{copies[0]['path']}").decode()
    assert dav.read(f"{FOLDER}/paper.md").decode() == text


async def test_new_deleted_and_renamed_files(client, app, dav):
    pid = await new_project(client)
    await connect_nextcloud(client, pid, dav)
    await run(client, pid)
    paper = await doc_of(app, pid, "paper.md")
    # New remote files: a document and an image.
    dav.put(f"{FOLDER}/notes/ideas.md", "# Ideas\n")
    dav.put(f"{FOLDER}/fig.png", b"\x89PNG fake")
    report = await run(client, pid)
    assert sorted(report["downloaded"]) == ["fig.png", "notes/ideas.md"]
    assert await text_of(app, pid, "notes/ideas.md") == "# Ideas\n"
    assert (await app.db.fetchone("SELECT size FROM assets WHERE path = 'fig.png'"))["size"] == 9
    # Renamed in Nextcloud (same fileid): the document keeps its id.
    dav.rename(f"{FOLDER}/paper.md", f"{FOLDER}/thesis.md")
    report = await run(client, pid)
    assert report["renamed"] == ["paper.md -> thesis.md"]
    assert (await doc_of(app, pid, "thesis.md"))["id"] == paper["id"]
    # Renamed in DocWizard: sent as MOVE.
    r = await client.request("PATCH", f"/api/documents/{paper['id']}", json={"path": "chapters/one.md"})
    assert r.status == 200
    report = await run(client, pid)
    assert report["renamed"] == ["thesis.md -> chapters/one.md"]
    assert f"{FOLDER}/chapters/one.md" in dav.files and f"{FOLDER}/thesis.md" not in dav.files
    assert ("MOVE", f"/remote.php/dav/files/jane/{FOLDER}/thesis.md") in dav.requests
    # Deleted remotely: moved to the trash here.
    dav.remove(f"{FOLDER}/notes/ideas.md")
    assert (await run(client, pid))["deleted_local"] == ["notes/ideas.md"]
    assert await doc_of(app, pid, "notes/ideas.md") is None
    # Deleted here: deleted remotely.
    r = await client.request("DELETE", f"/api/projects/{pid}/assets/fig.png")
    assert r.status == 200
    assert (await run(client, pid))["deleted_remote"] == ["fig.png"]
    assert f"{FOLDER}/fig.png" not in dav.files


async def test_binary_conflict_keeps_both(client, app, dav):
    pid = await new_project(client)
    await connect_nextcloud(client, pid, dav)
    await client.request("PUT", f"/api/projects/{pid}/assets/fig.png", body=b"ours v1")
    await run(client, pid)
    dav.put(f"{FOLDER}/fig.png", b"theirs v2")
    await client.request("PUT", f"/api/projects/{pid}/assets/fig.png", body=b"ours v2")
    report = await run(client, pid)
    assert report["conflicts"] == ["fig.png"]
    assert dav.read(f"{FOLDER}/fig.png") == b"ours v2"
    copy = await app.db.fetchone("SELECT path FROM assets WHERE path LIKE 'fig (conflict %).png'")
    assert copy is not None


async def test_failures_back_off_and_private_hosts_are_refused(client, app, dav):
    pid = await new_project(client)
    await connect_nextcloud(client, pid, dav)
    dav.password = "revoked"
    r = await client.post(f"/api/projects/{pid}/sync/run")
    assert "error" in r.json()["report"]
    target = await app.db.fetchone("SELECT * FROM sync_targets")
    assert target["status"] == "error" and target["failures"] == 1
    assert target["next_poll_at"] > target["last_sync_at"] if target["last_sync_at"] else True
    # Without the admin's exception, a host on a private address is refused.
    await app.set_setting("allow_private_outbound", [])
    r = await client.request("PUT", f"/api/projects/{pid}/sync",
                             json={"kind": "webdav", "url": dav.root, "username": "jane", "password": "x"})
    assert r.status == 400 and "private address" in r.text


async def test_generic_webdav_and_permissions(client, app, dav):
    from tests.integration.test_sharing import make_user

    pid = await new_project(client)
    r = await client.request("PUT", f"/api/projects/{pid}/sync",
                             json={"kind": "webdav", "url": dav.root, "remotePath": "plain",
                                   "username": "jane", "password": "wrong"})
    assert r.status == 400 and "credentials" in r.text
    r = await client.request("PUT", f"/api/projects/{pid}/sync",
                             json={"kind": "webdav", "url": dav.root, "remotePath": "plain",
                                   "username": "jane", "password": "app-password"})
    assert r.status == 201, r.text
    await run(client, pid)
    assert "plain/paper.md" in dav.files
    ed = await make_user(app, "eddie")
    await client.post(f"/api/projects/{pid}/members", json={"username": "eddie", "role": "editor"})
    assert (await ed.get(f"/api/projects/{pid}/sync")).status == 403
    # Local edits schedule a sync a few seconds later.
    await edit(app, pid, "paper.md", lambda t: t + "x")
    assert pid in app.sync._nudged
    await asyncio.sleep(0)

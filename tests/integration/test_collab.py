"""Collaboration authority and protocol tests (§8, §20)."""

import json
import re
import sqlite3


from tests.asgi_client import Client, WebSocket


async def new_project(client, name="Paper"):
    r = await client.post("/api/projects", json={"name": name})
    assert r.status == 201, r.text
    return r.json()["id"]


async def open_editor(client, pid, path="paper.md"):
    r = await client.get(f"/p/{pid}/{path}")
    assert r.status == 200, r.text
    m = re.search(r'<script type="application/json" id="snapshot">(.*?)</script>', r.text, re.S)
    return json.loads(m.group(1))


async def connect(app, client, snap):
    ws = WebSocket(app, client)
    assert await ws.connect()
    await ws.send({"t": "open", "doc": snap["doc"]["id"], "version": snap["version"], "clientID": snap["clientID"]})
    opened = await ws.recv()
    assert opened["t"] == "opened", opened
    return ws


def insert_at_end(snap_text, text):
    from docwizard.collab.changeset import ChangeSet, u16len

    n = u16len(snap_text)
    return ChangeSet.of([(n, n, text)], n).to_json()


async def test_snapshot_is_escaped(client, app):
    pid = await new_project(client)
    snap = await open_editor(client, pid)
    assert snap["text"].startswith("---")
    r = await client.get(f"/p/{pid}/paper.md")
    body = r.text.split('id="snapshot">', 1)[1].split("</script>", 1)[0]
    assert "<" not in body


async def test_push_broadcast_and_reject(client, app):
    pid = await new_project(client)
    s1 = await open_editor(client, pid)
    s2 = await open_editor(client, pid)
    a, b = await connect(app, client, s1), await connect(app, client, s2)
    doc = s1["doc"]["id"]
    v = s1["version"]
    await a.send({"t": "push", "doc": doc, "version": v,
                  "updates": [{"clientID": s1["clientID"], "changes": insert_at_end(s1["text"], "A")}]})
    ua, ub = await a.recv_until("updates"), await b.recv_until("updates")
    assert ua == ub and ua["from"] == v
    # b pushes against the old version and is rejected.
    await b.send({"t": "push", "doc": doc, "version": v,
                  "updates": [{"clientID": s2["clientID"], "changes": insert_at_end(s2["text"], "B")}]})
    rej = await b.recv_until("rejected")
    assert rej["version"] == v + 1
    # After rebasing, it succeeds.
    await b.send({"t": "push", "doc": doc, "version": v + 1,
                  "updates": [{"clientID": s2["clientID"], "changes": insert_at_end(s1["text"] + "A", "B")}]})
    assert (await a.recv_until("updates"))["from"] == v + 1
    room = app.rooms.loaded(doc)
    assert room.text().endswith("AB")
    await a.close()
    await b.close()


async def test_foreign_client_id_and_viewer(client, app):
    from docwizard.auth import passwords, users

    pid = await new_project(client)
    s1 = await open_editor(client, pid)
    pw = passwords.hash_password("viewer password 1")
    await app.db.write(lambda c: users.create(c, "vic", pw))
    await client.post(f"/api/projects/{pid}/members", json={"username": "vic", "role": "viewer"})
    viewer = Client(app)
    await viewer.login("vic", "viewer password 1")
    sv = await open_editor(viewer, pid)
    # Using someone else's client ID is refused.
    ws = WebSocket(app, viewer)
    assert await ws.connect()
    await ws.send({"t": "open", "doc": s1["doc"]["id"], "version": 0, "clientID": s1["clientID"]})
    err = await ws.recv()
    assert err["t"] == "error" and err["code"] == "bad_client_id"
    # A viewer can open but not push.
    await ws.send({"t": "open", "doc": sv["doc"]["id"], "version": sv["version"], "clientID": sv["clientID"]})
    assert (await ws.recv())["role"] == "viewer"
    await ws.send({"t": "push", "doc": sv["doc"]["id"], "version": sv["version"],
                   "updates": [{"clientID": sv["clientID"], "changes": insert_at_end(sv["text"], "X")}]})
    err = await ws.recv()
    assert err["code"] == "forbidden"
    await ws.close()


async def test_bad_origin_refused(client, app):
    ws = WebSocket(app, client, origin="https://evil.example")
    assert not await ws.connect()


async def test_invalid_and_failed_commits_leave_no_trace(client, app):
    pid = await new_project(client)
    s = await open_editor(client, pid)
    ws = await connect(app, client, s)
    doc = s["doc"]["id"]
    room = app.rooms.loaded(doc)
    before = room.text()
    # Wrong length.
    await ws.send({"t": "push", "doc": doc, "version": s["version"],
                   "updates": [{"clientID": s["clientID"], "changes": [1, [0, "x"]]}]})
    assert (await ws.recv())["code"] == "invalid_update"
    # Commit failure: text and version are reverted.
    def boom(n):
        raise sqlite3.OperationalError("disk I/O error")

    app.db.before_commit = boom
    await ws.send({"t": "push", "doc": doc, "version": s["version"],
                   "updates": [{"clientID": s["clientID"], "changes": insert_at_end(s["text"], "lost")}]})
    assert (await ws.recv())["code"] == "not_saved"
    app.db.before_commit = None
    assert room.text() == before and room.version == s["version"]
    # The retry succeeds.
    await ws.send({"t": "push", "doc": doc, "version": s["version"],
                   "updates": [{"clientID": s["clientID"], "changes": insert_at_end(s["text"], "kept")}]})
    assert (await ws.recv_until("updates"))["from"] == s["version"]
    state = await app.db.read(lambda c: __import__("docwizard.projects.service", fromlist=["x"]).load_state(c, doc))
    assert state.text.endswith("kept") and state.version == s["version"] + 1
    await ws.close()


async def test_reconnect_catches_up_and_reset(client, app):
    pid = await new_project(client)
    s = await open_editor(client, pid)
    ws = await connect(app, client, s)
    doc = s["doc"]["id"]
    await ws.send({"t": "push", "doc": doc, "version": s["version"],
                   "updates": [{"clientID": s["clientID"], "changes": insert_at_end(s["text"], "1")}]})
    await ws.recv_until("updates")
    await ws.close()
    ws2 = WebSocket(app, client)
    await ws2.connect()
    await ws2.send({"t": "open", "doc": doc, "version": s["version"], "clientID": s["clientID"]})
    assert (await ws2.recv())["t"] == "opened"
    up = await ws2.recv()
    assert up["t"] == "updates" and up["from"] == s["version"]
    await ws2.send({"t": "open", "doc": doc, "version": 999, "clientID": s["clientID"]})
    await ws2.recv()
    reset = await ws2.recv()
    assert reset["t"] == "reset" and reset["text"].endswith("1")
    await ws2.close()


async def test_materialize_and_outside_edit(client, app):
    pid = await new_project(client)
    s = await open_editor(client, pid)
    ws = await connect(app, client, s)
    doc = s["doc"]["id"]
    room = app.rooms.loaded(doc)
    path = app.rooms.project_dir(pid) / "paper.md"
    assert path.read_text() == s["text"]
    # Someone edits the file outside DocWizard.
    outside = s["text"].replace("Write here.", "Write here, please.")
    path.write_text(outside)
    assert await room.call(room.check_disk)
    up = await ws.recv_until("updates")
    assert up["updates"][0]["clientID"] == "system:disk"
    assert room.text() == outside
    # Both sides change the same sentence: ours stays, a conflict is recorded.
    await ws.send({"t": "push", "doc": doc, "version": room.version, "updates": [{"clientID": s["clientID"],
                   "changes": __import__("docwizard.collab.changeset", fromlist=["x"]).from_diff(
                       outside, outside.replace("please", "now")).to_json()}]})
    await ws.recv_until("updates")
    path.write_text(outside.replace("please", "later"))
    await room.call(room.check_disk)
    assert "now" in room.text() and "later" not in room.text()
    r = await client.get(f"/api/documents/{doc}/conflicts")
    conflicts = r.json()["conflicts"]
    assert len(conflicts) == 1 and "later" in conflicts[0]["theirs_text"]
    versions = (await client.get(f"/api/documents/{doc}/versions")).json()["versions"]
    assert versions and versions[0]["label"].startswith("Outside edit")
    # The file now holds the merged text again.
    assert path.read_text() == room.text()
    await ws.close()


async def test_crash_recovery_rewrites_file(cfg, tmp_path):
    """Committed edits that never reached the file are written at startup (§11.2)."""
    from docwizard.asgi import create_app
    from docwizard.auth import passwords, users

    app = create_app(cfg)
    await app.startup()
    pw = passwords.hash_password("correct horse battery")
    await app.db.write(lambda c: users.create(c, "jane", pw))
    client = Client(app)
    await client.login("jane", "correct horse battery")
    pid = await new_project(client)
    s = await open_editor(client, pid)
    ws = await connect(app, client, s)
    doc = s["doc"]["id"]
    await ws.send({"t": "push", "doc": doc, "version": s["version"],
                   "updates": [{"clientID": s["clientID"], "changes": insert_at_end(s["text"], "\nCommitted.")}]})
    await ws.recv_until("updates")
    path = app.rooms.project_dir(pid) / "paper.md"
    assert "Committed." not in path.read_text()
    # "Crash": stop without flushing rooms.
    for task in app._tasks:
        task.cancel()
    app.db.close()
    # Meanwhile, someone edits the file on disk in a different sentence.
    path.write_text(path.read_text().replace("# Introduction", "# Einleitung"))

    app2 = create_app(cfg)
    await app2.startup()
    text = path.read_text()
    assert "Committed." in text and "# Einleitung" in text
    await app2.shutdown()


async def test_new_file_on_disk_is_imported(client, app):
    pid = await new_project(client)
    folder = app.rooms.project_dir(pid)
    (folder / "notes.md").write_text("Notes\r\nline two\r\n")
    (folder / "figure.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    tree = (await client.get(f"/api/projects/{pid}/tree")).json()["tree"]
    paths = {e["path"]: e for e in tree}
    assert paths["notes.md"]["type"] == "document" and paths["figure.png"]["type"] == "asset"
    s = await open_editor(client, pid, "notes.md")
    assert s["text"] == "Notes\nline two\n"
    r = await client.get(f"/api/projects/{pid}/assets/figure.png")
    assert r.status == 200 and r.header("content-type") == "image/png"
    assert r.header("content-security-policy") == "sandbox; default-src 'none'"

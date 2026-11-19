"""Project activity (§8.12): what is recorded, how edits are grouped, who sees it, and presence."""

from docwizard import activity
from tests.integration.test_collab import connect, insert_at_end, new_project, open_editor
from tests.integration.test_sharing import make_user


async def kinds(client, pid):
    r = await client.get(f"/api/projects/{pid}/activity")
    assert r.status == 200, r.text
    return r.json()["items"]


async def test_edits_are_grouped_per_person_and_written_when_the_room_saves(client, app):
    pid = await new_project(client)
    snap = await open_editor(client, pid)
    doc = snap["doc"]["id"]
    ws = await connect(app, client, snap)
    text, version = snap["text"], snap["version"]
    for word in (" One.", " Two.", " Three."):
        await ws.send({"t": "push", "doc": doc, "version": version,
                       "updates": [{"clientID": snap["clientID"], "changes": insert_at_end(text, word)}]})
        await ws.recv_until("updates")
        text, version = text + word, version + 1
    room = app.rooms.loaded(doc)
    # Typing doesn't write to the timeline; saving the file does, once per person and session.
    assert not [i for i in await kinds(client, pid) if i["kind"] == "edit"]
    await room.call(lambda: room.materialize(force=True))
    await ws.send({"t": "push", "doc": doc, "version": version,
                   "updates": [{"clientID": snap["clientID"], "changes": insert_at_end(text, " Four.")}]})
    await ws.recv_until("updates")
    await room.call(lambda: room.materialize(force=True))
    edits = [i for i in await kinds(client, pid) if i["kind"] == "edit"]
    assert len(edits) == 1 and edits[0]["count"] == 4 and edits[0]["actorName"]
    assert edits[0]["text"].endswith("edited paper.md (4 changes)")
    assert edits[0]["currentPath"] == "paper.md"
    await ws.close()


async def test_files_comments_and_outside_edits_are_recorded(client, app):
    pid = await new_project(client)
    snap = await open_editor(client, pid)
    doc = snap["doc"]["id"]
    start = snap["text"].index("Write here.")
    r = await client.post(f"/api/documents/{doc}/comments", json={
        "body": "Is this the right tone?", "from": start, "to": start + 11, "version": snap["version"]})
    assert r.status == 201, r.text
    assert (await client.request("PATCH", f"/api/documents/{doc}", json={"path": "chapter-1.md"})).status == 200
    room = await app.rooms.get(doc)
    path = app.rooms.project_dir(pid) / "chapter-1.md"
    path.write_text(room.text().replace("Write here.", "Write here, please."))
    assert await room.call(room.check_disk)
    items = await kinds(client, pid)
    by_kind = {i["kind"]: i for i in items}
    assert {"comment", "rename", "outside"} <= set(by_kind)
    assert "Is this the right tone?" in by_kind["comment"]["text"]
    assert by_kind["rename"]["detail"]["from"] == "paper.md"
    assert by_kind["outside"]["text"] == "An outside edit changed chapter-1.md"
    # Pages go back in time.
    first = await client.get(f"/api/projects/{pid}/activity?limit=1")
    assert first.json()["more"] is True
    last_id = first.json()["items"][0]["id"]
    rest = (await client.get(f"/api/projects/{pid}/activity?before={last_id}&limit=50")).json()["items"]
    assert last_id not in [i["id"] for i in rest] and len(rest) == len(items) - 1
    # The project page shows the latest events.
    page = await client.get(f"/p/{pid}")
    assert "Activity" in page.text and "renamed paper.md to chapter-1.md" in page.text


async def test_only_members_see_activity_and_presence(client, app):
    pid = await new_project(client)
    stranger = await make_user(app, "stranger")
    assert (await stranger.get(f"/api/projects/{pid}/activity")).status == 404
    assert (await stranger.get(f"/api/projects/{pid}/presence")).status == 404
    snap = await open_editor(client, pid)
    ws = await connect(app, client, snap)
    docs = (await client.get(f"/api/projects/{pid}/presence")).json()["docs"]
    assert docs == [{"docId": snap["doc"]["id"], "path": "paper.md",
                     "people": [{"name": docs[0]["people"][0]["name"], "color": docs[0]["people"][0]["color"],
                                 "me": True}]}]
    await ws.close()


def test_descriptions_and_old_rows():
    say = lambda message, **values: message.format(**values)  # noqa: E731
    assert activity.describe({"kind": "sync", "detail": {"source": "sync:git", "sent": 2, "received": 1}}, say) \
        == "Sync with Git: 2 files sent, 1 file received"
    assert activity.describe({"kind": "conflict", "count": 3, "path": "a.md", "detail": {"source": "offline:Ann"}},
                             say) == "3 conflicts in a.md from Offline edits by Ann"
    assert activity.describe({"kind": "replace", "count": 5, "actorName": "Ann",
                              "detail": {"query": "colour", "replacement": "color", "docs": 2}}, say) \
        == "Ann replaced “colour” with “color” in 2 documents (5 times)"
    assert activity._excerpt("word " * 100).endswith("…")

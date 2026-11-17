"""Mentions, replies and their notifications, in the app and by mail (§8.9)."""

import re
import time

from docwizard import notifications
from docwizard.projects import service as svc
from tests.integration.test_collab import new_project, open_editor
from tests.integration.test_sharing import make_user


def test_mentions_are_parsed():
    assert notifications.mentioned("Thanks @cora and @ben.k. Also @Cora, mail me at x@example.org") == ["cora", "ben.k"]
    assert notifications.mentioned("(@dan)") == ["dan"]
    assert notifications.excerpt("a  b\n c") == "a b c"
    assert notifications.excerpt("x" * 300).endswith("…")


async def comment(client, doc, snap, body, **kw):
    start = snap["text"].index("Write here.")
    r = await client.post(f"/api/documents/{doc}/comments",
                          json={"body": body, "from": start, "to": start + 10, "version": snap["version"], **kw})
    assert r.status == 201, r.text
    return r.json()["id"]


async def test_mentions_and_replies_notify_members(client, app):
    pid = await new_project(client)
    snap = await open_editor(client, pid)
    doc = snap["doc"]["id"]
    cora = await make_user(app, "cora")
    await make_user(app, "olaf")  # not a member: mentioning him does nothing
    await client.post(f"/api/projects/{pid}/members", json={"username": "cora", "role": "commenter"})
    assert {"username": "cora", "name": "Cora"} in snap["members"] or \
        {"username": "cora", "name": "Cora"} in (await open_editor(client, pid))["members"]

    thread = await comment(client, doc, snap, "@cora can you check this? @olaf too")
    data = (await cora.get("/api/notifications")).json()
    assert data["unread"] == 1
    item = data["items"][0]
    assert item["kind"] == "mention" and item["actor"] == "Jane Doe" and item["threadId"] == thread
    assert item["url"].endswith(f"#comment-{thread}")
    olaf_id = (await app.db.fetchone("SELECT id FROM users WHERE username = 'olaf'"))["id"]
    assert await app.db.read(lambda c: notifications.unread_count(c, olaf_id)) == 0

    # The thread shows as unread for Cora until she has seen it.
    t = (await cora.get(f"/api/documents/{doc}/comments")).json()["threads"][0]
    assert t["unread"]
    r = await cora.post("/api/notifications/read", json={"doc": doc})
    assert r.json() == {"marked": 1, "unread": 0}
    assert not (await cora.get(f"/api/documents/{doc}/comments")).json()["threads"][0]["unread"]

    # Cora replies: Jane, who started the thread, hears about it; Cora doesn't notify herself.
    assert (await cora.post(f"/api/documents/{doc}/comments", json={"body": "Done.", "threadId": thread})).status == 201
    mine = (await client.get("/api/notifications")).json()
    assert mine["unread"] == 1 and mine["items"][0]["kind"] == "reply" and mine["items"][0]["actor"] == "Cora"
    assert (await cora.get("/api/notifications")).json()["unread"] == 0

    # The home page lists it; the notifications page marks everything read.
    home = await client.get("/")
    assert "For you" in home.text and "Done." in home.text
    page = await client.get("/notifications")
    assert page.status == 200 and "Cora" in page.text
    csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
    assert (await client.post("/notifications", form={"_csrf": csrf})).status == 303
    assert (await client.get("/api/notifications")).json()["unread"] == 0


async def test_unread_notifications_are_mailed_once(client, app):
    sent = []
    app.mail.transport = sent.append
    pid = await new_project(client)
    snap = await open_editor(client, pid)
    doc = snap["doc"]["id"]
    cora = await make_user(app, "cora")
    await app.db.execute("UPDATE users SET email = 'cora@example.org' WHERE username = 'cora'")
    await make_user(app, "dora")
    await app.db.execute("UPDATE users SET email = 'dora@example.org', preferences = ?"
                         " WHERE username = 'dora'", ('{"notify_email": "off"}',))
    for name in ("cora", "dora"):
        await client.post(f"/api/projects/{pid}/members", json={"username": name, "role": "commenter"})
    await comment(client, doc, snap, "@cora @dora please read the intro")
    await comment(client, doc, snap, "@cora and the conclusion")

    # Too fresh: people in the document see them there first.
    assert await app.notifier.send_due() == 0
    assert await app.notifier.send_due(now=time.time() + 600) == 1
    assert len(sent) == 1 and sent[0]["To"] == "cora@example.org"
    assert sent[0]["Subject"] == "2 new comments for you in DocWizard"
    body = sent[0].get_content()
    assert "Jane Doe mentioned you in paper.md" in body and "the conclusion" in body and "#comment-" in body
    # Nothing is mailed twice, and Dora, who turned mail off, gets none.
    assert await app.notifier.send_due(now=time.time() + 600) == 0
    assert len(sent) == 1
    del cora


async def test_notifications_follow_access(client, app):
    pid = await new_project(client)
    snap = await open_editor(client, pid)
    doc = snap["doc"]["id"]
    cora = await make_user(app, "cora")
    await client.post(f"/api/projects/{pid}/members", json={"username": "cora", "role": "viewer"})
    # Viewers can't comment, but they can be mentioned and see it.
    await comment(client, doc, snap, "FYI @cora")
    assert (await cora.get("/api/notifications")).json()["unread"] == 1
    # Once removed from the project, the notification no longer shows.
    cora_id = (await app.db.fetchone("SELECT id FROM users WHERE username = 'cora'"))["id"]
    await app.db.write(lambda c: svc.remove_member(c, pid, cora_id))
    assert (await cora.get("/api/notifications")).json() == {"unread": 0, "items": []}


async def test_suggestions_are_accepted_or_rejected(client, app):
    """A commenter suggests new text; an editor accepts one and rejects another (§8.10)."""
    from tests.integration.test_collab import connect

    pid = await new_project(client)
    snap = await open_editor(client, pid)
    doc = snap["doc"]["id"]
    cora = await make_user(app, "cora")
    await client.post(f"/api/projects/{pid}/members", json={"username": "cora", "role": "commenter"})
    start = snap["text"].index("Write here.")
    r = await cora.post(f"/api/documents/{doc}/comments", json={
        "body": "", "suggestion": "Start writing here.", "from": start, "to": start + 11, "version": snap["version"]})
    assert r.status == 201, r.text
    first = r.json()["id"]
    # Same text, or a suggestion on a reply, is refused.
    same = await cora.post(f"/api/documents/{doc}/comments", json={
        "suggestion": "Write here.", "from": start, "to": start + 11, "version": snap["version"]})
    assert same.status == 409
    assert (await cora.post(f"/api/documents/{doc}/comments",
                            json={"body": "x", "suggestion": "y", "threadId": first})).status == 400
    t = (await client.get(f"/api/documents/{doc}/comments")).json()
    thread = t["threads"][0]
    assert thread["suggestion"] == "Start writing here." and thread["quote"] == "Write here."
    assert t["canAccept"] and thread["suggestionState"] is None
    # The commenter can't accept her own suggestion.
    assert (await cora.post(f"/api/documents/{doc}/comments/{first}/suggestion",
                            json={"decision": "accept"})).status == 403

    ws = await connect(app, client, snap)
    r = await client.post(f"/api/documents/{doc}/comments/{first}/suggestion", json={"decision": "accept"})
    assert r.status == 200, r.text
    up = await ws.recv_until("updates")
    assert up["updates"][0]["clientID"] == "system:suggestion"
    room = app.rooms.loaded(doc)
    assert "Start writing here." in room.text() and "\nWrite here." not in room.text()
    thread = (await client.get(f"/api/documents/{doc}/comments")).json()["threads"][0]
    assert thread["resolved"] and thread["suggestionState"] == "accepted" and thread["resolvedBy"] == "Jane Doe"
    # Cora hears about the decision.
    item = (await cora.get("/api/notifications")).json()["items"][0]
    assert item["kind"] == "suggestion" and item["excerpt"].startswith("Accepted:")
    assert (await client.post(f"/api/documents/{doc}/comments/{first}/suggestion",
                              json={"decision": "reject"})).status == 409

    # A second suggestion; the passage changes before anyone decides.
    text = room.text()
    pos = text.index("Start writing")
    r = await cora.post(f"/api/documents/{doc}/comments", json={
        "suggestion": "Begin", "from": pos, "to": pos + 5, "version": room.version})
    second = r.json()["id"]
    from docwizard.collab.changeset import ChangeSet, u16len
    from tests.integration.test_collab import insert_at_end  # noqa: F401

    n = u16len(text)
    await ws.send({"t": "push", "doc": doc, "version": room.version, "updates": [
        {"clientID": snap["clientID"], "changes": ChangeSet.of([(pos, pos + 5, "START")], n).to_json()}]})
    await ws.recv_until("updates")
    r = await client.post(f"/api/documents/{doc}/comments/{second}/suggestion", json={"decision": "accept"})
    assert r.status == 409 and r.json()["error"]["code"] == "changed"
    r = await client.post(f"/api/documents/{doc}/comments/{second}/suggestion", json={"decision": "reject"})
    assert r.status == 200
    assert "START writing" in room.text()
    threads = {t["id"]: t for t in (await client.get(f"/api/documents/{doc}/comments")).json()["threads"]}
    assert threads[second]["suggestionState"] == "rejected" and threads[second]["resolved"]
    await ws.close()

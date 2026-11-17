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

"""Share links, guests, comments and pins (§8.8, §13.5, §14)."""

import re

from docwizard.auth import passwords, users
from docwizard.collab.changeset import ChangeSet, u16len
from tests.asgi_client import Client, WebSocket
from tests.integration.test_collab import connect, new_project, open_editor


# The password every test user from make_user() gets (a fixture, not a credential).
USER_PASSWORD = "a long enough password"


async def make_user(app, name):
    pw = passwords.hash_password(USER_PASSWORD)
    await app.db.write(lambda c: users.create(c, name, pw, display_name=name.title()))
    cl = Client(app)
    await cl.login(name, USER_PASSWORD)
    return cl


async def share(client, pid, **kw):
    r = await client.post(f"/api/projects/{pid}/shares", json={"role": "editor", **kw})
    assert r.status == 201, r.text
    return r.json()["url"].split("/s/", 1)[1]


async def as_guest(app, token, name="Gina", password=None):
    guest = Client(app)
    r = await guest.get(f"/s/{token}")
    assert r.status == 200 and "Open as guest" in r.text
    form = {"name": name}
    if password:
        form["password"] = password
    r = await guest.post(f"/s/{token}", form=form)
    assert r.status == 303, r.text
    me = await guest.get("/api/me")
    guest.csrf = me.json()["csrf"]
    return guest, r.header("location")


async def test_guest_joins_edits_and_is_limited(client, app):
    pid = await new_project(client)
    other = await new_project(client, "Private")
    token = await share(client, pid)
    guest, location = await as_guest(app, token)
    assert location == f"/p/{pid}/paper.md"
    me = (await guest.get("/api/me")).json()["user"]
    assert me["guest"] and me["displayName"] == "Gina (guest)"
    # The guest can open and edit the document over the websocket.
    snap = await open_editor(guest, pid)
    assert snap["user"]["guest"] and snap["can"]["edit"] and not snap["can"]["share"]
    ws = await connect(app, guest, snap)
    n = u16len(snap["text"])
    await ws.send({"t": "push", "doc": snap["doc"]["id"], "version": snap["version"],
                   "updates": [{"clientID": snap["clientID"], "changes": ChangeSet.of([(n, n, "guest")], n).to_json()}]})
    assert (await ws.recv_until("updates"))["updates"][0]["clientID"] == snap["clientID"]
    await ws.close()
    row = await app.db.fetchone("SELECT author FROM doc_ops WHERE doc_id = ? ORDER BY version DESC LIMIT 1",
                                (snap["doc"]["id"],))
    assert row["author"].startswith("g")
    # ...but sees nothing else: other projects, the home page, members, sharing.
    assert (await guest.get(f"/api/projects/{other}")).status == 404
    assert (await guest.get(f"/p/{other}")).status == 404
    assert (await guest.get("/")).header("location") == f"/p/{pid}"
    assert (await guest.get(f"/api/projects/{pid}/members")).status == 401
    assert (await guest.get(f"/api/projects/{pid}/shares")).status == 401
    assert (await guest.get("/api/projects")).status == 401


async def test_revoking_a_link_ends_guest_access(client, app):
    pid = await new_project(client)
    token = await share(client, pid, role="viewer")
    guest, _ = await as_guest(app, token)
    assert (await guest.get(f"/api/projects/{pid}")).status == 200
    links = (await client.get(f"/api/projects/{pid}/shares")).json()["links"]
    assert links[0]["role"] == "viewer" and links[0]["guests"] == 1
    r = await client.request("DELETE", f"/api/projects/{pid}/shares/{links[0]['id']}")
    assert r.status == 200
    assert (await guest.get(f"/api/projects/{pid}")).status == 401
    assert (await Client(app).get(f"/s/{token}")).status == 404


async def test_link_password_expiry_and_role_cap(client, app):
    pid = await new_project(client)
    token = await share(client, pid, password="sesame-open")
    guest = Client(app)
    await guest.get(f"/s/{token}")
    r = await guest.post(f"/s/{token}", form={"name": "Eve", "password": "wrong"})
    assert r.status == 400 and "password is wrong" in r.text
    await as_guest(app, token, "Eve", password="sesame-open")
    # The instance cap lowers the role a link grants.
    await app.set_setting("guest_max_role", "commenter")
    token2 = await share(client, pid, role="commenter")
    g2, _ = await as_guest(app, token2)
    snap = await open_editor(g2, pid)
    assert snap["role"] == "commenter" and not snap["can"]["edit"] and snap["can"]["comment"]
    r = await client.post(f"/api/projects/{pid}/shares", json={"role": "editor"})
    assert r.status == 400
    # Expired links are refused.
    await app.db.execute("UPDATE share_links SET expires_at = 1")
    assert (await Client(app).get(f"/s/{token2}")).status == 404
    # Guests can be turned off entirely.
    await app.set_setting("guests", False)
    assert (await g2.get(f"/api/projects/{pid}")).status == 401


async def test_logged_in_user_joins_as_member(client, app):
    pid = await new_project(client)
    token = await share(client, pid, role="commenter")
    ali = await make_user(app, "ali")
    r = await ali.get(f"/s/{token}")
    assert r.status == 303
    members = (await client.get(f"/api/projects/{pid}/members")).json()["members"]
    assert {m["username"]: m["role"] for m in members}["ali"] == "commenter"
    # A link never lowers an existing role.
    token2 = await share(client, pid, role="viewer")
    await ali.get(f"/s/{token2}")
    members = (await client.get(f"/api/projects/{pid}/members")).json()["members"]
    assert {m["username"]: m["role"] for m in members}["ali"] == "commenter"


async def test_editors_can_share_only_when_allowed(client, app):
    pid = await new_project(client)
    ed = await make_user(app, "eddie")
    await client.post(f"/api/projects/{pid}/members", json={"username": "eddie", "role": "editor"})
    assert (await ed.post(f"/api/projects/{pid}/shares", json={"role": "viewer"})).status == 403
    page = await client.get(f"/p/{pid}")
    csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
    r = await client.post(f"/p/{pid}", form={"_csrf": csrf, "action": "sharing", "editors_can_share": "1"})
    assert r.status == 303
    assert (await ed.post(f"/api/projects/{pid}/shares", json={"role": "viewer"})).status == 201
    # Not above their own role... which for editors includes editor links.
    assert (await ed.post(f"/api/projects/{pid}/shares", json={"role": "editor"})).status == 201


async def test_comments_follow_the_text(client, app):
    pid = await new_project(client)
    snap = await open_editor(client, pid)
    doc = snap["doc"]["id"]
    text = snap["text"]
    start = text.index("Write here.")
    r = await client.post(f"/api/documents/{doc}/comments",
                          json={"body": "Too short?", "from": start, "to": start + 10, "version": snap["version"]})
    assert r.status == 201, r.text
    thread = r.json()["id"]
    ws = await connect(app, client, snap)
    # Insert text before the anchored passage: the anchor moves with it.
    await ws.send({"t": "push", "doc": doc, "version": snap["version"], "updates": [
        {"clientID": snap["clientID"], "changes": ChangeSet.of([(0, 0, "ABC")], u16len(text)).to_json()}]})
    await ws.recv_until("updates")
    data = (await client.get(f"/api/documents/{doc}/comments")).json()
    t = data["threads"][0]
    assert (t["from"], t["to"]) == (start + 3, start + 13) and t["quote"] == "Write here"
    # A commenter replies; an editor resolves; the owner sees both.
    cora = await make_user(app, "cora")
    await client.post(f"/api/projects/{pid}/members", json={"username": "cora", "role": "commenter"})
    assert (await cora.post(f"/api/documents/{doc}/comments", json={"body": "Agreed", "threadId": thread})).status == 201
    assert (await cora.request("PATCH", f"/api/documents/{doc}/comments/{thread}", json={"resolved": True})).status == 403
    assert (await client.request("PATCH", f"/api/documents/{doc}/comments/{thread}", json={"resolved": True})).status == 200
    t = (await client.get(f"/api/documents/{doc}/comments")).json()["threads"][0]
    assert t["resolved"] and [c["author"] for c in t["comments"]] == ["Jane Doe", "Cora"]
    # Reopened threads pick up their anchor again.
    await client.request("PATCH", f"/api/documents/{doc}/comments/{thread}", json={"resolved": False})
    t = (await client.get(f"/api/documents/{doc}/comments")).json()["threads"][0]
    assert not t["resolved"] and t["from"] == start + 3
    # Deleting the anchored text orphans the thread.
    room = app.rooms.loaded(doc)
    v = room.version
    cur = room.text()
    await ws.send({"t": "push", "doc": doc, "version": v, "updates": [
        {"clientID": snap["clientID"],
         "changes": ChangeSet.of([(start + 2, start + 14, "")], u16len(cur)).to_json()}]})
    await ws.recv_until("updates")
    note = await ws.recv_until("comments")
    assert note["doc"] == doc
    t = (await client.get(f"/api/documents/{doc}/comments")).json()["threads"][0]
    assert t["orphaned"] and t["from"] is None
    await ws.close()


async def test_anchors_survive_unload_and_reload(client, app):
    pid = await new_project(client)
    snap = await open_editor(client, pid)
    doc = snap["doc"]["id"]
    pos = snap["text"].index("# Introduction")
    r = await client.post(f"/api/documents/{doc}/comments",
                          json={"body": "Check", "from": pos, "to": pos + 12, "version": snap["version"]})
    assert r.status == 201
    ws = await connect(app, client, snap)
    await ws.send({"t": "push", "doc": doc, "version": snap["version"], "updates": [
        {"clientID": snap["clientID"], "changes": ChangeSet.of([(0, 0, "12345")], u16len(snap["text"])).to_json()}]})
    await ws.recv_until("updates")
    await ws.close()
    room = app.rooms.loaded(doc)
    await app.rooms.unload(room)
    t = (await client.get(f"/api/documents/{doc}/comments")).json()["threads"][0]
    assert t["from"] == pos + 5 and t["to"] == pos + 17


async def test_guest_comment_and_presence_name(client, app):
    pid = await new_project(client)
    token = await share(client, pid, role="commenter")
    guest, _ = await as_guest(app, token, "Gus")
    snap = await open_editor(guest, pid)
    r = await guest.post(f"/api/documents/{snap['doc']['id']}/comments",
                         json={"body": "Nice", "from": 0, "to": 3, "version": snap["version"]})
    assert r.status == 201
    t = (await client.get(f"/api/documents/{snap['doc']['id']}/comments")).json()["threads"][0]
    assert t["comments"][0]["author"] == "Gus (guest)"
    owner_snap = await open_editor(client, pid)
    a = await connect(app, client, owner_snap)
    ws = WebSocket(app, guest)
    assert await ws.connect()
    await ws.send({"t": "open", "doc": snap["doc"]["id"], "version": snap["version"], "clientID": snap["clientID"]})
    assert (await ws.recv())["role"] == "commenter"
    joined = await a.recv_until("presence")
    assert joined["peer"]["name"] == "Gus (guest)"
    await ws.close()
    await a.close()


async def test_pins_on_home_page(client, app):
    pid = await new_project(client)
    snap = await open_editor(client, pid)
    page = await client.get("/")
    csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
    r = await client.post(f"/d/{snap['doc']['id']}/pin", form={"_csrf": csrf, "pinned": "1"})
    assert r.status == 303
    page = await client.get("/")
    assert "Unpin" in page.text


async def test_review_links_open_the_review_view(client, app):
    """Review links (§14.1): the text as it reads, with comments and suggestions."""
    pid = await new_project(client)
    token = await share(client, pid, role="commenter", review=True)
    links_ = (await client.get(f"/api/projects/{pid}/shares")).json()["links"]
    assert links_[0]["review"] is True
    guest = Client(app)
    landing = await guest.get(f"/s/{token}")
    assert "without Markdown" in landing.text
    guest, location = await as_guest(app, token, name="Rita")
    assert location == f"/p/{pid}/paper.md?view=review"
    snap = await open_editor(guest, pid)
    assert snap["review"] and snap["can"]["comment"] and not snap["can"]["edit"]
    page = await guest.get(f"/p/{pid}/paper.md")
    assert "review-mode" in page.text and "reviewing paper.md" in page.text
    # The guest can still switch to the source; members choose the view with ?view=.
    assert not (await open_editor(guest, pid, "paper.md?view=source"))["review"]
    assert not (await open_editor(client, pid))["review"]
    assert (await open_editor(client, pid, "paper.md?view=review"))["review"]
    # A member joining through a review link lands in the review view too.
    from tests.integration.test_sharing import make_user  # noqa: F401
    rob = await make_user(app, "rob")
    r = await rob.get(f"/s/{token}")
    assert r.status == 303 and r.header("location") == f"/p/{pid}/paper.md?view=review"
    # Ordinary links don't.
    plain = await share(client, pid, role="commenter")
    _, location = await as_guest(app, plain, name="Pia")
    assert location == f"/p/{pid}/paper.md"

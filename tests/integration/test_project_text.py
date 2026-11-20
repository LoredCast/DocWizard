"""Find and replace across a project (§8.12) and spelling dictionaries (§6.8)."""

from docwizard.collab.changeset import to_u16_offset
from tests.integration.test_collab import connect, new_project, open_editor
from tests.integration.test_sharing import make_user


async def add_doc(client, pid, path, text):
    r = await client.post(f"/api/projects/{pid}/documents", json={"path": path})
    assert r.status == 201, r.text
    doc = r.json()["id"]
    snap = await open_editor(client, pid, path)
    return doc, snap


def edits_for(text, word, insert):
    out, at = [], text.find(word)
    while at >= 0:
        frm = to_u16_offset(text, at)  # offsets count UTF-16 units, like the browser's
        out.append({"from": frm, "to": frm + len(word), "expect": word, "insert": insert})
        at = text.find(word, at + 1)
    return out


async def test_replace_across_documents_keeps_edits_made_since(client, app):
    pid = await new_project(client)
    first = await open_editor(client, pid)
    second_id, _ = await add_doc(client, pid, "chapter-2.md", "")
    room2 = await app.rooms.get(second_id)
    await room2.call(lambda: room2.apply_text("The colour of the colour wheel.\n", "test"))
    room1 = await app.rooms.get(first["doc"]["id"])
    await room1.call(lambda: room1.apply_text("A colour here, 😀 colour there.\n", "test"))

    texts = (await client.get(f"/api/projects/{pid}/texts")).json()["docs"]
    assert {d["path"] for d in texts} >= {"paper.md", "chapter-2.md", "references.json"}
    texts = [d for d in texts if d["kind"] == "markdown"]
    by_path = {d["path"]: d for d in texts}

    # Someone edits the second "colour" of chapter 2 after the search; the first one is still replaced.
    ws = await connect(app, client, await open_editor(client, pid, "chapter-2.md"))
    old = room2.text()
    new = old.replace("colour wheel", "hue wheel")
    await room2.call(lambda: room2.apply_text(new, "other"))

    docs = [{"id": d["id"], "version": d["version"], "edits": edits_for(d["text"], "colour", "color")}
            for d in texts]
    r = await client.post(f"/api/projects/{pid}/replace", json={"query": "colour", "replacement": "color", "docs": docs})
    assert r.status == 200, r.text
    assert r.json()["replaced"] == 3
    assert room1.text() == "A color here, 😀 color there.\n"
    assert room2.text() == "The color of the hue wheel.\n"
    skipped = {d["id"]: d["skipped"] for d in r.json()["documents"]}
    assert skipped[second_id] == 1
    assert (await ws.recv_until("updates"))["updates"][-1]["clientID"] == "other"
    assert (await ws.recv_until("updates"))["updates"][-1]["clientID"] == "system:replace"
    # A named version from before, and one activity entry for the whole replacement.
    versions = (await client.get(f"/api/documents/{by_path['paper.md']['id']}/versions")).json()["versions"]
    assert any((v["label"] or "").startswith("Before replacing “colour”") for v in versions)
    items = (await client.get(f"/api/projects/{pid}/activity")).json()["items"]
    replace = [i for i in items if i["kind"] == "replace"]
    assert len(replace) == 1 and replace[0]["count"] == 3 and "in 2 documents (3 times)" in replace[0]["text"]
    await ws.close()


async def test_replace_needs_edit_rights_and_project_documents(client, app):
    pid = await new_project(client)
    snap = await open_editor(client, pid)
    other = await new_project(client, "Other")
    other_snap = await open_editor(client, other)
    bad = [{"id": other_snap["doc"]["id"], "version": other_snap["version"], "edits": []}]
    r = await client.post(f"/api/projects/{pid}/replace", json={"query": "x", "replacement": "y", "docs": bad})
    assert r.status == 400
    vera = await make_user(app, "vera")
    await client.post(f"/api/projects/{pid}/members", json={"username": "vera", "role": "viewer"})
    assert (await vera.get(f"/api/projects/{pid}/texts")).status == 200
    docs = [{"id": snap["doc"]["id"], "version": snap["version"], "edits": edits_for(snap["text"], "Write", "Read")}]
    assert (await vera.post(f"/api/projects/{pid}/replace",
                            json={"query": "Write", "replacement": "Read", "docs": docs})).status == 403


async def test_project_and_personal_dictionaries(client, app):
    pid = await new_project(client)
    snap = await open_editor(client, pid)
    ws = await connect(app, client, snap)
    r = await client.post(f"/api/projects/{pid}/dictionary", json={"scope": "project", "words": ["Zettelkasten", "bad word", ""]})
    assert r.status == 200 and r.json()["project"] == ["Zettelkasten"]
    assert (await ws.recv_until("dictionary"))["doc"] == snap["doc"]["id"]
    r = await client.post(f"/api/projects/{pid}/dictionary", json={"scope": "mine", "word": "Kvothe"})
    assert r.json() == {"project": ["Zettelkasten"], "mine": ["Kvothe"]}
    # The editor gets both lists; another member sees the project's words but not Jane's own.
    assert (await open_editor(client, pid))["grammar"]["words"] == {"project": ["Zettelkasten"], "mine": ["Kvothe"]}
    cleo = await make_user(app, "cleo")
    await client.post(f"/api/projects/{pid}/members", json={"username": "cleo", "role": "commenter"})
    assert (await cleo.get(f"/api/projects/{pid}/dictionary")).json() == {"project": ["Zettelkasten"], "mine": []}
    # A commenter can't change the project's dictionary, but keeps their own.
    assert (await cleo.post(f"/api/projects/{pid}/dictionary", json={"scope": "project", "word": "x"})).status == 403
    assert (await cleo.post(f"/api/projects/{pid}/dictionary", json={"scope": "mine", "word": "Auri"})).status == 200
    r = await client.request("DELETE", f"/api/projects/{pid}/dictionary", json={"scope": "project", "word": "Zettelkasten"})
    assert r.json()["project"] == []
    # The project page and the settings page manage them without scripts.
    page = await client.get(f"/p/{pid}")
    assert "Project dictionary" in page.text
    await ws.close()


async def test_settings_words(client, app):
    page = await client.get("/settings")
    token = __import__("re").search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
    r = await client.post("/settings/words", form={"_csrf": token, "words": "Hobbit Ent"})
    assert r.status == 303
    page = await client.get("/settings")
    assert "Hobbit" in page.text and "Ent" in page.text


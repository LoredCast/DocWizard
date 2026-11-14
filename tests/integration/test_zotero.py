"""Zotero Web API sources (§10.2, §10.3) against a fake API."""

import json
import re

import pytest

from docwizard.citations import zotero
from tests.fakes.zotero import FakeZotero
from tests.integration.test_collab import new_project

KEY = "k" * 24


def csl(family, year, title, **extra):
    return {"type": "book", "author": [{"family": family, "given": "A."}],
            "issued": {"date-parts": [[year]]}, "title": title, **extra}


@pytest.fixture
async def zot(app):
    server = FakeZotero(api_key=KEY)
    app.net_resolver = lambda host, port: ["127.0.0.1"]
    await app.set_setting("allow_private_outbound", ["zotero.test"])
    app.zotero.base_url = server.base
    yield server
    server.stop()


async def settings_csrf(client):
    page = await client.get("/settings")
    return re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)


async def add_source(client, name="thesis", api_key=KEY):
    form = {"_csrf": await settings_csrf(client), "action": "add", "name": name, "library_type": "user",
            "library_id": "12345", "collection": "", "api_key": api_key}
    return await client.post("/settings/zotero", form=form)


def test_citation_keys():
    assert zotero.pinned_key({"citation-key": "doe2020"}) == "doe2020"
    assert zotero.pinned_key({"note": "Some note\nCitation Key: smith-jones2019\n"}) == "smith-jones2019"
    assert zotero.generated_base(csl("Müller", 2021, "T")) == "muller2021"
    assert zotero.generated_base({"title": "Anonymous work"}) == "anonymousnd"
    assert [zotero._suffix(i) for i in (0, 1, 25, 26)] == ["a", "b", "z", "aa"]


async def test_library_refresh_keys_and_render_input(client, app, zot):
    zot.put("AAAA1111", csl("Doe", 2020, "First"))
    zot.put("BBBB2222", csl("Doe", 2020, "Second"))
    zot.put("CCCC3333", csl("Roe", 2019, "Pinned", note="Citation Key: roe-pinned"))
    r = await add_source(client)
    assert r.status == 303, r.text
    src = await app.db.fetchone("SELECT * FROM citation_sources")
    assert KEY not in src["config_enc"] and src["library_version"] == zot.version
    keys = {r["item_key"]: r["citation_key"] for r in await app.db.fetchall("SELECT * FROM citation_keys")}
    assert keys == {"AAAA1111": "doe2020", "BBBB2222": "doe2020a", "CCCC3333": "roe-pinned"}
    page = await client.get("/settings")
    assert "3 items" in page.text, re.search(r"thesis.{0,300}", page.text, re.S).group(0)

    # Incremental: a new item, a deletion; keys never move.
    zot.put("DDDD4444", csl("Doe", 2020, "Third"))
    zot.delete("AAAA1111")
    assert await app.zotero.refresh(src["id"]) == {"changed": 1, "deleted": 1}
    assert any("since=" in p for p in zot.requests)
    keys = {r["item_key"]: r["citation_key"] for r in await app.db.fetchall("SELECT * FROM citation_keys")}
    assert keys["DDDD4444"] == "doe2020b" and keys["BBBB2222"] == "doe2020a"
    assert await app.zotero.refresh(src["id"]) == {"changed": 0, "deleted": 0}  # 304

    # A document using zotero:thesis gets the library in its render input and autocomplete.
    pid = await new_project(client)
    snap = await client.get(f"/p/{pid}/paper.md")
    assert snap.status == 200
    doc = await app.db.fetchone("SELECT id FROM documents WHERE project_id = ? AND path = 'paper.md'", (pid,))
    room = await app.rooms.get(doc["id"])

    async def cite():
        text = room.text().replace("bibliography: references.json",
                                   "bibliography:\n  - zotero:thesis\n  - references.json")
        return await room.apply_text(text, "test", 1)

    await room.call(cite)
    files, _ = await app.renderer.snapshot(pid)
    items = json.loads(files.texts["_zotero/thesis.json"])
    assert sorted(i["id"] for i in items) == ["doe2020a", "doe2020b", "roe-pinned"]
    data = (await client.get(f"/api/projects/{pid}/citations")).json()
    assert {"doe2020a", "roe-pinned"} <= {i["id"] for i in data["items"]}


async def test_bad_key_is_reported_and_not_saved(client, app, zot):
    r = await add_source(client, api_key="x" * 24)
    assert r.status == 400 and "API key" in r.text
    assert await app.db.fetchone("SELECT 1 FROM citation_sources") is None
    r = await add_source(client, name="bad name!")
    assert r.status == 400


async def test_sources_of_other_users_stay_private(client, app, zot):
    from tests.integration.test_sharing import make_user

    zot.put("AAAA1111", csl("Doe", 2020, "First"))
    other = await make_user(app, "olga")
    assert (await add_source(other)).status == 303
    pid = await new_project(client)
    # Olga isn't a member of Jane's project: her library isn't used there.
    assert await app.zotero.csl_for_project(pid, {"thesis"}) == {}
    await client.post(f"/api/projects/{pid}/members", json={"username": "olga", "role": "viewer"})
    assert await app.zotero.csl_for_project(pid, {"thesis"}) == {}
    await client.post(f"/api/projects/{pid}/members", json={"username": "olga", "role": "editor"})
    assert "doe2020" in (await app.zotero.csl_for_project(pid, {"thesis"}))["thesis"]

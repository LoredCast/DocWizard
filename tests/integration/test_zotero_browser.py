"""Browsing Zotero libraries from the editor (§10.4): collections, search, attachments and
their files from Zotero's storage or WebDAV, and Zotero desktop's local API."""

import io
import re
import zipfile

import pytest

from tests.fakes.webdav import FakeDav
from tests.fakes.zotero import FakeZotero
from tests.integration.test_collab import new_project
from tests.integration.test_zotero import KEY, add_source, csl, settings_csrf

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


@pytest.fixture
async def zot(app):
    server = FakeZotero(api_key=KEY)
    app.net_resolver = lambda host, port: ["127.0.0.1"]
    await app.set_setting("allow_private_outbound", ["zotero.test", "storage.test", "cloud.test"])
    app.zotero.base_url = server.base
    yield server
    server.stop()


def library(z):
    z.put_collection("COLL0001", "Thesis")
    z.put_collection("COLL0002", "Chapter 1", parent="COLL0001")
    z.put_collection("COLL0003", "Other")
    z.put("AAAA1111", csl("Doe", 2020, "Power and Place", page="123-145"), collections=["COLL0002"],
          added="2024-03-01T00:00:00Z")
    z.put("BBBB2222", csl("Müller", 2019, "Über Räume"), collections=["COLL0003"], added="2024-02-01T00:00:00Z")
    z.put("CCCC3333", csl("Roe", 2018, "Cities"), added="2024-01-01T00:00:00Z", citationKey="roe-cities")
    z.put_attachment("PDF00001", "AAAA1111", "doe.pdf", PDF)
    z.put_attachment("LINK0001", "BBBB2222", link_mode="linked_url", content_type="text/html", md5=None)


async def api_client(client):
    client.csrf = (await client.get("/api/me")).json()["csrf"]
    return client


async def test_collections_search_and_details(client, app, zot):
    library(zot)
    assert (await add_source(client)).status == 303
    await api_client(client)
    sources = (await client.get("/api/citations/sources")).json()["sources"]
    assert [(s["name"], s["connection"], s["itemCount"], s["files"]) for s in sources] == [("thesis", "web", 3, True)]
    sid = sources[0]["id"]

    cols = (await client.get(f"/api/citations/sources/{sid}/collections")).json()["collections"]
    assert {(c["key"], c["parent"]) for c in cols} == {("COLL0001", None), ("COLL0002", "COLL0001"),
                                                       ("COLL0003", None)}

    async def items(**q):
        query = "&".join(f"{k}={v}" for k, v in q.items())
        return (await client.get(f"/api/citations/sources/{sid}/items?{query}")).json()

    everything = await items()
    assert [i["id"] for i in everything["items"]] == ["doe2020", "muller2019", "roe-cities"]  # newest first
    assert everything["items"][0]["zotero"] == {"source": "thesis", "sourceId": sid, "item": "AAAA1111", "pdf": True}
    assert everything["items"][0]["page"] == "123-145"
    # Accents and case don't matter, every word has to match, in any order.
    assert [i["id"] for i in (await items(q="uber%20muller"))["items"]] == ["muller2019"]
    assert [i["id"] for i in (await items(q="2018"))["items"]] == ["roe-cities"]
    assert (await items(q="nothing-like-this"))["total"] == 0
    # A collection includes its subcollections.
    assert [i["id"] for i in (await items(collection="COLL0001"))["items"]] == ["doe2020"]
    assert [i["id"] for i in (await items(collection="COLL0003"))["items"]] == ["muller2019"]
    r = await client.get(f"/api/citations/sources/{sid}/items?collection=../x")
    assert r.status == 400

    detail = (await client.get(f"/api/citations/sources/{sid}/items/AAAA1111")).json()
    assert detail["item"]["id"] == "doe2020" and detail["fields"]["page"] == "123-145"
    assert detail["links"]["web"].endswith("/users/12345/items/AAAA1111")
    assert detail["links"]["desktop"] == "zotero://select/library/items/AAAA1111"
    [att] = detail["attachments"]
    assert att["key"] == "PDF00001" and att["file"] and att["desktop"].startswith("zotero://open-pdf/")
    linked = (await client.get(f"/api/citations/sources/{sid}/items/BBBB2222")).json()["attachments"]
    assert linked[0]["linkMode"] == "linked_url" and not linked[0]["file"]

    # Autocomplete across all of your libraries.
    found = (await client.get("/api/citations/search?q=doe")).json()["items"]
    assert [i["id"] for i in found] == ["doe2020"]


async def test_pdf_from_zotero_storage_keeps_the_key_at_home(client, app, zot):
    library(zot)
    assert (await add_source(client)).status == 303
    await api_client(client)
    sid = (await client.get("/api/citations/sources")).json()["sources"][0]["id"]
    r = await client.get(f"/api/citations/sources/{sid}/attachments/PDF00001/file")
    assert r.status == 200 and r.body == PDF
    assert dict(r.headers)["content-type"] == "application/pdf"
    # The signed storage URL gets no API key.
    assert zot.storage_headers and all("Zotero-API-Key" not in h for h in zot.storage_headers)
    # A second open comes from the cache.
    zot.files.clear()
    assert (await client.get(f"/api/citations/sources/{sid}/attachments/PDF00001/file")).body == PDF
    # Linked URLs have no file to fetch.
    await client.get(f"/api/citations/sources/{sid}/items/BBBB2222")
    r = await client.get(f"/api/citations/sources/{sid}/attachments/LINK0001/file")
    assert r.status == 409


async def test_other_users_cannot_browse_your_library(client, app, zot):
    from tests.integration.test_sharing import make_user

    library(zot)
    assert (await add_source(client)).status == 303
    sid = (await app.db.fetchone("SELECT id FROM citation_sources"))["id"]
    olga = await make_user(app, "olga")
    await api_client(olga)
    assert (await olga.get("/api/citations/sources")).json()["sources"] == []
    for path in ("collections", "items", "items/AAAA1111", "attachments/PDF00001/file"):
        assert (await olga.get(f"/api/citations/sources/{sid}/{path}")).status == 404, path
    assert (await olga.get("/api/citations/search?q=doe")).json()["items"] == []


async def test_full_refresh_drops_what_zotero_no_longer_has(client, app, zot):
    library(zot)
    assert (await add_source(client)).status == 303
    src = await app.db.fetchone("SELECT * FROM citation_sources")
    # The migration to collections and attachments fetches everything again.
    await app.db.execute("INSERT INTO citation_items(source_id, item_key, csl_json) VALUES (?, 'GONE0000', '{}')",
                         (src["id"],))
    await app.db.execute("UPDATE citation_sources SET library_version = 0")
    assert (await app.zotero.refresh(src["id"]))["changed"] == 3
    keys = {r["item_key"] for r in await app.db.fetchall("SELECT item_key FROM citation_items")}
    assert keys == {"AAAA1111", "BBBB2222", "CCCC3333"}


async def test_project_citations_carry_their_zotero_item(client, app, zot):
    library(zot)
    assert (await add_source(client)).status == 303
    pid = await new_project(client)
    await client.get(f"/p/{pid}/paper.md")
    doc = await app.db.fetchone("SELECT id FROM documents WHERE project_id = ? AND path = 'paper.md'", (pid,))
    room = await app.rooms.get(doc["id"])

    async def cite():
        text = room.text().replace("bibliography: references.json",
                                   "bibliography:\n  - zotero:thesis\n  - references.json")
        return await room.apply_text(text, "test", 1)

    await room.call(cite)
    data = (await client.get(f"/api/projects/{pid}/citations")).json()
    roe = next(i for i in data["items"] if i["id"] == "roe-cities")
    assert roe["source"] == "zotero:thesis" and roe["zotero"] == {"source": "thesis", "item": "CCCC3333", "pdf": False}
    # The project's own file comes first when both have a key.
    assert next(i for i in data["items"] if i["id"] == "doe2020")["source"] == "references.json"
    assert not any(i["source"].startswith("_zotero/") for i in data["items"])


@pytest.fixture
async def local_zot(app):
    server = FakeZotero(local=True)
    app.net_resolver = lambda host, port: ["127.0.0.1"]
    await app.set_setting("allow_private_outbound", ["zotero.test", "cloud.test"])
    yield server
    server.stop()


async def add_local(client, base):
    form = {"_csrf": await settings_csrf(client), "action": "add", "connection": "local", "name": "desk",
            "library_type": "user", "library_id": "", "collection": "", "api_url": base}
    return await client.post("/settings/zotero", form=form)


async def test_local_zotero_with_webdav_files(client, app, local_zot):
    library(local_zot)
    r = await add_local(client, local_zot.base)
    assert r.status == 303, r.text
    src = await app.db.fetchone("SELECT * FROM citation_sources")
    assert src["api_url"] == local_zot.base and src["library_id"] == "0" and src["server_id"] == "srv1"
    assert not any("Zotero-API-Key" in p for p in local_zot.requests)
    page = await client.get("/settings")
    assert "Zotero on zotero.test" in page.text

    await api_client(client)
    sid = src["id"]
    detail = (await client.get(f"/api/citations/sources/{sid}/items/AAAA1111")).json()
    assert detail["links"]["web"] is None and not detail["attachments"][0]["file"]
    # Zotero desktop only hands out file:// links.
    r = await client.get(f"/api/citations/sources/{sid}/attachments/PDF00001/file")
    assert r.status == 409 and "WebDAV" in r.json()["error"]["message"]

    # With the WebDAV folder Zotero syncs to, the PDF opens from its zip.
    dav = FakeDav().start()
    try:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("doe.pdf", PDF)
        dav.put("zotero/PDF00001.zip", buf.getvalue())
        form = {"_csrf": await settings_csrf(client), "action": "storage", "source_id": str(sid),
                "storage_url": dav.root + "/", "storage_username": dav.user, "storage_password": "wrong"}
        r = await client.post("/settings/zotero", form=form)
        assert r.status == 400 and "credentials" in r.text
        form["storage_password"] = dav.password
        r = await client.post("/settings/zotero", form=form)
        assert r.status == 303, r.text
        row = await app.db.fetchone("SELECT storage_enc FROM citation_sources")
        assert dav.password not in row["storage_enc"]
        client.csrf = (await client.get("/api/me")).json()["csrf"]
        source = (await client.get("/api/citations/sources")).json()["sources"][0]
        assert source["webdav"] and source["files"]
        r = await client.get(f"/api/citations/sources/{sid}/attachments/PDF00001/file")
        assert r.status == 200 and r.body == PDF
    finally:
        dav.stop()


async def test_local_zotero_from_another_installation_starts_over(client, app, local_zot):
    library(local_zot)
    assert (await add_local(client, local_zot.base)).status == 303
    sid = (await app.db.fetchone("SELECT id FROM citation_sources"))["id"]
    # A different Zotero answers at the same address: its versions mean nothing here.
    local_zot.server_id = "srv2"
    local_zot.items.pop("CCCC3333")
    result = await app.zotero.refresh(sid)
    assert "error" not in result
    keys = {r["item_key"] for r in await app.db.fetchall("SELECT item_key FROM citation_items")}
    assert keys == {"AAAA1111", "BBBB2222"}
    src = await app.db.fetchone("SELECT server_id FROM citation_sources")
    assert src["server_id"] == "srv2"


async def test_local_api_switched_off_is_explained(client, app, local_zot):
    r = await add_local(client, f"http://zotero.test:{local_zot.port}/nothing")
    assert r.status == 400
    assert await app.db.fetchone("SELECT 1 FROM citation_sources") is None
    assert re.search(r"doesn&#39;t know this library|local API", r.text), r.text[:2000]

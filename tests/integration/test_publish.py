"""Publishing documents as static sites (§14.4), through the real renderer."""

import asyncio
import base64

import pytest

from tests.asgi_client import Client
from tests.integration.test_collab import new_project
from tests.integration.test_render import HAS_TYPST, pytestmark, rapp, rclient  # noqa: F401
from tests.integration.test_sharing import make_user

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


async def paper(app, client):
    pid = await new_project(client)
    assert (await client.get(f"/p/{pid}/paper.md")).status == 200
    doc = await app.db.fetchone("SELECT id FROM documents WHERE project_id = ? AND path = 'paper.md'", (pid,))
    return pid, doc["id"]


async def test_publish_serve_update_and_unpublish(rapp, rclient):
    app = rapp
    pid, doc_id = await paper(app, rclient)
    r = await rclient.request("PUT", f"/api/projects/{pid}/assets/figures/dot.png", body=PNG)
    assert r.status in (200, 201), r.text
    assert (await rclient.get(f"/api/documents/{doc_id}/publication")).json()["publication"] is None

    bad = await rclient.request("PUT", f"/api/documents/{doc_id}/publication", json={"slug": "No Spaces!"})
    assert bad.status == 400
    r = await rclient.request("PUT", f"/api/documents/{doc_id}/publication", json={"slug": "my-paper", "autoUpdate": True})
    assert r.status == 200, r.text
    pub = r.json()["publication"]
    assert pub["status"] == "ok", pub
    assert pub["url"] == app.cfg.server.publish_url.rstrip("/") + "/my-paper/" and not pub["outdated"]

    site = Client(app, origin=app.cfg.preview_origin)
    page = await site.get("/my-paper/")
    assert page.status == 200 and "An Example Book" in page.text
    csp = page.header("content-security-policy")
    assert "default-src 'none'" in csp and "script" not in csp and "frame-ancestors 'none'" in csp
    assert (await site.get("/my-paper")).status == 308
    assert (await site.get("/my-paper/figures/dot.png")).body == PNG
    assert (await site.get("/my-paper/meta.json")).status == 404
    assert (await site.get("/my-paper/../my-paper/index.html")).status == 404
    # Not on the app origin.
    assert (await rclient.get("/my-paper/")).status == 404

    # Another document can't take the address.
    other = await rclient.post(f"/api/projects/{pid}/documents", json={"path": "notes.md", "text": "# Notes\n"})
    taken = await rclient.request("PUT", f"/api/documents/{other.json()['id']}/publication", json={"slug": "my-paper"})
    assert taken.status == 409

    # Auto update republishes after edits.
    room = await app.rooms.get(doc_id)

    async def edit():
        return await room.apply_text(room.text() + "\nA brand new closing sentence.\n", "test", 1)

    await room.call(edit)
    app.publisher._due[doc_id] -= 60
    task = asyncio.create_task(app.publisher.run_forever())
    try:
        for _ in range(200):
            await asyncio.sleep(0.1)
            if "brand new closing" in (await site.get("/my-paper/")).text:
                break
        else:
            raise AssertionError("not republished")
    finally:
        task.cancel()

    # Switching to unlisted moves the site to an unguessable address.
    r = await rclient.request("PUT", f"/api/documents/{doc_id}/publication", json={"visibility": "unlisted"})
    pub = r.json()["publication"]
    assert pub["slug"].startswith("u-") and len(pub["slug"]) == 26
    assert (await site.get("/my-paper/")).status == 404
    assert (await site.get(f"/{pub['slug']}/")).status == 200
    again = (await rclient.request("PUT", f"/api/documents/{doc_id}/publication", json={"visibility": "unlisted"})).json()
    assert again["publication"]["slug"] == pub["slug"]

    assert (await rclient.request("DELETE", f"/api/documents/{doc_id}/publication")).status == 200
    assert (await site.get(f"/{pub['slug']}/")).status == 404
    assert await app.db.fetchone("SELECT 1 FROM publications") is None


@pytest.mark.skipif(not HAS_TYPST, reason="needs typst")
async def test_publish_with_pdf(rapp, rclient):
    pid, doc_id = await paper(rapp, rclient)
    r = await rclient.request("PUT", f"/api/documents/{doc_id}/publication", json={"slug": "with-pdf", "withPdf": True})
    assert r.json()["publication"]["status"] == "ok", r.text
    site = Client(rapp, origin=rapp.cfg.preview_origin)
    page = await site.get("/with-pdf/")
    assert 'href="document.pdf"' in page.text
    pdf = await site.get("/with-pdf/document.pdf")
    assert pdf.body.startswith(b"%PDF") and pdf.header("content-type") == "application/pdf"


async def test_publish_permissions_and_deletion(rapp, rclient):
    app = rapp
    pid, doc_id = await paper(app, rclient)
    olga = await make_user(app, "olga")
    await rclient.post(f"/api/projects/{pid}/members", json={"username": "olga", "role": "editor"})
    assert (await olga.request("PUT", f"/api/documents/{doc_id}/publication", json={"slug": "olgas"})).status == 403
    page = await rclient.get(f"/p/{pid}")
    csrf = page.text.split('name="_csrf" value="', 1)[1].split('"', 1)[0]
    await rclient.post(f"/p/{pid}", form={"_csrf": csrf, "action": "sharing", "editors_can_publish": "1"})
    r = await olga.request("PUT", f"/api/documents/{doc_id}/publication", json={"slug": "olgas"})
    assert r.status == 200, r.text

    await rclient.request("PUT", f"/api/documents/{doc_id}/publication", json={"slug": "doomed"})
    site = Client(app, origin=app.cfg.preview_origin)
    assert (await site.get("/doomed/")).status == 200
    assert (await rclient.request("DELETE", f"/api/documents/{doc_id}")).status == 200
    assert (await site.get("/doomed/")).status == 404
    assert await app.db.fetchone("SELECT 1 FROM publications") is None

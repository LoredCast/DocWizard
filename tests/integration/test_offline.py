"""Service worker and offline shells (§6.5)."""

import json
import re

from tests.asgi_client import Client
from tests.integration.test_collab import new_project, open_editor


async def test_service_worker_lists_this_release(client, app):
    r = await Client(app).get("/sw.js")
    assert r.status == 200
    assert r.header("cache-control") == "no-cache"
    assert r.header("content-type").startswith("text/javascript")
    manifest = json.loads(re.search(r"const MANIFEST = (\{.*?\});", r.text).group(1))
    assert manifest["release"] == app.cfg.release
    assert manifest["shells"] == ["/_shell/home", "/_shell/editor"]
    assets = set(manifest["assets"])
    prefix = app.static_prefix
    for path in ("vendor/codemirror.js", "app/editor/main.js", "app/offline.js", "css/app.css"):
        assert f"{prefix}/{path}" in assets
    assert not any(a.endswith(("sw.js", "MANIFEST", "LICENSE")) for a in assets)
    # Every precached asset exists, or installing the worker would fail.
    for url in assets:
        assert (await client.get(url)).status == 200, url


async def test_shells_carry_no_user_data(client, app):
    pid = await new_project(client)
    await open_editor(client, pid)
    for url in ("/_shell/home", "/_shell/editor"):
        r = await client.get(url)  # even when logged in
        assert r.status == 200 and r.header("cache-control") == "no-cache"
        assert "Jane" not in r.text and 'name="dw-user"' not in r.text
        assert 'name="csrf-token" content=""' in r.text
    editor = (await Client(app).get("/_shell/editor")).text
    assert '<script type="application/json" id="snapshot">null</script>' in editor
    assert 'id="crumb-doc"' in editor and "open-share" not in editor


async def test_home_mirrors_recent_and_snapshot_has_pins(client, app):
    pid = await new_project(client)
    snap = await open_editor(client, pid)
    assert snap["doc"]["pinned"] is False
    page = await client.get("/")
    data = json.loads(re.search(r'<script type="application/json" id="recent-data">(.*?)</script>', page.text).group(1))
    assert data[0]["url"] == f"/p/{pid}/paper.md" and data[0]["docId"] == snap["doc"]["id"]
    assert 'name="dw-user" content="u1"' in page.text
    csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
    await client.post(f"/d/{snap['doc']['id']}/pin", form={"_csrf": csrf, "pinned": "1"})
    assert (await open_editor(client, pid))["doc"]["pinned"] is True

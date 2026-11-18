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
    assert app.static_prefix == f"/static/{manifest['version']}"
    assert manifest["shells"] == ["/_shell/home", "/_shell/editor"]
    assets = set(manifest["assets"])
    prefix = app.static_prefix
    for path in ("vendor/codemirror.js", "vendor/pdfjs/pdf.min.mjs", "vendor/pdfjs/pdf.worker.min.mjs",
                 "app/editor/main.js", "app/offline.js", "css/app.css"):
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


def test_static_urls_change_with_the_files(app, tmp_path, monkeypatch):
    """Static files are cached as immutable, so changed files need new URLs (stale CSS otherwise)."""
    from docwizard import asgi

    (tmp_path / "css").mkdir()
    (tmp_path / "css" / "app.css").write_text("body { color: red }")
    monkeypatch.setattr(asgi, "WEB_DIR", tmp_path)
    first = asgi.static_version(app.cfg)
    assert first.startswith(app.cfg.release + "-")
    assert asgi.static_version(app.cfg) == first
    (tmp_path / "css" / "app.css").write_text("body { color: blue }")
    assert asgi.static_version(app.cfg) != first
    # Behind the proxy the files live under the release name.
    monkeypatch.setattr(app.cfg.server, "serve_static", False)
    assert asgi.static_version(app.cfg) == app.cfg.release


async def test_responsive_toolbar_and_markdown_bar_render(client):
    pid = await new_project(client)
    home = (await client.get("/")).text
    assert 'class="menu account-menu"' in home and 'class="table-wrap"' in home
    project = (await client.get(f"/p/{pid}")).text
    assert 'class="menu account-menu"' in project and 'class="table-wrap"' in project
    editor = (await client.get(f"/p/{pid}/paper.md")).text
    for marker in ('id="mode-edit"', 'id="mode-preview"', 'id="more-actions"',
                   'id="format-bar"', 'data-cmd="bold"', 'id="preview-error"'):
        assert marker in editor
    assert 'id="format-bar" role="toolbar" aria-label="Formatting" hidden' not in editor
    await client.post(f"/api/projects/{pid}/documents", json={"path": "main.tex", "text": "\\documentclass{article}"})
    tex = (await client.get(f"/p/{pid}/main.tex")).text
    assert 'id="format-bar" role="toolbar" aria-label="Formatting" hidden' in tex

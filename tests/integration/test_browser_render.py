"""The render kit for PDFs made in the browser (§6.4)."""

import base64
import json

from docwizard.api import browser_render
from tests.integration.test_collab import new_project, open_editor


def test_pandoc_options_from_worker_arguments():
    opts = browser_render.pandoc_options(
        ["paper.md", "--from", "markdown", "--standalone", "--metadata-file", "../options.json",
         "--resource-path", ".:/cache/l/_csl", "--lua-filter", "/cache/l/_builtin/lang.lua", "--citeproc",
         "--template", "/cache/l/template.typ", "--to", "typst", "-o", "../out/document.typ"], "/cache/l")
    assert opts == {
        "input-files": ["paper.md"], "from": "markdown", "standalone": True, "metadata-files": ["options.json"],
        "resource-path": [".", "layout/_csl"], "to": "typst", "template": "layout/template.typ",
        "filters": [{"type": "lua", "path": "layout/_builtin/lang.lua"}, {"type": "citeproc"}],
        "output-file": "../out/document.typ",
    }


async def test_render_kit(app, client, monkeypatch):
    monkeypatch.setattr(browser_render, "installed", lambda: False)
    pid = await new_project(client)
    snap = await open_editor(client, pid)
    assert snap["browserRender"] is False
    url = f"/api/projects/{pid}/render-kit?doc={snap['doc']['id']}"
    r = await client.get(url)
    assert r.status == 404 and r.json()["error"]["code"] == "not_installed"

    monkeypatch.setattr(browser_render, "installed", lambda: True)
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    put = await client.request("PUT", f"/api/projects/{pid}/assets/figures/dot.png", body=png,
                               headers={"content-type": "image/png"})
    assert put.status < 300, put.text
    r = await client.get(url)
    assert r.status == 200, r.text
    kit = r.json()
    assert kit["doc"] == "paper.md" and kit["inputs"] == ["paper.md"]
    assert kit["pandoc"]["input-files"] == ["src/paper.md"]
    assert kit["pandoc"]["output-file"] == "src/.docwizard.typ" and kit["pandoc"]["to"] == "typst"
    assert kit["typst"]["main"] == "src/.docwizard.typ"
    files = kit["files"]
    assert "src/paper.md" in files and "src/figures/dot.png" in files and "b64" in files["src/figures/dot.png"]
    assert json.loads(files["options.json"]["text"])
    assert all(m in files for m in ("dwlocale.lua", "dwgraphics.lua", "dwsheet.lua", "locales.json"))
    for f in kit["pandoc"]["filters"]:
        assert f["type"] == "citeproc" or f["path"] in files
    assert all(not k.startswith("layout/_csl/") or k.endswith(".csl") for k in files)
    # Unchanged: 304 with the same ETag.
    etag = r.header("etag")
    again = await client.get(url, headers={"if-none-match": etag})
    assert again.status == 304

    # Another project's document is refused.
    other = await new_project(client, name="Other")
    r = await client.get(f"/api/projects/{other}/render-kit?doc={snap['doc']['id']}")
    assert r.status == 400

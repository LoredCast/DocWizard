"""Diagrams, data tables and charts through the real worker (§9.7), and web sources for data sheets (§11.3a)."""

import shutil

import pytest

from tests.integration.test_collab import new_project, open_editor
from tests.integration.test_render import HAS_TYPST, _pandoc_ok, _path, rapp, rclient, wait_job  # noqa: F401

pytestmark = pytest.mark.skipif(not _pandoc_ok(), reason="needs pandoc")
HAS_DOT = shutil.which("dot", path=_path()) is not None

SHEET = 'Year,Revenue,Cost\n2022,"1,500",=B2*0.6\n2023,1750,1100\nTotal,=SUM(B2:B3),=SUM(C2:C3)\n'
PAPER = """---
title: Charts
---

::: {.data src="data/results.csv" #tbl:results}
Results per year
:::

::: {.chart src="data/results.csv" type=bar x=Year rows=2-3 #fig:rev}
Revenue and cost
:::

```{.mermaid #fig:flow caption="The flow"}
flowchart LR
  A[Plan] --> B{Data?} -->|yes| C(Write)
```

As [@tbl:results], [@fig:rev] and [@fig:flow] show.
"""


async def _export(client, pid, doc_id, target):
    r = await client.post(f"/api/projects/{pid}/export", json={"doc": doc_id, "target": target})
    assert r.status == 202, r.text
    data = await wait_job(client, r.json()["job"])
    assert data["status"] == "ok", (target, data)
    return await client.get(data["url"])


async def test_tables_charts_and_diagrams_render(rapp, rclient):
    if not (HAS_TYPST and HAS_DOT):
        pytest.skip("needs typst and graphviz")
    pid = await new_project(rclient)
    r = await rclient.post(f"/api/projects/{pid}/documents", json={"path": "data/results.csv", "text": SHEET})
    assert r.status == 201, r.text
    r = await rclient.post(f"/api/projects/{pid}/documents", json={"path": "charts.md", "text": PAPER})
    snap = await open_editor(rclient, pid, "charts.md")
    doc = snap["doc"]["id"]
    html = (await _export(rclient, pid, doc, "html")).text
    # The table shows the formulas' results, the chart and the diagram are pictures.
    assert "3250" in html and "=SUM" not in html and "1,500" in html
    assert html.count('src="data:image/svg+xml,') == 2
    assert "Table 1" in html and "Figure 1" in html and "Figure 2" in html
    pdf = await _export(rclient, pid, doc, "pdf-typst")
    assert pdf.body.startswith(b"%PDF")
    docx = await _export(rclient, pid, doc, "docx")
    assert docx.body.startswith(b"PK")
    md = (await _export(rclient, pid, doc, "md-resolved")).text
    assert "flowchart LR" in md and "dw-font-paths" not in md


async def test_diagram_problems_show_in_the_document(rapp, rclient):
    if not HAS_DOT:
        pytest.skip("needs graphviz")
    pid = await new_project(rclient)
    text = "```mermaid\nsequenceDiagram\n  A->>B: hi\n```\n\n```dot\ndigraph { a -> }\n```\n\n" \
           "::: {.chart src=\"../etc/passwd\"}\n:::\n"
    await rclient.post(f"/api/projects/{pid}/documents", json={"path": "bad.md", "text": text})
    snap = await open_editor(rclient, pid, "bad.md")
    html = (await _export(rclient, pid, snap["doc"]["id"], "html")).text
    assert "Only Mermaid flowcharts" in html
    assert "Graphviz diagram" in html
    assert "../etc/passwd isn" in html and "root:" not in html


async def test_sheet_web_source(rapp, rclient, monkeypatch):
    import http.server
    import threading

    from docwizard.api import data

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = "Name,Score\nAnn,3\nBob,5\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        pid = await new_project(rclient)
        r = await rclient.post(f"/api/projects/{pid}/documents", json={"path": "scores.csv", "text": "Name,Score\n"})
        doc = r.json()["id"]
        url = f"http://scores.example:{server.server_port}/scores.csv"
        # Only https:// addresses.
        assert (await rclient.request("PUT", f"/api/documents/{doc}/source", json={"url": url})).status == 400
        monkeypatch.setattr(data, "SCHEMES", ("https", "http"))
        r = await rclient.request("PUT", f"/api/documents/{doc}/source", json={"url": url})
        assert r.status == 200, r.text
        # Private addresses stay blocked unless an admin allows them.
        rapp.net_resolver = lambda host, port: ["127.0.0.1"]
        r = await rclient.post(f"/api/documents/{doc}/source/refresh")
        assert r.status == 502 and "private" in r.text
        monkeypatch.setattr(type(rapp), "policy", lambda self, key, _orig=type(rapp).policy: (
            ["scores.example"] if key == "allow_private_outbound" else _orig(self, key)))
        r = await rclient.post(f"/api/documents/{doc}/source/refresh")
        assert r.status == 200, r.text
        assert r.json()["changed"] and r.json()["rows"] == 3
        snap = await open_editor(rclient, pid, "scores.csv")
        assert snap["text"] == "Name,Score\nAnn,3\nBob,5\n"
        info = (await rclient.get(f"/api/documents/{doc}/source")).json()
        assert info["url"] == url and info["status"] == "ok"
        # Markdown documents have no web source.
        paper = (await open_editor(rclient, pid))["doc"]["id"]
        assert (await rclient.get(f"/api/documents/{paper}/source")).status == 400
    finally:
        server.shutdown()

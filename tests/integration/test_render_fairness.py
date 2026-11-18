"""Render fairness and per-person limits (§9.2). The worker is replaced by a fake: these tests are
about who runs when, not about what pandoc makes."""

import asyncio

import pytest

from docwizard.http.request import HTTPError
from docwizard.render.scheduler import RenderResult
from tests.integration.test_collab import connect, insert_at_end, new_project, open_editor
from tests.integration.test_sharing import make_user


def fake_runs(app, gate=None):
    """Replace the worker run: record targets, optionally wait for *gate*."""
    seen = []

    async def run(key, project_id, target, layout, inputs, options, snap, layout_folder="layouts", lang=None,
                  timeout=None):
        seen.append({"target": target, "timeout": timeout})
        if gate is not None:
            await gate.wait()
        return RenderResult(key, "ok", target, "index.html", [], project_id=project_id)

    app.renderer._run = run
    return seen


async def test_latex_cannot_take_the_fast_lane_and_no_clamp(app):
    r = app.renderer
    app.cfg.render.concurrency = 6
    app.cfg.render.fast_lane = 2
    r.configure()
    release = asyncio.Event()
    running = []

    async def job(target, name):
        async with r.slot(target):
            running.append(name)
            await release.wait()

    tasks = [asyncio.create_task(job("pdf-latex", f"latex{i}")) for i in range(6)]
    await asyncio.sleep(0.05)
    # Six slots (no clamp to 4), two of them kept for quick targets.
    assert len(running) == 4 and r.queued == 2
    tasks += [asyncio.create_task(job("html", "html")), asyncio.create_task(job("pdf-typst", "typst"))]
    await asyncio.sleep(0.05)
    assert running[-2:] == ["html", "typst"]
    # Quick targets also use free heavy slots, but never the other way round.
    release.set()
    await asyncio.gather(*tasks)
    assert len(running) == 8 and r.queued == 0


async def test_one_slot_still_runs_everything(app):
    app.cfg.render.concurrency = 1
    app.cfg.render.fast_lane = 1  # can't take the only slot
    app.renderer.configure()
    async with app.renderer.slot("pdf-latex"):
        pass
    async with app.renderer.slot("html"):
        pass


async def test_previews_wait_their_turn_per_person(app):
    r = app.renderer
    release = asyncio.Event()
    order = []

    async def preview(actor, name):
        async with r.previews.hold(actor):
            order.append(name)
            await release.wait()

    tasks = [asyncio.create_task(preview("u1", "a1")), asyncio.create_task(preview("u1", "a2")),
             asyncio.create_task(preview("u2", "b1"))]
    await asyncio.sleep(0.05)
    assert order == ["a1", "b1"]  # Ada's second preview waits; Ben's doesn't wait for Ada
    release.set()
    await asyncio.gather(*tasks)
    assert order == ["a1", "b1", "a2"] and not r.previews._sems


async def test_exports_per_person_get_429(client, app):
    gate = asyncio.Event()
    fake_runs(app, gate)
    app.cfg.render.user_exports = 1
    pid = await new_project(client)
    r = await client.post(f"/api/projects/{pid}/export", json={"target": "html"})
    assert r.status == 202, r.text
    await asyncio.sleep(0.05)
    r = await client.post(f"/api/projects/{pid}/export", json={"target": "docx"})
    assert r.status == 429 and r.json()["error"]["code"] == "export_limit"
    # Someone else isn't held up.
    olga = await make_user(app, "olga")
    olga.csrf = (await olga.get("/api/me")).json()["csrf"]
    other = (await olga.post("/api/projects", json={"name": "Olga's"})).json()["id"]
    assert (await olga.post(f"/api/projects/{other}/export", json={"target": "html"})).status == 202
    gate.set()
    await asyncio.sleep(0.05)
    assert (await client.post(f"/api/projects/{pid}/export", json={"target": "docx"})).status == 202


async def test_renders_per_minute(client, app):
    fake_runs(app)
    app.cfg.render.per_user_rpm = 2
    pid = await new_project(client)
    for target in ("html", "docx"):
        assert (await client.post(f"/api/projects/{pid}/export", json={"target": target})).status == 202
        await asyncio.sleep(0.02)
    r = await client.post(f"/api/projects/{pid}/export", json={"target": "odt"})
    assert r.status == 429 and r.json()["error"]["code"] == "render_limited"
    assert app.renderer.rejections == 1

    # Previews that miss the cache count too, and say so in the editor.
    s = await open_editor(client, pid)
    ws = await connect(app, client, s)
    app.renderer._rate = None
    app.cfg.render.per_user_rpm = 1
    await ws.send({"t": "render", "doc": s["doc"]["id"], "version": s["version"], "target": "html"})
    assert (await ws.recv_until("rendered"))["status"] == "ok"
    await ws.send({"t": "push", "doc": s["doc"]["id"], "version": s["version"],
                   "updates": [{"clientID": s["clientID"], "changes": insert_at_end(s["text"], " more")}]})
    await ws.recv_until("updates")
    await ws.send({"t": "render", "doc": s["doc"]["id"], "version": s["version"] + 1, "target": "html"})
    msg = await ws.recv_until("rendered")
    assert msg["status"] == "limited" and msg["url"] is None and "rendering a lot" in msg["diagnostics"][0]["message"]
    await ws.close()


async def latex_project(client):
    pid = await new_project(client)
    text = "\\documentclass{article}\n\\begin{document}\nHi\n\\end{document}\n"
    assert (await client.post(f"/api/projects/{pid}/documents", json={"path": "main.tex", "text": text})).status == 201
    return pid


async def test_latex_policy_and_daily_limit(client, app):
    seen = fake_runs(app)
    pid = await latex_project(client)
    project = dict(await app.db.fetchone("SELECT * FROM projects WHERE id = ?", (pid,)))
    # Off for the server: refused, with a pointer to Typst.
    app.cfg.render.allow_latex = False
    with pytest.raises(HTTPError) as exc:
        await app.renderer.render(project, "pdf-latex", doc_path="main.tex")
    assert exc.value.status == 403 and exc.value.code == "latex_disabled" and "Typst" in exc.value.message
    # An admin allows it for this account, with a longer time limit.
    uid = (await app.db.fetchone("SELECT id FROM users WHERE username = 'jane'"))[0]
    page = await client.get("/admin/users")
    assert 'name="allow_latex"' in page.text and "Render time limit in seconds" in page.text
    import re

    csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
    r = await client.post(f"/admin/users/{uid}", form={"_csrf": csrf, "action": "update", "email": "",
                                                        "quota_gb": "", "allow_latex": "1", "render_timeout": "300"})
    assert r.status == 303, r.text
    res = await app.renderer.render(project, "pdf-latex", doc_path="main.tex", actor=f"u{uid}")
    assert res.status == "ok" and seen[-1] == {"target": "pdf-latex", "timeout": 300}
    r = await client.post(f"/admin/users/{uid}", form={"_csrf": csrf, "action": "update", "email": "",
                                                        "quota_gb": "", "allow_latex": "", "render_timeout": "5"})
    assert r.status == 400

    # A daily limit per person.
    app.cfg.render.latex_daily_limit = 1
    r = await client.post(f"/api/projects/{pid}/export", json={"target": "pdf-latex"})
    assert r.status == 429 and r.json()["error"]["code"] == "latex_limit"
    with pytest.raises(HTTPError):
        app.renderer.check_latex_quota(f"u{uid}", charge=False)
    app.renderer.check_latex_quota(f"u{uid + 1}", charge=False)


async def test_latex_waits_for_a_confirmed_owner(client, app):
    fake_runs(app)
    app.mail.transport = [].append
    await app.set_setting("require_email_verification", True)
    olga = await make_user(app, "olga")
    olga.csrf = (await olga.get("/api/me")).json()["csrf"]
    pid = await latex_project(olga)
    project = dict(await app.db.fetchone("SELECT * FROM projects WHERE id = ?", (pid,)))
    with pytest.raises(HTTPError) as exc:
        await app.renderer.render(project, "pdf-latex", doc_path="main.tex")
    assert exc.value.code == "email_unverified"
    await app.db.execute("UPDATE users SET email = 'olga@example.org', email_verified_at = 1 WHERE username = 'olga'")
    assert (await app.renderer.render(project, "pdf-latex", doc_path="main.tex")).status == "ok"

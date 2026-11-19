"""Organizing the home page (§6.2): each member's own tags and archive, and the filter bar."""

import re

from docwizard.projects import service as svc
from tests.integration.test_collab import new_project
from tests.integration.test_sharing import make_user


def rows(page: str) -> dict[str, bool]:
    """Project id → whether its row is shown, from the home page's projects table."""
    table = page.split('id="projects-table"', 1)[1].split("</table>", 1)[0]
    return {m.group(1): " hidden" not in m.group(0)
            for m in re.finditer(r'<tr data-href="/p/([^"]+)"[^>]*>', table)}


def order(page: str) -> list[str]:
    table = page.split('id="projects-table"', 1)[1].split("</table>", 1)[0]
    return re.findall(r'<tr data-href="/p/([^"]+)"', table)


def test_tags_are_normalized():
    assert svc.normalize_tags(" thesis,  2026 ,Thesis, #seminar  notes,, ") == ["thesis", "2026", "seminar notes"]
    assert svc.normalize_tags(["A", "a", "b"]) == ["A", "b"]
    for bad in ("x" * 33, [1], {"a": 1}, ",".join(f"t{i}" for i in range(13))):
        try:
            svc.normalize_tags(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


async def test_tags_and_archive_are_per_member(app, client):
    thesis = await new_project(client, "Thesis")
    notes = await new_project(client, "Seminar notes")
    r = await client.request("PUT", f"/api/projects/{thesis}/home", json={"tags": ["phd", "2026"]})
    assert r.status == 200 and r.json() == {"tags": ["phd", "2026"], "archived": False}
    r = await client.request("PUT", f"/api/projects/{notes}/home", json={"archived": True})
    assert r.json() == {"tags": [], "archived": True}
    bad = await client.request("PUT", f"/api/projects/{notes}/home", json={"archived": "yes"})
    assert bad.status == 400

    listed = {p["id"]: p for p in (await client.get("/api/projects")).json()["projects"]}
    assert listed[thesis]["tags"] == ["phd", "2026"] and not listed[thesis]["archived"]
    assert listed[notes]["archived"] and not listed[notes]["shared"]

    # Another member sees the project without the first one's tags or archive, and as shared.
    ali = await make_user(app, "ali")
    await client.post(f"/api/projects/{notes}/members", json={"username": "ali", "role": "editor"})
    theirs = {p["id"]: p for p in (await ali.get("/api/projects")).json()["projects"]}
    assert theirs[notes]["tags"] == [] and not theirs[notes]["archived"] and theirs[notes]["shared"]
    ali.csrf = (await ali.get("/api/me")).json()["csrf"]
    assert (await ali.request("PUT", f"/api/projects/{thesis}/home", json={"tags": ["x"]})).status in (403, 404)

    home = await client.get("/")
    shown = rows(home.text)
    assert shown == {thesis: True, notes: False}
    assert "#phd" in home.text and "Archived (1)" in home.text

    archived = await client.get("/?archived=1")
    assert rows(archived.text) == {thesis: False, notes: True}
    assert "Archived projects" in archived.text


async def test_filter_and_sort_without_javascript(app, client):
    a = await new_project(client, "Alpha")
    b = await new_project(client, "Beta")
    c = await new_project(client, "Gamma")
    await client.request("PUT", f"/api/projects/{a}/home", json={"tags": ["Thesis"]})
    await client.request("PUT", f"/api/projects/{c}/home", json={"tags": ["thesis", "draft"]})
    await client.request("PATCH", f"/api/projects/{b}", json={"layout": "acm"})

    assert rows((await client.get("/?tag=thesis")).text) == {a: True, b: False, c: True}
    assert rows((await client.get("/?layout=acm")).text) == {a: False, b: True, c: False}
    assert rows((await client.get("/?q=gam")).text) == {a: False, b: False, c: True}
    assert rows((await client.get("/?q=draft")).text) == {a: False, b: False, c: True}
    assert rows((await client.get("/?shared=1")).text) == {a: False, b: False, c: False}
    none = await client.get("/?q=nothing-like-this")
    assert "No projects match." in none.text

    assert order((await client.get("/")).text) == [a, b, c]
    assert order((await client.get("/?sort=-name")).text) == [c, b, a]
    page = (await client.get("/?sort=layout")).text
    assert order(page)[-1] == b or order(page)[0] == b  # ACM sorts apart from the default layout
    # The header links flip the order of the column they sort by.
    assert 'href="/?sort=-name"' in (await client.get("/")).text


async def test_organize_from_forms(app, client):
    pid = await new_project(client, "Paper")
    page = await client.get(f"/p/{pid}")
    assert "Your home page" in page.text
    csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
    r = await client.post(f"/p/{pid}", form={"_csrf": csrf, "action": "organize", "tags": "a, b"})
    assert r.status == 303
    r = await client.post(f"/p/{pid}", form={"_csrf": csrf, "action": "organize", "archived": "1", "next": "/"})
    assert r.status == 303 and r.header("location") == "/"
    state = (await client.get("/api/projects")).json()["projects"][0]
    assert state["tags"] == ["a", "b"] and state["archived"]
    page = await client.get(f"/p/{pid}")
    assert "Restore to home page" in page.text

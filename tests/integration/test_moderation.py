"""Publication moderation, reserved addresses, legal links, readiness and metrics (§14.4, §13.2, §16)."""

import re

import pytest

from docwizard.render.publish import PublishError, check_slug, robots_tag
from tests.asgi_client import Client
from tests.integration.test_collab import new_project
from tests.integration.test_sharing import make_user


async def csrf_of(client, path):
    page = await client.get(path)
    return re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)


def test_reserved_addresses():
    for slug in ("admin", "api", "assets", "static", "login", "logout", "register", "healthz", "readyz", "metrics",
                 "well-known", "robots", "sitemap", "abuse", "security", "Admin"):
        with pytest.raises(PublishError, match="reserved"):
            check_slug(slug)
    assert check_slug("my-thesis") == "my-thesis"
    # A publication keeps an address it had before the address was reserved.
    assert check_slug("docs", current="docs") == "docs"
    with pytest.raises(PublishError):
        check_slug("docs", current="other")


def test_robots():
    assert robots_tag("u-0123abcd", True) == "noindex, nofollow"
    assert robots_tag("my-thesis", True) is None
    assert robots_tag("my-thesis", False) == "noindex, nofollow"


async def publication(app, client, slug, visibility="public", user="jane"):
    pid = await new_project(client, f"Project {slug}")
    doc = await app.db.fetchone("SELECT id FROM documents WHERE project_id = ?", (pid,))
    owner = (await app.db.fetchone("SELECT id FROM users WHERE username = ?", (user,)))[0]
    pub_id = await app.db.write(lambda c: c.execute(
        "INSERT INTO publications(project_id, doc_id, slug, visibility, created_by, updated_at) VALUES (?, ?, ?, ?,"
        " ?, 1)", (pid, doc["id"], slug, visibility, owner)).lastrowid)
    site = app.publisher.root / slug
    site.mkdir(parents=True)
    (site / "index.html").write_text("<html><head></head><body>Hello</body></html>")
    return pub_id, doc["id"], site


async def test_reserved_and_taken_down_addresses_via_api(client, app):
    pid = await new_project(client)
    doc = (await app.db.fetchone("SELECT id FROM documents WHERE project_id = ?", (pid,)))["id"]
    r = await client.request("PUT", f"/api/documents/{doc}/publication", json={"slug": "login"})
    assert r.status == 400 and "reserved" in r.json()["error"]["message"]
    await app.db.execute("INSERT INTO taken_down_slugs(slug, taken_down_at) VALUES ('spam-site', 1)")
    r = await client.request("PUT", f"/api/documents/{doc}/publication", json={"slug": "spam-site"})
    assert r.status == 400 and "no longer available" in r.json()["error"]["message"]


async def test_unlisted_sites_are_not_indexed(client, app):
    await publication(app, client, "u-" + "a" * 24, "unlisted")
    await publication(app, client, "open-paper")
    site = Client(app, origin=app.cfg.preview_origin)
    assert (await site.get("/u-" + "a" * 24 + "/")).header("x-robots-tag") == "noindex, nofollow"
    assert (await site.get("/open-paper/")).header("x-robots-tag") is None
    app.cfg.server.index_public_sites = False
    assert (await site.get("/open-paper/")).header("x-robots-tag") == "noindex, nofollow"


def test_written_sites_carry_the_robots_tag(tmp_path, app):
    render_dir = tmp_path / "render"
    render_dir.mkdir()
    (render_dir / "index.html").write_text("<html><head><title>x</title></head><body>x</body></html>")
    app.publisher._write_site("u-" + "b" * 24, render_dir, "index.html", None, [], indexable=False)
    page = (app.publisher.root / ("u-" + "b" * 24) / "index.html").read_text()
    assert '<meta name="robots" content="noindex, nofollow">' in page
    app.publisher._write_site("public-one", render_dir, "index.html", None, [], indexable=True)
    assert "robots" not in (app.publisher.root / "public-one" / "index.html").read_text()


async def test_admin_unpublishes_and_takes_down(client, app):
    unpub_id, _, unpub_site = await publication(app, client, "harmless")
    bad_id, bad_doc, bad_site = await publication(app, client, "phishing-page")
    page = await client.get("/admin/publications")
    assert page.status == 200 and "phishing-page" in page.text and "harmless" in page.text
    csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
    r = await client.post("/admin/publications", form={"_csrf": csrf, "id": str(unpub_id), "action": "unpublish"})
    assert r.status == 303
    assert not unpub_site.exists()
    assert await app.db.fetchone("SELECT 1 FROM publications WHERE id = ?", (unpub_id,)) is None
    r = await client.post("/admin/publications", form={"_csrf": csrf, "id": str(bad_id), "action": "takedown",
                                                       "reason": "Phishing report #12"})
    assert r.status == 303
    assert not bad_site.exists()
    assert await app.db.fetchone("SELECT 1 FROM publications WHERE id = ?", (bad_id,)) is None
    row = await app.db.fetchone("SELECT reason FROM taken_down_slugs WHERE slug = 'phishing-page'")
    assert row["reason"] == "Phishing report #12"
    actions = [r["action"] for r in await app.db.fetchall("SELECT action FROM audit_log WHERE action LIKE 'publication.%'")]
    assert actions == ["publication.unpublish", "publication.takedown"]
    # Nobody can publish there again, until an admin allows it.
    r = await client.request("PUT", f"/api/documents/{bad_doc}/publication", json={"slug": "phishing-page"})
    assert r.status in (400, 404)
    page = await client.get("/admin/publications")
    assert "Phishing report #12" in page.text
    r = await client.post("/admin/publications", form={"_csrf": csrf, "slug": "phishing-page", "action": "release"})
    assert r.status == 303
    assert await app.db.fetchone("SELECT 1 FROM taken_down_slugs") is None


async def test_suspend_owner_and_unpublish_all(client, app):
    olga = await make_user(app, "olga")
    olga.csrf = (await olga.get("/api/me")).json()["csrf"]
    first, _, first_site = await publication(app, olga, "olga-one", user="olga")
    _, _, second_site = await publication(app, olga, "olga-two", user="olga")
    _, _, mine = await publication(app, client, "janes-own")
    csrf = await csrf_of(client, "/admin/publications")
    r = await client.post("/admin/publications", form={"_csrf": csrf, "id": str(first), "action": "suspend-owner",
                                                       "reason": "spam"})
    assert r.status == 303
    assert not first_site.exists() and not second_site.exists() and mine.exists()
    assert (await app.db.fetchone("SELECT active FROM users WHERE username = 'olga'"))[0] == 0
    assert (await olga.get("/api/me")).status == 401
    entry = await app.db.fetchone("SELECT meta_json FROM audit_log WHERE action = 'user.suspend'")
    assert "olga-one" in entry["meta_json"] and "olga-two" in entry["meta_json"]
    # The same from the users page.
    emil = await make_user(app, "emil")
    emil.csrf = (await emil.get("/api/me")).json()["csrf"]
    _, _, emil_site = await publication(app, emil, "emil-site", user="emil")
    uid = (await app.db.fetchone("SELECT id FROM users WHERE username = 'emil'"))[0]
    csrf = await csrf_of(client, "/admin/users")
    r = await client.post(f"/admin/users/{uid}", form={"_csrf": csrf, "action": "suspend"})
    assert r.status == 303 and not emil_site.exists()


async def test_legal_links_in_footers(app, client):
    plain = await Client(app).get("/login")
    assert "Imprint" not in plain.text
    app.cfg.legal.terms_url = "https://example.org/terms"
    app.cfg.legal.privacy_url = "https://example.org/privacy"
    app.cfg.legal.imprint_url = "https://example.org/imprint"
    app.cfg.legal.abuse_email = "abuse@example.org"
    for path in ("/login", "/register", "/no-such-page"):
        page = await Client(app).get(path)
        for text in ('href="https://example.org/terms"', 'href="https://example.org/privacy"',
                     'href="https://example.org/imprint"', 'href="mailto:abuse@example.org"'):
            assert text in page.text, (path, text)
    assert "you accept the policies" in (await Client(app).get("/register")).text


def test_legal_config_is_checked(tmp_path):
    from docwizard import config

    path = tmp_path / "docwizard.toml"
    path.write_text('[legal]\nterms_url = "javascript:alert(1)"\n')
    with pytest.raises(ValueError, match="terms_url"):
        config.load(path, env={})
    path.write_text('[legal]\nimprint_url = "https://example.org/imprint"\nabuse_email = "abuse@example.org"\n')
    assert config.load(path, env={}).legal.imprint_url == "https://example.org/imprint"


async def test_readyz(app):
    anon = Client(app)
    r = await anon.get("/readyz")
    # No worker runs in these tests.
    assert r.status == 503 and r.json()["checks"]["database"] == "ok" and r.json()["checks"]["disk"] == "ok"
    assert r.json()["checks"]["worker"].startswith("error")

    async def hello():
        return {"pandoc": "3"}

    app.renderer.worker.hello = hello
    r = await anon.get("/readyz")
    assert r.status == 200 and r.json()["ok"] is True
    assert (await anon.get("/healthz")).status == 200


async def test_metrics(client, app):
    from docwizard.render.scheduler import RenderResult

    async def run(key, project_id, target, *args, **kwargs):
        return RenderResult(key, "ok", target, "index.html", [], project_id=project_id)

    app.renderer._run = run
    pid = await new_project(client)
    project = dict(await app.db.fetchone("SELECT * FROM projects WHERE id = ?", (pid,)))
    await app.renderer.render(project, "html")
    app.quotas.rejections = 2
    text = (await client.get("/metrics")).text
    assert 'docwizard_renders_total{target="html",status="ok"} 1' in text
    assert 'docwizard_render_duration_seconds_count{target="html"} 1' in text
    assert 'docwizard_render_duration_seconds_bucket{target="html",le="+Inf"} 1' in text
    assert "# TYPE docwizard_render_duration_seconds histogram" in text
    assert "docwizard_render_queue_depth 0" in text
    assert "docwizard_quota_rejections_total 2" in text
    assert "docwizard_registrations_total 0" in text
    used = int(re.search(r"^docwizard_storage_bytes (\d+)$", text, re.M).group(1))
    assert used > 0
    # No personal data in labels.
    assert "jane" not in text
    assert (await Client(app).get("/metrics")).status in (303, 401, 403, 404)

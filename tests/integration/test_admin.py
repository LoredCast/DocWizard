"""The admin area, invitations and password resets (§13.2, §13.6)."""

import re

from tests.asgi_client import Client
from tests.integration.test_sharing import make_user


async def csrf_of(client, path="/admin/users"):
    page = await client.get(path)
    return re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)


def link_in(text):
    return re.search(r'value="(http[^"]+/reset/[^"]+)"', text).group(1)


async def test_only_admins_get_in(app, client):
    olga = await make_user(app, "olga")
    for path in ("/admin", "/admin/users", "/admin/settings", "/admin/layouts", "/admin/audit", "/metrics"):
        assert (await olga.get(path)).status == 404, path
        r = await client.get(path)
        assert r.status == 200, (path, r.text[:300])
    metrics = await client.get("/metrics")
    assert "docwizard_users 2" in metrics.text and "# TYPE docwizard_projects gauge" in metrics.text
    assert "Admin" in (await client.get("/")).text and ">Admin<" not in (await olga.get("/")).text


async def test_invite_without_mail_and_accept(app, client):
    csrf = await csrf_of(client)
    r = await client.post("/admin/users", form={"_csrf": csrf, "username": "newbie", "display_name": "",
                                                "email": "", "role": "user"})
    assert r.status == 200 and "Invitation link for newbie" in r.text
    url = link_in(r.text)
    path = url.split("://", 1)[1].split("/", 1)[1]
    guest = Client(app)
    page = await guest.get("/" + path)
    assert page.status == 200 and "Welcome to DocWizard" in page.text
    short = await guest.post("/" + path, form={"password": "short", "display_name": "New Person"})
    assert short.status == 400
    r = await guest.post("/" + path, form={"password": "a long enough password", "display_name": "New Person"})
    assert r.status == 303
    me = (await guest.get("/api/me")).json()["user"]
    assert me["username"] == "newbie" and me["displayName"] == "New Person"
    assert (await Client(app).get("/" + path)).status == 404  # single use


async def test_reset_by_mail_and_forgot_password(app, client):
    sent = []
    app.mail.transport = sent.append
    olga = await make_user(app, "olga")
    await app.db.execute("UPDATE users SET email = 'olga@example.org' WHERE username = 'olga'")
    row = await app.db.fetchone("SELECT id FROM users WHERE username = 'olga'")
    csrf = await csrf_of(client)
    r = await client.post(f"/admin/users/{row['id']}", form={"_csrf": csrf, "action": "reset-link"})
    assert r.status == 303 and len(sent) == 1
    assert sent[0]["To"] == "olga@example.org" and "/reset/" in sent[0].get_content()

    # Anyone can ask; the answer is the same whether the account exists.
    anon = Client(app)
    assert "Send link" in (await anon.get("/forgot")).text
    r = await anon.post("/forgot", form={"identifier": "nobody"})
    assert "If an account" in r.text and len(sent) == 1
    r = await anon.post("/forgot", form={"identifier": "OLGA@example.org"})
    assert "If an account" in r.text and len(sent) == 2
    url = re.search(r"(http\S+/reset/\S+)", sent[1].get_content()).group(1)
    path = "/" + url.split("://", 1)[1].split("/", 1)[1]
    # The earlier link stopped working when the new one was issued.
    old = re.search(r"(http\S+/reset/\S+)", sent[0].get_content()).group(1)
    assert (await anon.get("/" + old.split("://", 1)[1].split("/", 1)[1])).status == 404
    r = await anon.post(path, form={"password": "an entirely new password"})
    assert r.status == 303
    # Olga's other sessions ended.
    assert (await olga.get("/api/me")).status == 401
    fresh = Client(app)
    await fresh.login("olga", "an entirely new password")


async def test_user_actions(app, client):
    olga = await make_user(app, "olga")
    row = await app.db.fetchone("SELECT id FROM users WHERE username = 'olga'")
    me = await app.db.fetchone("SELECT id FROM users WHERE username = 'jane'")
    csrf = await csrf_of(client)

    async def act(uid, action, **extra):
        return await client.post(f"/admin/users/{uid}", form={"_csrf": csrf, "action": action, **extra})

    assert (await act(me["id"], "disable")).status == 400  # not yourself
    assert (await act(row["id"], "disable")).status == 303
    assert (await olga.get("/api/me")).status == 401
    assert (await Client(app).post("/login", form={"username": "olga", "password": "a long enough password",
                                                     "next": "/"})).status == 400
    assert (await act(row["id"], "enable")).status == 303
    assert (await act(row["id"], "make-admin")).status == 303
    assert (await app.db.fetchone("SELECT instance_role FROM users WHERE id = ?", (row["id"],)))[0] == "admin"
    assert (await act(row["id"], "update", email="olga@example.org", quota_gb="1.5")).status == 303
    u = await app.db.fetchone("SELECT email, quota_bytes FROM users WHERE id = ?", (row["id"],))
    assert u["email"] == "olga@example.org" and u["quota_bytes"] == 1_500_000_000
    assert (await act(row["id"], "require-totp")).status == 303
    olga2 = Client(app)
    await olga2.post("/login", form={"username": "olga", "password": "a long enough password", "next": "/"})
    assert (await olga2.get("/")).header("location").startswith("/settings?totp=required")
    # A user who owns projects can't be deleted.
    await act(row["id"], "unrequire-totp")
    olga2.csrf = (await olga2.get("/api/me")).json()["csrf"]
    r = await olga2.post("/api/projects", json={"name": "Olga's"})
    assert r.status == 201, r.text
    r = await act(row["id"], "delete")
    assert r.status == 400 and "owns 1 project" in r.text
    log = await client.get("/admin/audit?action=user.")
    assert "user.make-admin" in log.text and "olga" in log.text


async def test_settings_and_layout_approval(app, client):
    csrf = await csrf_of(client, "/admin/settings")
    form = {"_csrf": csrf, "registration": "invite", "guests": "1", "guest_max_role": "commenter",
            "min_password_length": "14", "default_layout": "default", "allow_private_outbound": "nextcloud.lan\n10.0.0.0/8"}
    r = await client.post("/admin/settings", form=form)
    assert r.status == 303, r.text
    assert app.policy("registration") == "invite" and app.policy("guest_max_role") == "commenter"
    assert app.policy("allow_private_outbound") == ["nextcloud.lan", "10.0.0.0/8"]
    assert app.policy("min_password_length") == 14
    bad = await client.post("/admin/settings", form={**form, "allow_private_outbound": "not a host!"})
    assert bad.status == 400
    # Requiring two-step login for admins needs it on your own account first.
    locked = await client.post("/admin/settings", form={**form, "require_totp_for_admins": "1"})
    assert locked.status == 400 and "lock yourself out" in locked.text
    await app.load_settings()
    assert app.policy("registration") == "invite"

    # Layout filter approval by hash.
    layout = next((l for l in app.layouts.list() if l.filter_files()), None)
    page = await client.get("/admin/layouts")
    assert page.status == 200
    if layout is not None:
        digest = layout.filters_hash()
        r = await client.post("/admin/layouts", form={"_csrf": csrf, "action": "approve", "layout": layout.id,
                                                      "hash": digest})
        assert r.status == 303 and digest in app.layouts.approved_filters
        app.layouts.approved_filters.clear()
        await app.load_settings()
        assert digest in app.layouts.approved_filters

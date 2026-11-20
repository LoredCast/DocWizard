"""Registration for open servers: throttling, email confirmation and the anti-bot hook (§13.2)."""

import re
import time

from docwizard.auth.throttle import LoginThrottle
from tests.asgi_client import Client


def link_in(message, route="verify"):
    url = re.search(rf"(http\S+/{route}/\S+)", message.get_content()).group(1)
    return "/" + url.split("://", 1)[1].split("/", 1)[1]


async def register(app, username, email="", ip="198.51.100.1", **extra):
    cl = Client(app)
    r = await cl.post("/register", form={"username": username, "display_name": "", "email": email,
                                         "password": "a long enough password", **extra},
                      headers={"x-forwarded-for": ip})
    return cl, r


async def csrf_of(client, path="/settings"):
    page = await client.get(path)
    return re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)


def test_register_buckets_are_per_address_and_email():
    t = LoginThrottle()
    # Many people at once, each from their own address: nobody is turned away.
    assert all(t.allow_register(f"10.0.{i // 250}.{i % 250}", f"user{i}@example.org") for i in range(200))
    # One address runs out after 10, while another one is unaffected.
    assert all(t.allow_register("192.0.2.1", f"x{i}@example.org") for i in range(10))
    assert not t.allow_register("192.0.2.1", "late@example.org")
    assert t.allow_register("192.0.2.2", "late@example.org")
    # One email address can't be tried from many addresses.
    assert all(t.allow_register(f"203.0.113.{i}", "Same@Example.org ") for i in range(3))
    assert not t.allow_register("203.0.113.99", "same@example.org")
    # Password reset requests have buckets of their own.
    assert all(t.allow_forgot(f"10.1.0.{i}", f"u{i}") for i in range(50))


async def test_parallel_registrations_are_not_globally_throttled(app):
    # Before, every registration shared one "register" bucket: the 11th person was refused.
    for i in range(15):
        _, r = await register(app, f"student{i}", ip=f"198.51.100.{i + 1}")
        assert r.status == 303, (i, r.text[:300])
    # The same address is still limited.
    statuses = [(await register(app, f"burst{i}", ip="198.51.100.200"))[1].status for i in range(11)]
    assert statuses[:10] == [303] * 10 and statuses[10] == 429


async def test_email_confirmation_gates_publishing(app):
    sent = []
    app.mail.transport = sent.append
    await app.set_setting("require_email_verification", True)

    # The address is required, must look like one and must be free.
    _, r = await register(app, "nomail")
    assert r.status == 400 and "Enter your email address" in r.text
    _, r = await register(app, "badmail", email="not-an-address")
    assert r.status == 400 and "doesn" in r.text
    ada, r = await register(app, "ada", email="Ada@Example.org")
    assert r.status == 303 and r.header("location") == "/?verify=sent"
    assert len(sent) == 1 and sent[0]["To"] == "Ada@Example.org"
    _, r = await register(app, "copycat", email="ada@example.org")
    assert r.status == 400 and "Another account uses this email address" in r.text

    # Unconfirmed: the home page says so, and publishing answers with a clear message.
    assert "Confirm your email address to publish" in (await ada.get("/")).text
    ada.csrf = (await ada.get("/api/me")).json()["csrf"]
    r = await ada.post("/api/projects", json={"name": "Essay"})
    assert r.status == 201, r.text
    doc, pid = r.json()["mainDocument"], r.json()["id"]
    r = await ada.request("PUT", f"/api/documents/{doc}/publication", json={"visibility": "unlisted"})
    assert r.status == 403 and r.json()["error"]["code"] == "email_unverified"
    assert "Confirm your email address" in r.json()["error"]["message"]
    r = await ada.request("PUT", f"/api/projects/{pid}/sync", json={"kind": "webdav", "url": "https://dav.example.org"})
    assert r.status == 403 and r.json()["error"]["code"] == "email_unverified"

    # Opening the link only shows a button; confirming works once.
    path = link_in(sent[0])
    page = await Client(app).get(path)
    assert page.status == 200 and "Ada@Example.org" in page.text
    row = await app.db.fetchone("SELECT email_verified_at FROM users WHERE username = 'ada'")
    assert row["email_verified_at"] is None
    r = await ada.post(path, form={"_csrf": await csrf_of(ada)})
    assert r.status == 303 and "email-verified" in r.header("location")
    assert (await Client(app).post(path, form={})).status == 404
    assert "Confirmed" in (await ada.get("/settings")).text
    r = await ada.request("PUT", f"/api/documents/{doc}/publication", json={"visibility": "unlisted"})
    assert r.status != 403, r.text


async def test_verify_links_expire(app):
    sent = []
    app.mail.transport = sent.append
    bea, _ = await register(app, "bea", email="bea@example.org")
    await app.db.execute("UPDATE account_tokens SET expires_at = ? WHERE purpose = 'verify'", (time.time() - 1,))
    path = link_in(sent[0])
    assert (await Client(app).get(path)).status == 404
    assert (await bea.post(path, form={"_csrf": await csrf_of(bea)})).status == 404
    # A new link can be sent from the settings.
    r = await bea.post("/settings/email", form={"_csrf": await csrf_of(bea), "action": "resend"})
    assert r.status == 303 and len(sent) == 2
    r = await bea.post(link_in(sent[1]), form={"_csrf": await csrf_of(bea)})
    assert r.status == 303
    assert (await app.db.fetchone("SELECT email_verified_at FROM users WHERE username = 'bea'"))[0] is not None


async def test_changing_the_email_needs_the_new_address_confirmed(app):
    sent = []
    app.mail.transport = sent.append
    cleo, _ = await register(app, "cleo", email="cleo@example.org")
    await cleo.post(link_in(sent[0]), form={"_csrf": await csrf_of(cleo)})
    csrf = await csrf_of(cleo)
    r = await cleo.post("/settings/email", form={"_csrf": csrf, "email": "new@example.org", "current": "wrong"})
    assert r.status == 400 and "current password is wrong" in r.text
    r = await cleo.post("/settings/email", form={"_csrf": csrf, "email": "new@example.org",
                                                 "current": "a long enough password"})
    assert r.status == 303 and sent[-1]["To"] == "new@example.org"
    # The old address stays until the new one is confirmed.
    row = await app.db.fetchone("SELECT email, email_verified_at FROM users WHERE username = 'cleo'")
    assert row["email"] == "cleo@example.org" and row["email_verified_at"] is not None
    assert "Waiting for confirmation: new@example.org" in (await cleo.get("/settings")).text
    await cleo.post(link_in(sent[-1]), form={"_csrf": csrf})
    row = await app.db.fetchone("SELECT email, email_verified_at FROM users WHERE username = 'cleo'")
    assert row["email"] == "new@example.org" and row["email_verified_at"] is not None


async def test_optional_email_without_mail(app):
    # Without [smtp] and without the setting, registration works as before, with or without an address.
    _, r = await register(app, "dora")
    assert r.status == 303 and r.header("location") == "/"
    _, r = await register(app, "emil", email="emil@example.org")
    assert r.status == 303
    row = await app.db.fetchone("SELECT email, email_verified_at FROM users WHERE username = 'emil'")
    assert row["email"] == "emil@example.org" and row["email_verified_at"] is None


async def test_admin_cannot_require_confirmation_without_mail(app, client):
    page = await client.get("/admin/settings")
    csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
    form = {"_csrf": csrf, "registration": "open", "guest_max_role": "editor", "min_password_length": "12",
            "default_layout": "default", "allow_private_outbound": "", "require_email_verification": "1"}
    r = await client.post("/admin/settings", form=form)
    assert r.status == 400 and "Set up mail first" in r.text
    assert "scratch" not in page.text.lower()
    app.mail.transport = [].append
    r = await client.post("/admin/settings", form=form)
    assert r.status == 303 and app.policy("require_email_verification") is True


async def test_anti_bot_check_on_register_and_forgot(app, cfg):
    cfg.security.captcha_provider = "turnstile"
    cfg.security.captcha_site_key = "site-key"
    cfg.security.captcha_secret = "secret"
    answers = []
    app.captcha_verifier = lambda answer, ip: answers.append((answer, ip)) or answer == "ok"
    page = await Client(app).get("/register")
    assert 'class="cf-turnstile" data-sitekey="site-key"' in page.text
    assert "challenges.cloudflare.com" in page.header("content-security-policy")
    # Other pages don't load the provider.
    assert "cloudflare" not in (await Client(app).get("/login")).header("content-security-policy")
    _, r = await register(app, "robot")
    assert r.status == 400 and "not a robot" in r.text
    _, r = await register(app, "robot", **{"cf-turnstile-response": "nope"})
    assert r.status == 400
    _, r = await register(app, "human", **{"cf-turnstile-response": "ok"})
    assert r.status == 303 and answers[-1] == ("ok", "198.51.100.1")
    app.mail.transport = [].append
    r = await Client(app).post("/forgot", form={"identifier": "human"})
    assert r.status == 400 and "not a robot" in r.text
    r = await Client(app).post("/forgot", form={"identifier": "human", "cf-turnstile-response": "ok"})
    assert r.status == 200 and "If an account" in r.text


async def test_migration_keeps_existing_addresses_confirmed(db):
    from docwizard.auth import users

    uid = db.write_sync(lambda c: users.create(c, "old", None, email="old@example.org", email_verified=True))
    row = db.read_sync(lambda c: c.execute("SELECT email_verified_at FROM users WHERE id = ?", (uid,)).fetchone())
    assert row[0] is not None
    # The rebuilt token table takes the new purpose.
    db.write_sync(lambda c: c.execute("INSERT INTO account_tokens(token_hash, user_id, purpose, expires_at,"
                                      " pending_email) VALUES ('h', ?, 'verify', 0, 'x@example.org')", (uid,)))


def test_config_files_from_older_releases_still_load(tmp_path):
    from docwizard import config

    path = tmp_path / "docwizard.toml"
    path.write_text('[accounts]\nregistration = "open"\nscratch_projects = true\n')
    cfg = config.load(path, env={})
    assert cfg.accounts.registration == "open" and not hasattr(cfg.accounts, "scratch_projects")
    path.write_text('[accounts]\nrequire_email_verification = true\n')
    try:
        config.load(path, env={})
    except ValueError as exc:
        assert "smtp" in str(exc)
    else:
        raise AssertionError("require_email_verification without [smtp] should not load")

import pytest

from docwizard.auth import passwords, permissions
from docwizard.auth.throttle import TokenBucket
from docwizard.http.request import HTTPError


def test_password_roundtrip_and_format():
    h = passwords.hash_password("a long passphrase", log_n=10)
    assert h.startswith("scrypt$10$8$1$")
    assert passwords.verify_password("a long passphrase", h)
    assert not passwords.verify_password("a long passphrasE", h)
    assert not passwords.verify_password("x", "garbage")


def test_password_policy():
    assert passwords.check_policy("short") is not None
    assert passwords.check_policy("password1234") is not None
    assert passwords.check_policy("janedoe12345", username="janedoe12345") is not None
    assert passwords.check_policy("a sensible passphrase") is None


def test_permissions_matrix():
    assert permissions.can("viewer", "read")
    assert not permissions.can("viewer", "edit")
    assert not permissions.can("commenter", "edit")
    assert permissions.can("editor", "edit")
    assert not permissions.can("editor", "manage_members")
    assert permissions.can("owner", "manage_members")
    with pytest.raises(HTTPError) as e:
        permissions.require(None, "read")
    assert e.value.status == 404
    with pytest.raises(HTTPError) as e:
        permissions.require("viewer", "edit")
    assert e.value.status == 403


def test_token_bucket():
    b = TokenBucket(2, 1)
    assert b.allow("k", now=0) and b.allow("k", now=0)
    assert not b.allow("k", now=0)
    assert b.allow("k", now=1.5)


async def test_login_logout_and_csrf(app):
    from tests.asgi_client import Client
    from docwizard.auth import users

    pw = passwords.hash_password("correct horse battery")
    await app.db.write(lambda c: users.create(c, "ali", pw))
    cl = Client(app)
    r = await cl.get("/")
    assert r.status == 303 and r.header("location").startswith("/login")
    r = await cl.post("/login", form={"username": "ali", "password": "nope", "next": "/"})
    assert r.status == 400 and "wrong" in r.text
    me = await cl.login("ali", "correct horse battery")
    assert me["user"]["username"] == "ali"
    # A cross-site POST is refused.
    r = await cl.post("/settings/profile", form={"display_name": "X"}, origin="https://evil.example")
    assert r.status == 403
    # Missing CSRF token is refused.
    token, cl.csrf = cl.csrf, None
    r = await cl.post("/settings/profile", form={"display_name": "X"})
    assert r.status == 403
    r = await cl.post("/settings/profile", form={"display_name": "Ali B", "_csrf": token, "theme": "light"})
    assert r.status == 303
    cl.csrf = token
    r = await cl.get("/settings")
    assert 'data-theme="light"' in r.text and "Ali B" in r.text
    r = await cl.post("/logout", form={"_csrf": token})
    assert r.status == 303
    assert (await cl.get("/api/me")).status == 401


async def test_security_headers(client):
    r = await client.get("/")
    csp = r.header("content-security-policy")
    assert "script-src 'self' 'nonce-" in csp and "frame-ancestors 'none'" in csp
    assert r.header("x-content-type-options") == "nosniff"


async def test_register_open(app):
    from tests.asgi_client import Client

    cl = Client(app)
    r = await cl.post("/register", form={"username": "newbie", "display_name": "New", "password": "short"})
    assert r.status == 400
    r = await cl.post("/register", form={"username": "newbie", "display_name": "New", "password": "a decent passphrase"})
    assert r.status == 303
    assert (await cl.get("/api/me")).json()["user"]["username"] == "newbie"

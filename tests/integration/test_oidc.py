"""OIDC login (§13.4) against a fake provider."""

from urllib.parse import parse_qs, urlsplit

import pytest

from docwizard import config
from docwizard.asgi import create_app
from docwizard.auth import passwords, users
from tests.asgi_client import Client
from tests.fakes.oidc import FakeOIDC


@pytest.fixture
async def op():
    server = FakeOIDC()
    yield server
    server.stop()


@pytest.fixture
async def oapp(cfg, op):
    cfg.oidc = [config.OIDCProvider(id="kc", label="University login", issuer=op.issuer, client_id="docwizard",
                                    client_secret="s3cret", scopes=["openid", "profile", "email", "groups"],
                                    allowed_groups=["students", "staff"], admin_groups=["dw-admins"])]
    app = create_app(cfg)
    app.net_resolver = lambda host, port: ["127.0.0.1"]
    await app.startup()
    await app.set_setting("allow_private_outbound", ["id.test"])
    yield app
    await app.shutdown()


async def login(app, op, client=None, sub="u-1", next_url="/", link=False, **claims):
    client = client or Client(app)
    r = await client.get(f"/login/oidc/kc?next={next_url}" + ("&link=1" if link else ""))
    assert r.status == 303, r.text
    auth = urlsplit(r.header("location"))
    q = {k: v[0] for k, v in parse_qs(auth.query).items()}
    assert auth.path.endswith("/auth") and q["code_challenge_method"] == "S256" and q["response_type"] == "code"
    assert q["redirect_uri"].endswith("/login/oidc/kc/callback")
    cookie = r.header("set-cookie").split(";")[0].split("=", 1)
    client.cookies[cookie[0]] = cookie[1]
    op.issue("code-1", sub, q["nonce"], q["code_challenge"], **claims)
    r = await client.get(f"/login/oidc/kc/callback?code=code-1&state={q['state']}")
    for header in [v for k, v in r.headers if k == "set-cookie"]:
        name, _, rest = header.partition("=")
        value = rest.split(";")[0]
        if value:
            client.cookies[name] = value
        else:
            client.cookies.pop(name, None)
    return client, r


async def test_first_login_creates_the_account(oapp, op):
    client, r = await login(oapp, op, next_url="/settings", preferred_username="maria.k", name="Maria K",
                            email="maria@example.org", groups=["students"])
    assert r.status == 303 and r.header("location") == "/settings", r.header("location")
    me = (await client.get("/api/me")).json()["user"]
    assert me["username"] == "maria.k" and me["displayName"] == "Maria K"
    row = await oapp.db.fetchone("SELECT password_hash, instance_role FROM users WHERE username = 'maria.k'")
    assert row["password_hash"] is None and row["instance_role"] == "user"
    # The same subject logs in to the same account; admin rights follow the groups each time.
    client2, _ = await login(oapp, op, groups=["staff", "dw-admins"], preferred_username="other-name")
    me2 = (await client2.get("/api/me")).json()["user"]
    assert me2["username"] == "maria.k"
    assert (await oapp.db.fetchone("SELECT instance_role FROM users WHERE username = 'maria.k'"))[0] == "admin"
    await login(oapp, op, groups=["staff"])
    assert (await oapp.db.fetchone("SELECT instance_role FROM users WHERE username = 'maria.k'"))[0] == "user"


async def test_groups_and_email_never_link_by_themselves(oapp, op):
    pw = passwords.hash_password("correct horse battery")
    await oapp.db.write(lambda c: users.create(c, "jane", pw, display_name="Jane", email="jane@example.org"))
    _, r = await login(oapp, op, groups=["visitors"])
    assert "group" in r.header("location")
    client, r = await login(oapp, op, sub="u-2", preferred_username="jane", email="jane@example.org",
                            groups=["staff"])
    me = (await client.get("/api/me")).json()["user"]
    assert me["username"] == "jane2"  # a new account, not Jane's


async def test_linking_an_existing_account(oapp, op):
    pw = passwords.hash_password("correct horse battery")
    await oapp.db.write(lambda c: users.create(c, "jane", pw, display_name="Jane"))
    jane = Client(oapp)
    await jane.login("jane", "correct horse battery")
    page = await jane.get("/settings")
    assert "Link University login" in page.text
    _, r = await login(oapp, op, client=jane, link=True, sub="u-jane", groups=["staff"])
    assert r.header("location").startswith("/settings?saved=linked")
    fresh, _ = await login(oapp, op, sub="u-jane", groups=["staff"])
    assert (await fresh.get("/api/me")).json()["user"]["username"] == "jane"
    # Logging out of an OIDC session continues at the provider.
    me = (await fresh.get("/api/me")).json()
    fresh.csrf = me["csrf"]
    r = await fresh.post("/logout", form={"_csrf": me["csrf"]})
    assert r.header("location").startswith(op.issuer + "/logout?")


async def test_bad_tokens_are_refused(oapp, op):
    from joserfc.jwk import OctKey, RSAKey

    cases = [
        ({"aud": "someone-else"}, {}, None),
        ({"iss": "http://evil.test"}, {}, None),
        ({"exp": 1000}, {}, None),
        ({}, {"alg": "HS256"}, OctKey.import_key("x" * 32)),
        ({}, {}, RSAKey.generate_key(2048, parameters={"kid": "k1"}, private=True)),  # wrong key, same kid
    ]
    for override, header, key in cases:
        op.override, op.header_override, op.sign_with = override, header, key
        client, r = await login(oapp, op, groups=["staff"])
        assert r.header("location").startswith("/login?error="), (override, header)
        assert (await client.get("/api/me")).status == 401
    op.override, op.header_override, op.sign_with = {}, {}, None
    # A callback whose state wasn't started in this browser.
    client = Client(oapp)
    r = await client.get("/login/oidc/kc")
    state = parse_qs(urlsplit(r.header("location")).query)["state"][0]
    r = await Client(oapp).get(f"/login/oidc/kc/callback?code=x&state={state}")
    assert "didn" in r.header("location")


async def test_key_rotation_refreshes_the_jwks(oapp, op):
    client, _ = await login(oapp, op, groups=["staff"])
    oapp.oidc._jwks_at["kc"] = 0  # the last refresh was long ago
    op.rotate_key()
    client, r = await login(oapp, op, groups=["staff"])
    assert r.header("location") == "/" and (await client.get("/api/me")).status == 200


async def test_login_page_lists_providers(oapp):
    page = await Client(oapp).get("/login")
    assert "Log in with University login" in page.text

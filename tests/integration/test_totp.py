"""Two-step login with TOTP and recovery codes (§13.2)."""

import re

from docwizard.auth import qr, totp
from tests.asgi_client import Client


def test_rfc6238_vectors():
    # RFC 6238 appendix B, SHA-1, secret "12345678901234567890", last six digits.
    import base64

    secret = base64.b32encode(b"12345678901234567890").decode()
    for at, code in [(59, "287082"), (1111111109, "081804"), (1234567890, "005924"), (2000000000, "279037")]:
        assert totp.code_at(secret, at // 30) == code
    assert totp.verify(secret, "287082", at=59) == 1
    assert totp.verify(secret, "287082", last_counter=1, at=59) is None  # no replay
    assert totp.verify(secret, "28708", at=59) is None
    assert totp.normalize_recovery("ABCDE FGHJK") == "abcde-fghjk"


def test_qr_matrix_shape():
    grid = qr.matrix(totp.provisioning_uri(totp.new_secret(), "jane", "DocWizard"))
    assert len(grid) in range(21, 58, 4)
    # Finder pattern corners are dark, the separator next to them light.
    assert grid[0][0] and grid[0][6] and not grid[0][7]
    assert "<svg" in qr.svg("hello")


async def csrf_of(client):
    page = await client.get("/settings")
    return re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)


async def enable(client):
    csrf = await csrf_of(client)
    r = await client.post("/settings/totp", form={"_csrf": csrf, "action": "begin"})
    assert r.status == 200 and "<svg" in r.text
    secret = re.search(r"Enter this key in the app: <code>([A-Z0-9 ]+)</code>", r.text).group(1).replace(" ", "")
    bad = await client.post("/settings/totp", form={"_csrf": csrf, "action": "enable", "code": "000000"})
    assert bad.status == 400
    code = totp.code_at(secret, totp.now_counter())
    r = await client.post("/settings/totp", form={"_csrf": csrf, "action": "enable", "code": code})
    assert r.status == 200, r.text
    codes = re.findall(r"<li><code>([a-z0-9]{5}-[a-z0-9]{5})</code></li>", r.text)
    assert len(codes) == 10
    return secret, codes


async def test_enable_login_with_code_and_recovery(app, client):
    secret, codes = await enable(client)
    row = await app.db.fetchone("SELECT totp_secret_enc FROM users WHERE username = 'jane'")
    assert secret not in row["totp_secret_enc"]
    assert "Two-step login is on" in (await client.get("/settings")).text

    fresh = Client(app)
    r = await fresh.post("/login", form={"username": "jane", "password": "correct horse battery", "next": "/p/x"})
    assert r.status == 303 and r.header("location") == "/login/totp"
    assert (await fresh.get("/api/me")).status == 401  # no session yet
    assert (await fresh.post("/login/totp", form={"code": "123456"})).status == 400
    # The code used during setup can't be replayed: wait for the next step only if needed.
    counter = totp.now_counter()
    used = (await app.db.fetchone("SELECT totp_last_counter FROM users WHERE username = 'jane'"))[0]
    if used >= counter:
        counter = used + 1  # within the accepted window of one step ahead
    r = await fresh.post("/login/totp", form={"code": totp.code_at(secret, counter)})
    assert r.status == 303 and r.header("location") == "/p/x"
    assert (await fresh.get("/api/me")).status == 200

    # Recovery codes work once.
    other = Client(app)
    await other.post("/login", form={"username": "jane", "password": "correct horse battery", "next": "/"})
    r = await other.post("/login/totp", form={"code": codes[0].upper().replace("-", " ")})
    assert r.status == 303
    again = Client(app)
    await again.post("/login", form={"username": "jane", "password": "correct horse battery", "next": "/"})
    assert (await again.post("/login/totp", form={"code": codes[0]})).status == 400

    # Turning it off needs the password.
    csrf = await csrf_of(client)
    r = await client.post("/settings/totp", form={"_csrf": csrf, "action": "disable", "password": "nope"})
    assert r.status == 400
    r = await client.post("/settings/totp", form={"_csrf": csrf, "action": "disable",
                                                  "password": "correct horse battery"})
    assert r.status == 303
    plain = Client(app)
    await plain.login("jane", "correct horse battery")


async def test_too_many_wrong_codes_end_the_attempt(app, client):
    await enable(client)
    fresh = Client(app)
    await fresh.post("/login", form={"username": "jane", "password": "correct horse battery", "next": "/"})
    for _ in range(4):
        assert (await fresh.post("/login/totp", form={"code": "000000"})).status == 400
    r = await fresh.post("/login/totp", form={"code": "000000"})
    assert r.status == 303 and r.header("location") == "/login"
    assert (await fresh.get("/login/totp")).status == 303


async def test_required_totp_blocks_until_enabled(app, client):
    await app.db.execute("UPDATE users SET require_totp = 1 WHERE username = 'jane'")
    r = await client.get("/")
    assert r.status == 303 and r.header("location").startswith("/settings?totp=required")
    assert (await client.get("/api/projects")).status == 403
    page = await client.get("/settings?totp=required")
    assert page.status == 200 and "needs two-step login" in page.text
    await enable(client)
    assert (await client.get("/")).status == 200
    csrf = await csrf_of(client)
    r = await client.post("/settings/totp", form={"_csrf": csrf, "action": "disable",
                                                  "password": "correct horse battery"})
    assert r.status == 400  # required: can't be turned off

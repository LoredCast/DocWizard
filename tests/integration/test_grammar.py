"""Grammar checking (§6.8): what the editor is told, the setting, and LanguageTool through the app."""

import json
import re
from urllib.parse import parse_qs

from docwizard.api import grammar
from docwizard.netpolicy import Policy, Response
from tests.integration.test_collab import new_project, open_editor

LT_ANSWER = {
    "language": {"detectedLanguage": {"code": "de-DE"}},
    "matches": [
        {"offset": 4, "length": 4, "message": "Möglicher Tippfehler gefunden.", "shortMessage": "Rechtschreibfehler",
         "replacements": [{"value": "Haus"}, {"value": "Maus"}], "rule": {"id": "GERMAN_SPELLER_RULE",
                                                                          "issueType": "misspelling"}},
        {"offset": 10, "length": 3, "message": "Kongruenz", "replacements": [],
         "rule": {"id": "DE_AGREEMENT", "issueType": "grammar", "category": {"name": "Grammatik"}}},
        {"offset": "x", "length": 1, "message": "broken"},
    ],
}


async def test_editor_is_told_what_this_server_offers(app, client, monkeypatch):
    monkeypatch.setattr(grammar, "harper_installed", lambda app: True)
    pid = await new_project(client)
    snap = await open_editor(client, pid)
    assert snap["grammar"] == {"harper": True, "languagetool": False, "choice": "auto"}
    assert "lang" in snap["project"]
    # Choosing in the settings.
    page = await client.get("/settings")
    assert 'name="grammar"' in page.text and "LanguageTool" not in page.text
    csrf = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
    r = await client.post("/settings/profile", form={"_csrf": csrf, "display_name": "Jane", "grammar": "off"})
    assert r.status == 303
    assert (await open_editor(client, pid))["grammar"]["choice"] == "off"
    await client.post("/settings/profile", form={"_csrf": csrf, "display_name": "Jane", "grammar": "nonsense"})
    assert (await open_editor(client, pid))["grammar"]["choice"] == "off"
    # Without Harper's files there is nothing to choose.
    monkeypatch.setattr(grammar, "harper_installed", lambda app: False)
    assert 'name="grammar"' not in (await client.get("/settings")).text


async def test_harper_is_served_and_allowed(app, client):
    from docwizard.asgi import WEB_DIR

    snap_page = await client.get(f"/p/{await new_project(client)}/paper.md")
    csp = snap_page.header("content-security-policy")
    assert "'wasm-unsafe-eval'" in csp and "worker-src 'self'" in csp
    # A release's service worker doesn't precache Harper's 20 MB.
    from docwizard.pages.offline import static_assets

    folder = WEB_DIR / "vendor" / "harper"
    made_folder = not folder.exists()
    folder.mkdir(parents=True, exist_ok=True)
    marker = folder / "VERSION"
    made_marker = not marker.exists()
    if made_marker:
        marker.write_text("test\n")
    try:
        assert not any("/vendor/harper/" in a for a in static_assets(app))
    finally:
        if made_marker:
            marker.unlink()
        if made_folder:
            folder.rmdir()


async def test_languagetool_through_the_app(app, client, monkeypatch):
    r = await client.post("/api/grammar/check", json={"text": "Das Hous", "language": "de-DE"})
    assert r.status == 404
    app.cfg.grammar.languagetool_url = "http://languagetool.internal:8010/"
    calls = []

    def fake_fetch(self, method, url, body=None, headers=None, **kw):
        calls.append((method, url, parse_qs(body.decode()), self))
        return Response(200, {}, json.dumps(LT_ANSWER).encode(), url)

    monkeypatch.setattr(Policy, "fetch", fake_fetch)
    snap = await open_editor(client, await new_project(client))
    assert snap["grammar"]["languagetool"] is True
    r = await client.post("/api/grammar/check", json={"text": "Das Hous ist gros.", "language": "de-DE"})
    assert r.status == 200, r.text
    method, url, form, _ = calls[0]
    assert (method, url) == ("POST", "http://languagetool.internal:8010/v2/check")
    assert form == {"text": ["Das Hous ist gros."], "language": ["de-DE"]}
    data = r.json()
    assert data["language"] == "de-DE"
    assert data["matches"] == [
        {"from": 4, "to": 8, "message": "Möglicher Tippfehler gefunden.", "title": "Rechtschreibfehler",
         "kind": "spelling", "suggestions": ["Haus", "Maus"], "rule": "GERMAN_SPELLER_RULE"},
        {"from": 10, "to": 13, "message": "Kongruenz", "title": "Grammatik", "kind": "grammar", "suggestions": [],
         "rule": "DE_AGREEMENT"},
    ]
    # Language detection asks for the usual variants; an API key goes along when there is one.
    app.cfg.grammar.languagetool_api_key = "k"
    app.cfg.grammar.languagetool_username = "u@example.org"
    await client.post("/api/grammar/check", json={"text": "Hello"})
    form = calls[1][2]
    assert form["language"] == ["auto"] and "preferredVariants" in form
    assert form["apiKey"] == ["k"] and form["username"] == ["u@example.org"]
    # Bad requests.
    assert (await client.post("/api/grammar/check", json={"text": "x", "language": "../etc"})).status == 400
    assert (await client.post("/api/grammar/check", json={"text": 5})).status == 400
    # An unreachable or failing server is a 502, not a crash.
    monkeypatch.setattr(Policy, "fetch", lambda self, *a, **kw: Response(500, {}, b"oops", ""))
    r = await client.post("/api/grammar/check", json={"text": "Hello", "language": "en-US"})
    assert r.status == 502 and "500" in r.json()["error"]["message"]


async def test_languagetool_needs_a_session(app):
    from tests.asgi_client import Client

    app.cfg.grammar.languagetool_url = "http://lt.example:8010"
    r = await Client(app).post("/api/grammar/check", json={"text": "Hello"})
    assert r.status in (401, 403)

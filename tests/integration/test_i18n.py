"""The German interface (§21): picked from the browser, a cookie or the account."""

import json
import re

from docwizard import i18n
from docwizard.projects import service as svc
from tests.asgi_client import Client


def test_catalog_is_complete_for_placeholders():
    de = i18n.catalog("de")
    assert de, "German catalog missing"
    for key, value in de.items():
        assert set(re.findall(r"\{(\w+)\}", key)) == set(re.findall(r"\{(\w+)\}", value)), key
    assert i18n.gettext("de", "Projects") == "Projekte"
    assert i18n.gettext("en", "Projects") == "Projects"
    assert i18n.gettext("de", "no such message {n}", n=3) == "no such message 3"
    assert i18n.pick("de-AT,de;q=0.9,en;q=0.5") == "de"
    assert i18n.pick("fr-FR,fr;q=0.9") == "en"
    assert i18n.pick(None) == "en"


async def test_browser_language_and_account_choice(app, client):
    anon = Client(app)
    page = await anon.get("/login", headers={"Accept-Language": "de-DE,de;q=0.9"})
    assert page.status == 200 and '<html lang="de"' in page.text and "Passwort vergessen?" in page.text
    page = await anon.get("/login", headers={"Accept-Language": "en-US"})
    assert '<html lang="en"' in page.text and "Forgot your password?" in page.text

    # Error pages and JSON errors are translated too.
    gone = await anon.get("/reset/nonsense", headers={"Accept-Language": "de"})
    assert gone.status == 404 and "abgelaufen" in gone.text

    # The account's choice wins over the browser and sticks in a cookie.
    settings = await client.get("/settings")
    csrf = re.search(r'name="_csrf" value="([^"]+)"', settings.text).group(1)
    r = await client.post("/settings/profile", form={"_csrf": csrf, "display_name": "Jane", "theme": "dark",
                                                    "language": "de", "editor_font": "sans", "editor_size": "16"})
    assert r.status == 303 and any(k.lower() == "set-cookie" and v.startswith("lang=de") for k, v in r.headers)
    home = await client.get("/", headers={"Accept-Language": "en"})
    assert '<html lang="de"' in home.text and "Zuletzt geöffnet" in home.text and "Neues Projekt" in home.text
    # The editor's scripts get their strings from the page.
    data = re.search(r'<script type="application/json" id="i18n">(.*?)</script>', home.text, re.S)
    assert data and json.loads(data.group(1)).get("New project") == "Neues Projekt"
    admin = await client.get("/admin/users")
    assert "Benutzer hinzufügen" in admin.text and "Verwaltung" in admin.text
    health = await client.get("/admin")
    assert "Volltextsuche" in health.text and "Speicherplatz" in health.text
    # New projects start with a German paper.
    client.csrf = (await client.get("/api/me")).json()["csrf"]
    r = await client.post("/api/projects", json={"name": "Versuch"}, headers={"Accept-Language": "en"})
    assert r.status == 201, r.text
    doc = r.json()["mainDocument"]
    text = await app.db.read(lambda c: svc.load_state(c, doc).text)
    assert "# Einleitung" in text and "lang: de-DE" in text

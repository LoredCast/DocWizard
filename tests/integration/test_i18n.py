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
    assert i18n.pick("fr-FR,fr;q=0.9") == "fr"
    assert i18n.pick("es-MX,es;q=0.9,en;q=0.5") == "es"
    assert i18n.pick("it-IT") == "en"


def test_every_language_has_every_german_string():
    """French and Spanish follow the German catalog, which follows the source strings."""
    de = i18n.catalog("de")
    for lang in ("fr", "es"):
        catalog = i18n.catalog(lang)
        assert set(catalog) == set(de), (lang, sorted(set(de) ^ set(catalog))[:10])
        for key, value in catalog.items():
            assert set(re.findall(r"\{(\w+)\}", key)) == set(re.findall(r"\{(\w+)\}", value)), (lang, key)
    assert i18n.gettext("fr", "Projects") == "Projets"
    assert i18n.gettext("es", "{n} words", n=3) == "3 palabras"
    assert i18n.reltime("fr", "2020-03-04T10:00:00Z") == "4 mars 2020"
    assert i18n.reltime("es", "2020-08-04T10:00:00Z") == "4 ago. 2020"
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
                                                    "language": "de", "editor_font": "serif", "editor_size": "16"})
    assert r.status == 303 and any(k.lower() == "set-cookie" and v.startswith("lang=de") for k, v in r.headers)
    settings_after = await client.get("/settings")
    assert '<option value="serif" selected>Serifen</option>' in settings_after.text
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
    # Editor snapshot carries the chosen serif font preference.
    ed = await client.get(f"/p/{r.json()['id']}/{r.json()['mainPath']}")
    snap_m = re.search(r'<script type="application/json" id="snapshot">(.*?)</script>', ed.text, re.S)
    assert snap_m and json.loads(snap_m.group(1))["prefs"]["font"] == "serif"


async def test_quick_language_switch_and_footer_links(app, client):
    from tests.integration.test_collab import new_project
    from tests.integration.test_sharing import as_guest, share

    # Logged-out visitor switches language from the login page footer.
    anon = Client(app)
    login = await anon.get("/login", headers={"Accept-Language": "en-US"})
    assert 'action="/language"' in login.text and 'href="/source"' in login.text
    r = await anon.post("/language", form={"lang": "de", "next": "/login"})
    assert r.status == 303 and r.header("location") == "/login"
    assert any(k.lower() == "set-cookie" and v.startswith("lang=de") for k, v in r.headers)
    login_de = await anon.get("/login", headers={"Accept-Language": "en-US"})
    assert '<html lang="de"' in login_de.text and "Passwort vergessen?" in login_de.text
    # Open redirects in next are rejected.
    bad = await anon.post("/language", form={"lang": "fr", "next": "//evil.example/phish"})
    assert bad.status == 303 and bad.header("location") == "/"

    # Logged-in user switches language from any page and stays on that page; Source code only on home/login.
    pid = await new_project(client)
    home = await client.get("/")
    assert 'href="/source"' in home.text and 'action="/language"' in home.text
    project = await client.get(f"/p/{pid}")
    assert 'href="/source"' not in project.text and 'action="/language"' in project.text
    editor = await client.get(f"/p/{pid}/paper.md")
    assert 'href="/source"' not in editor.text and 'action="/language"' in editor.text
    settings = await client.get("/settings")
    assert 'href="/source"' not in settings.text and 'action="/language"' in settings.text

    csrf = re.search(r'name="_csrf" value="([^"]+)"', project.text).group(1)
    r = await client.post("/language", form={"_csrf": csrf, "lang": "fr", "next": f"/p/{pid}?tab=files"})
    assert r.status == 303 and r.header("location") == f"/p/{pid}?tab=files"
    project_fr = await client.get(f"/p/{pid}", headers={"Accept-Language": "en-US"})
    assert '<html lang="fr"' in project_fr.text and "Fichiers" in project_fr.text

    # Guests can also switch language via /language.
    token = await share(client, pid)
    guest, loc = await as_guest(app, token)
    r = await guest.post("/language", form={"_csrf": guest.csrf, "lang": "es", "next": loc})
    assert r.status == 303 and r.header("location") == loc
    guest_editor = await guest.get(loc, headers={"Accept-Language": "en-US"})
    assert '<html lang="es"' in guest_editor.text

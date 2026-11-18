"""Settings for a user's Zotero libraries (§10.2): the Web API, Zotero desktop's local API,
and the WebDAV folder Zotero syncs attachment files to."""

from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlsplit

from ..citations.files import check_storage
from ..citations.zotero import LOCAL_API, NAME_RE, ZoteroError
from ..http.request import Request
from ..http.response import Response, redirect
from ..secretbox import context_for


def _http_url(value: str) -> bool:
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.hostname) and not (parts.username or parts.password)


def register(app) -> None:
    async def zotero(req: Request) -> Response:
        form = await req.form()
        action = form.get("action")
        uid = req.user.id

        async def fail(message: str, values: dict | None = None) -> Response:
            return await app.render_settings(req, 400, saved=None, error=None, zotero_error=message,
                                             zotero_values=values or {})

        if action in ("refresh", "remove", "storage", "storage_remove"):
            sid = form.get("source_id", "")
            row = await app.db.fetchone("SELECT * FROM citation_sources WHERE id = ? AND owner_id = ?",
                                        (int(sid) if sid.isdigit() else -1, uid))
            if row is None:
                return redirect("/settings#zotero")
            if action == "remove":
                await app.db.execute("DELETE FROM citation_sources WHERE id = ?", (row["id"],))
            elif action == "refresh":
                await app.zotero.refresh(row["id"])
            elif action == "storage_remove":
                await app.db.execute("UPDATE citation_sources SET storage_enc = NULL WHERE id = ?", (row["id"],))
            else:
                url = form.get("storage_url", "").strip()
                username = form.get("storage_username", "").strip()
                password = form.get("storage_password", "")
                if not _http_url(url) or len(url) > 1000:
                    return await fail("Enter the WebDAV address you gave Zotero, starting with https://.")
                try:
                    await asyncio.to_thread(check_storage, app.net, url, username, password)
                except ZoteroError as exc:
                    return await fail(str(exc))
                sealed = app.zotero.seal_storage(row["id"], {"url": url, "username": username, "password": password})
                await app.db.execute("UPDATE citation_sources SET storage_enc = ? WHERE id = ?", (sealed, row["id"]))
                return redirect("/settings?saved=zotero#zotero")
            return redirect("/settings#zotero")

        app.require_verified_email(req, "zotero")
        values = {k: form.get(k, "").strip()
                  for k in ("name", "connection", "library_type", "library_id", "collection", "api_url")}
        api_key = form.get("api_key", "").strip()
        local = values["connection"] == "local"
        if not NAME_RE.match(values["name"]):
            return await fail("Names use letters, digits, - and _, up to 40 characters.", values)
        if values["library_type"] not in ("user", "group"):
            return await fail("Choose your library or a group library.", values)
        if local and values["library_type"] == "user" and not values["library_id"]:
            values["library_id"] = "0"  # the local API's name for the signed-in user's library
        if not re.fullmatch(r"\d{1,12}", values["library_id"]):
            return await fail("Enter the numeric ID of your Zotero user or group.", values)
        if values["collection"] and not re.fullmatch(r"[A-Z0-9]{8}", values["collection"]):
            return await fail("Collection keys are 8 letters and digits, as in the collection's web address.", values)
        api_url = None
        if local:
            api_url = (values["api_url"] or LOCAL_API).rstrip("/")
            if not _http_url(api_url) or len(api_url) > 300:
                return await fail("Enter the address of Zotero's local API, such as http://localhost:23119/api.",
                                  values)
            api_key = ""
        elif not re.fullmatch(r"[A-Za-z0-9]{10,64}", api_key):
            return await fail("Paste the API key from zotero.org.", values)
        exists = await app.db.fetchone("SELECT 1 FROM citation_sources WHERE owner_id = ? AND name = ?",
                                       (uid, values["name"]))
        if exists:
            return await fail("You already have a library with this name.", values)

        def work(c):
            sid = c.execute(
                "INSERT INTO citation_sources(owner_id, name, kind, library_type, library_id, collection, config_enc,"
                " api_url) VALUES (?, ?, 'zotero_web', ?, ?, ?, '', ?)",
                (uid, values["name"], values["library_type"], values["library_id"], values["collection"] or None,
                 api_url)).lastrowid
            c.execute("UPDATE citation_sources SET config_enc = ? WHERE id = ?",
                      (app.secrets.seal(json.dumps({"api_key": api_key}), context_for("citation_sources", sid)), sid))
            return sid

        sid = await app.db.write(work)
        result = await app.zotero.refresh(sid)
        if "error" in result:
            await app.db.execute("DELETE FROM citation_sources WHERE id = ?", (sid,))
            return await fail(result["error"], values)
        return redirect("/settings?saved=zotero#zotero")

    app.router.add("POST", "/settings/zotero", zotero)

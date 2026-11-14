"""Settings for a user's Zotero libraries (§10.2)."""

from __future__ import annotations

import json
import re

from ..citations.zotero import NAME_RE
from ..http.request import Request
from ..http.response import Response, redirect
from ..secretbox import context_for


def register(app) -> None:
    async def zotero(req: Request) -> Response:
        form = await req.form()
        action = form.get("action")
        uid = req.user.id
        if action in ("refresh", "remove"):
            sid = form.get("source_id", "")
            row = await app.db.fetchone("SELECT id FROM citation_sources WHERE id = ? AND owner_id = ?",
                                        (int(sid) if sid.isdigit() else -1, uid))
            if row is not None:
                if action == "remove":
                    await app.db.execute("DELETE FROM citation_sources WHERE id = ?", (row["id"],))
                else:
                    await app.zotero.refresh(row["id"])
            return redirect("/settings#zotero")

        values = {k: form.get(k, "").strip() for k in ("name", "library_type", "library_id", "collection")}
        api_key = form.get("api_key", "").strip()

        async def fail(message: str) -> Response:
            return await app.render_settings(req, 400, saved=None, error=None, zotero_error=message,
                                             zotero_values=values)

        if not NAME_RE.match(values["name"]):
            return await fail("Names use letters, digits, - and _, up to 40 characters.")
        if values["library_type"] not in ("user", "group") or not re.fullmatch(r"\d{1,12}", values["library_id"]):
            return await fail("Enter the numeric ID of your Zotero user or group.")
        if values["collection"] and not re.fullmatch(r"[A-Z0-9]{8}", values["collection"]):
            return await fail("Collection keys are 8 letters and digits, as in the collection's web address.")
        if not re.fullmatch(r"[A-Za-z0-9]{10,64}", api_key):
            return await fail("Paste the API key from zotero.org.")
        exists = await app.db.fetchone("SELECT 1 FROM citation_sources WHERE owner_id = ? AND name = ?",
                                       (uid, values["name"]))
        if exists:
            return await fail("You already have a library with this name.")

        def work(c):
            sid = c.execute(
                "INSERT INTO citation_sources(owner_id, name, kind, library_type, library_id, collection, config_enc)"
                " VALUES (?, ?, 'zotero_web', ?, ?, ?, '')",
                (uid, values["name"], values["library_type"], values["library_id"], values["collection"] or None)
            ).lastrowid
            c.execute("UPDATE citation_sources SET config_enc = ? WHERE id = ?",
                      (app.secrets.seal(json.dumps({"api_key": api_key}), context_for("citation_sources", sid)), sid))
            return sid

        sid = await app.db.write(work)
        result = await app.zotero.refresh(sid)
        if "error" in result:
            await app.db.execute("DELETE FROM citation_sources WHERE id = ?", (sid,))
            return await fail(result["error"])
        return redirect("/settings?saved=zotero#zotero")

    app.router.add("POST", "/settings/zotero", zotero)

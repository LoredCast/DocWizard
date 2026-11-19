"""Data sheets kept up to date from the web (§11.3a).

A .csv or .tsv document can name an HTTPS address. Refreshing fetches it through the outbound
request policy (netpolicy.py: no private addresses unless an admin allows them, redirects
checked, size and time limits) and applies the new text as an ordinary edit, so open grids,
the history and every collaborator see it like any other change. Nothing is fetched on its own:
someone who may edit presses Refresh.
"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit

from ..http.request import HTTPError, Request, bad_request
from ..http.response import Response, json_response
from ..netpolicy import FetchError, PolicyError
from ..projects import files
from .projects import author_of, document_access, enforce_quota, user_id_of

MAX_BYTES = 5 * 1024 * 1024
SCHEMES = ("https",)
TIMEOUT = 20.0


def check_url(url: str) -> str:
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in SCHEMES or not parts.hostname:
        raise bad_request("Use an https:// address.")
    if parts.username or parts.password:
        raise bad_request("Addresses can't contain a user name or password.")
    if len(url) > 2000:
        raise bad_request("The address is too long.")
    return url


def decode(data: bytes) -> str:
    """CSV files are UTF-8 (with or without a byte order mark); older exports are often Windows-1252."""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("cp1252", "replace")
    if "\x00" in text:
        raise bad_request("That address doesn't give a text file.")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def register(app) -> None:
    r = app.router

    async def sheet(req: Request, capability: str):
        doc, project, role = await document_access(app, req, req.param("id"), capability)
        if not files.is_sheet(doc["path"]):
            raise bad_request("Only data sheets (.csv, .tsv) can be refreshed from an address.")
        return doc, project, role

    async def get_source(req: Request) -> Response:
        doc, _, _ = await sheet(req, "read")
        row = await app.db.fetchone("SELECT url, refreshed_at, status FROM data_sources WHERE doc_id = ?", (doc["id"],))
        if row is None:
            return json_response({"url": None})
        return json_response({"url": row["url"], "refreshedAt": row["refreshed_at"], "status": row["status"]})

    async def put_source(req: Request) -> Response:
        doc, _, _ = await sheet(req, "edit")
        body = await req.json_object()
        url = body.get("url")
        if not url:
            await app.db.execute("DELETE FROM data_sources WHERE doc_id = ?", (doc["id"],))
            return json_response({"url": None})
        if not isinstance(url, str):
            raise bad_request("The address must be text.")
        url = check_url(url)
        await app.db.execute(
            "INSERT INTO data_sources(doc_id, url, set_by) VALUES (?, ?, ?) ON CONFLICT(doc_id) DO UPDATE SET"
            " url = excluded.url, set_by = excluded.set_by, refreshed_at = NULL, status = NULL, etag = NULL",
            (doc["id"], url, user_id_of(req)))
        return json_response({"url": url})

    async def refresh(req: Request) -> Response:
        doc, _, _ = await sheet(req, "edit")
        row = await app.db.fetchone("SELECT url FROM data_sources WHERE doc_id = ?", (doc["id"],))
        if row is None:
            raise bad_request("This sheet has no address to refresh from.")
        url = row["url"]
        policy = app.net
        try:
            resp = await asyncio.to_thread(policy.fetch, "GET", url, headers={"Accept": "text/csv, text/plain, */*"},
                                           timeout=TIMEOUT, max_bytes=MAX_BYTES)
        except (PolicyError, FetchError, OSError) as exc:
            await _status(doc["id"], f"error: {exc}"[:300])
            raise HTTPError(502, "fetch_failed", f"The address couldn't be fetched: {exc}") from None
        if urlsplit(resp.url).scheme not in SCHEMES:
            raise HTTPError(502, "fetch_failed", "The address redirected to a page without https.")
        if resp.status != 200:
            await _status(doc["id"], f"HTTP {resp.status}")
            raise HTTPError(502, "fetch_failed", f"The address answered with HTTP {resp.status}.")
        text = decode(resp.body)
        await enforce_quota(app, doc["project_id"], len(text.encode()), new_files=0)
        room = await app.rooms.get(doc["id"])
        before = await room.call(lambda: _version(room))
        after = await room.call(lambda: room.apply_text(text, "system:data-source", author_of(req)))
        await _status(doc["id"], "ok")
        return json_response({"changed": after != before, "version": after,
                              "rows": text.count("\n") + (0 if text.endswith("\n") or not text else 1)})

    async def _status(doc_id: str, status: str) -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await app.db.execute("UPDATE data_sources SET status = ?, refreshed_at = ? WHERE doc_id = ?",
                             (status, now, doc_id))

    r.add("GET", "/api/documents/{id}/source", get_source, guest=True)
    r.add("PUT", "/api/documents/{id}/source", put_source, guest=True)
    r.add("POST", "/api/documents/{id}/source/refresh", refresh, guest=True)


async def _version(room):
    return room.version

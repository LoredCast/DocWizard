"""JSON API: browsing your Zotero libraries from the editor (§10.4).

Search, collections, item details and attachment files of the signed-in user's own
Zotero sources. Collaborators cite from a library through the project's citation
list instead (``/api/projects/{id}/citations``), which never includes files.
"""

from __future__ import annotations

import asyncio
import json
import re

from ..citations.files import AttachmentFiles
from ..citations.library import label, summarize
from ..citations.zotero import ITEM_KEY_RE, ZoteroError, attachment_row, desktop_url, fold, web_url
from ..http.request import HTTPError, Request, bad_request, not_found
from ..http.response import Response, json_response
from ..netpolicy import FetchError, PolicyError
from ..secretbox import SecretError

MAX_LIMIT = 100
_TOKEN = re.compile(r"\S+")


def _like(token: str) -> str:
    return "%" + token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def source_json(row) -> dict:
    return {"id": row["id"], "name": row["name"], "connection": "local" if row["api_url"] else "web",
            "libraryType": row["library_type"], "libraryId": row["library_id"], "collection": row["collection"],
            "status": row["status"], "lastError": row["last_error"], "lastSyncAt": row["last_sync_at"],
            "itemCount": row["item_count"], "webdav": bool(row["storage_enc"]),
            # Files come from Zotero's storage, or from WebDAV for a local library.
            "files": bool(row["storage_enc"]) or not row["api_url"]}


def item_json(src, row) -> dict | None:
    try:
        csl = json.loads(row["csl_json"])
    except ValueError:
        return None
    s = summarize(csl)
    if s is None:
        return None
    s["label"] = label(s)
    s["source"] = f"zotero:{src['name']}"
    s["zotero"] = {"source": src["name"], "sourceId": src["id"], "item": row["item_key"],
                   "pdf": bool(row["has_pdf"])}
    return s


def register(app) -> None:
    r = app.router
    files = AttachmentFiles(app)
    app.zotero_files = files

    async def own_source(req: Request):
        sid = req.param("sid")
        row = await app.db.fetchone(
            "SELECT s.*, (SELECT COUNT(*) FROM citation_items i WHERE i.source_id = s.id) AS item_count"
            " FROM citation_sources s WHERE s.id = ? AND s.owner_id = ?",
            (int(sid) if sid.isdigit() else -1, req.user.id))
        if row is None:
            raise not_found("This library doesn't exist or isn't yours.")
        return row

    async def list_sources(req: Request) -> Response:
        rows = await app.db.fetchall(
            "SELECT s.*, (SELECT COUNT(*) FROM citation_items i WHERE i.source_id = s.id) AS item_count"
            " FROM citation_sources s WHERE s.owner_id = ? ORDER BY s.name", (req.user.id,))
        return json_response({"sources": [source_json(row) for row in rows]})

    async def refresh(req: Request) -> Response:
        src = await own_source(req)
        result = await app.zotero.refresh(src["id"])
        src = await own_source(req)
        if "error" in result:
            raise HTTPError(502, "zotero", result["error"])
        return json_response({"source": source_json(src), **result})

    async def collections(req: Request) -> Response:
        src = await own_source(req)
        rows = await app.db.fetchall(
            "SELECT collection_key, name, parent_key FROM citation_collections WHERE source_id = ? ORDER BY name",
            (src["id"],))
        cols = [{"key": c["collection_key"], "name": c["name"], "parent": c["parent_key"]} for c in rows]
        if src["collection"]:
            # A source limited to one collection shows what lies below it.
            below, frontier = set(), {src["collection"]}
            while frontier:
                frontier = {c["key"] for c in cols if c["parent"] in frontier} - below
                below |= frontier
            cols = [{**c, "parent": None if c["parent"] == src["collection"] else c["parent"]}
                    for c in cols if c["key"] in below]
        return json_response({"collections": cols})

    async def search(sources: list, q: str, collection: str, limit: int, offset: int) -> tuple[list, int]:
        if not sources:
            return [], 0
        where = [f"i.source_id IN ({','.join('?' * len(sources))})"]
        args: list = [s["id"] for s in sources]
        tokens = _TOKEN.findall(fold(q))[:8]
        for token in tokens:
            where.append("i.search_text LIKE ? ESCAPE '\\'")
            args.append(_like(token))
        if collection:
            # The collection and every collection below it, as in Zotero with subcollections shown.
            rows = await app.db.fetchall("SELECT collection_key, parent_key FROM citation_collections"
                                         " WHERE source_id = ?", (sources[0]["id"],))
            keys, frontier = {collection}, {collection}
            while frontier:
                frontier = {x["collection_key"] for x in rows if x["parent_key"] in frontier} - keys
                keys |= frontier
            where.append(f"EXISTS (SELECT 1 FROM json_each(i.collections) j WHERE j.value IN"
                         f" ({','.join('?' * len(keys))}))")
            args += sorted(keys)
        cond = " AND ".join(where)
        # Keys and first names that start with the first word come first, then the newest.
        order = "i.search_text LIKE ? ESCAPE '\\' DESC, " if tokens else ""
        order_args = [tokens[0].replace("%", "\\%").replace("_", "\\_") + "%"] if tokens else []
        total = (await app.db.fetchone(f"SELECT COUNT(*) AS n FROM citation_items i WHERE {cond}", tuple(args)))["n"]
        rows = await app.db.fetchall(
            "SELECT i.source_id, i.item_key, i.csl_json, EXISTS(SELECT 1 FROM citation_attachments a"
            " WHERE a.source_id = i.source_id AND a.parent_key = i.item_key AND a.content_type = 'application/pdf')"
            f" AS has_pdf FROM citation_items i WHERE {cond} ORDER BY {order}i.added_at DESC, i.item_key"
            " LIMIT ? OFFSET ?", (*args, *order_args, limit, offset))
        by_id = {s["id"]: s for s in sources}
        return [x for row in rows if (x := item_json(by_id[row["source_id"]], row))], total

    def _paging(req: Request) -> tuple[int, int]:
        try:
            limit = min(MAX_LIMIT, max(1, int(req.query.get("limit", "50"))))
            offset = max(0, int(req.query.get("offset", "0")))
        except ValueError:
            raise bad_request("limit and offset are numbers.") from None
        return limit, offset

    async def items(req: Request) -> Response:
        src = await own_source(req)
        limit, offset = _paging(req)
        collection = req.query.get("collection", "")
        if collection and not ITEM_KEY_RE.match(collection):
            raise bad_request("Collection keys are 8 letters and digits.")
        found, total = await search([src], req.query.get("q", "")[:200], collection, limit, offset)
        return json_response({"items": found, "total": total})

    async def search_all(req: Request) -> Response:
        """Autocomplete across all of your libraries."""
        sources = await app.db.fetchall("SELECT * FROM citation_sources WHERE owner_id = ? ORDER BY name",
                                        (req.user.id,))
        limit, _ = _paging(req)
        q = req.query.get("q", "")[:200]
        if not q.strip():
            return json_response({"items": [], "total": 0})
        found, total = await search(list(sources), q, "", min(limit, 30), 0)
        return json_response({"items": found, "total": total})

    async def item_detail(req: Request) -> Response:
        src = await own_source(req)
        key = req.param("key")
        row = await app.db.fetchone(
            "SELECT i.*, 0 AS has_pdf FROM citation_items i WHERE i.source_id = ? AND i.item_key = ?",
            (src["id"], key))
        if row is None:
            raise not_found("This item isn't in the library.")
        csl = json.loads(row["csl_json"])
        # Attachments as Zotero has them now; the stored list when Zotero can't be reached.
        stored = await app.db.fetchall("SELECT * FROM citation_attachments WHERE source_id = ? AND parent_key = ?"
                                       " ORDER BY title", (src["id"], key))
        atts = [dict(a) for a in stored]
        live_error = None
        try:
            client = app.zotero.client(src)
            children = await asyncio.wait_for(asyncio.to_thread(
                client.children, src["library_type"], src["library_id"], key), 20)
            live = [a for c in children if (a := attachment_row(c))]

            def save(c):
                c.execute("DELETE FROM citation_attachments WHERE source_id = ? AND parent_key = ?", (src["id"], key))
                for a in live:
                    c.execute("INSERT OR REPLACE INTO citation_attachments(source_id, attachment_key, parent_key,"
                              " title, content_type, filename, link_mode, url, md5) VALUES (:sid, :attachment_key,"
                              " :parent_key, :title, :content_type, :filename, :link_mode, :url, :md5)",
                              {"sid": src["id"], **a})

            await app.db.write(save)
            atts = live
        except (ZoteroError, FetchError, PolicyError, SecretError, ValueError, KeyError, TimeoutError) as exc:
            live_error = str(exc)
        summary = item_json(src, row) or {}
        summary["zotero"]["pdf"] = any(a["content_type"] == "application/pdf" for a in atts)
        fields = {k: csl.get(k) for k in ("DOI", "URL", "ISBN", "publisher", "publisher-place", "volume", "issue",
                                          "page", "edition", "abstract", "language") if isinstance(csl.get(k), str)}
        files_ok = bool(src["storage_enc"]) or not src["api_url"]
        return json_response({
            "item": summary, "fields": fields, "csl": csl,
            "links": {"web": None if src["api_url"] else web_url(src["library_type"], src["library_id"], key),
                      "desktop": desktop_url(src["library_type"], src["library_id"], key)},
            "attachments": [{
                "key": a["attachment_key"], "title": a["title"] or a["filename"], "contentType": a["content_type"],
                "filename": a["filename"], "linkMode": a["link_mode"], "url": a["url"],
                "file": files_ok and a["link_mode"] in ("imported_file", "imported_url"),
                "desktop": desktop_url(src["library_type"], src["library_id"], a["attachment_key"], pdf=True),
            } for a in atts],
            "attachmentsError": live_error,
        })

    async def attachment_file(req: Request) -> Response:
        src = await own_source(req)
        key = req.param("key")
        att = await app.db.fetchone("SELECT * FROM citation_attachments WHERE source_id = ? AND attachment_key = ?",
                                    (src["id"], key))
        if att is None:
            raise not_found("This attachment isn't in the library. Open its item again to refresh the list.")
        try:
            data = await files.get(src, att)
        except ZoteroError as exc:
            raise HTTPError(exc.status if exc.status in (404, 409) else 502, "zotero_file", str(exc)) from None
        is_pdf = data[:1024].lstrip().startswith(b"%PDF")
        resp = Response(data, 200, "application/pdf" if is_pdf else "application/octet-stream")
        name = re.sub(r'[^\w.\- ]', "_", att["filename"] or f"{key}.pdf")[:120]
        resp.set_header("content-disposition", f'{"inline" if is_pdf else "attachment"}; filename="{name}"')
        resp.set_header("x-content-type-options", "nosniff")
        resp.set_header("cache-control", "private, max-age=3600")
        if not is_pdf:
            resp.set_header("content-security-policy", "sandbox; default-src 'none'")
        return resp

    r.add("GET", "/api/citations/sources", list_sources)
    r.add("GET", "/api/citations/search", search_all)
    r.add("POST", "/api/citations/sources/{sid}/refresh", refresh)
    r.add("GET", "/api/citations/sources/{sid}/collections", collections)
    r.add("GET", "/api/citations/sources/{sid}/items", items)
    r.add("GET", "/api/citations/sources/{sid}/items/{key}", item_detail)
    r.add("GET", "/api/citations/sources/{sid}/attachments/{key}/file", attachment_file)

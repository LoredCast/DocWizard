"""Zotero Web API client and library refresh (§10.2, §10.3).

Libraries are fetched server-side as CSL-JSON, incrementally: items modified since
the stored library version, plus the keys deleted since then. Every item gets a
citation key that never changes once assigned: the CSL ``citation-key`` field, a
``Citation Key: …`` line in Zotero's Extra field (Better BibTeX's pinned keys), or
a generated author-year key (``smith2020``, ``smith2020a``, …).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import quote, urlencode, urljoin

from ..logs import fields
from ..netpolicy import FetchError, PolicyError
from ..secretbox import SecretError, context_for

log = logging.getLogger("docwizard.zotero")

API = "https://api.zotero.org"
LOCAL_API = "http://localhost:23119/api"
PAGE = 100
MAX_ITEMS = 20_000
MAX_FILE_BYTES = 150_000_000
REFRESH_SECONDS = 15 * 60
# Notes and annotations are never cited; attachments are kept for their files.
SKIP_TYPES = "-note || annotation"
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
KEY_RE = re.compile(r"^[A-Za-z0-9_:.#$%&+?<>~/-]{1,100}$")
ITEM_KEY_RE = re.compile(r"^[A-Z0-9]{8}$")


class ZoteroError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def library_path(library_type: str, library_id: str, collection: str | None = None) -> str:
    base = f"/{'users' if library_type == 'user' else 'groups'}/{quote(library_id, safe='')}"
    return base + (f"/collections/{quote(collection, safe='')}" if collection else "")


def web_url(library_type: str, library_id: str, item_key: str) -> str:
    kind = "users" if library_type == "user" else "groups"
    return f"https://www.zotero.org/{kind}/{library_id}/items/{item_key}"


def desktop_url(library_type: str, library_id: str, item_key: str, *, pdf: bool = False,
                page: int | None = None) -> str:
    """zotero:// links that open an item, or an attachment at a page, in Zotero desktop."""
    library = "library" if library_type == "user" else f"groups/{library_id}"
    if pdf:
        return f"zotero://open-pdf/{library}/items/{item_key}" + (f"?page={page}" if page else "")
    return f"zotero://select/{library}/items/{item_key}"


@dataclass
class Changes:
    version: int
    items: list = field(default_factory=list)          # Zotero JSON objects with data and csljson
    collections: list = field(default_factory=list)    # Zotero JSON collection objects
    deleted_items: list = field(default_factory=list)
    deleted_collections: list = field(default_factory=list)
    complete: bool = True                              # False when MAX_ITEMS cut the list short


class Client:
    """The Zotero Web API, or Zotero desktop's local API, which answers the same way.

    Blocking: call from a thread.
    """

    def __init__(self, policy, api_key: str = "", base: str = API):
        self.policy = policy
        self.base = base.rstrip("/")
        self.local = self.base != API
        self.headers = {"Zotero-API-Version": "3"}
        if api_key:
            self.headers["Zotero-API-Key"] = api_key
        self.server_id: str | None = None

    def _get(self, path: str, params: dict, extra: dict | None = None, *, follow: bool = True,
             max_bytes: int = 50_000_000, ok: tuple = (200, 304)):
        url = f"{self.base}{path}" + (f"?{urlencode(params)}" if params else "")
        resp = self.policy.fetch("GET", url, headers={**self.headers, **(extra or {})}, timeout=60,
                                 max_bytes=max_bytes, follow_redirects=follow)
        self.server_id = resp.header("zotero-server-id") or self.server_id
        if resp.status in (403, 401):
            if self.local:
                raise ZoteroError("Zotero's local API is off. In Zotero, open Settings › Advanced and turn on"
                                  " “Allow other applications on this computer to communicate with Zotero”.",
                                  resp.status)
            raise ZoteroError("Zotero didn't accept the API key for this library.", resp.status)
        if resp.status == 404:
            raise ZoteroError("Zotero doesn't know this library or collection.", 404)
        if resp.status == 429 or resp.status >= 500:
            raise ZoteroError(f"Zotero is busy (HTTP {resp.status}). Trying again later.", resp.status)
        if resp.status not in ok:
            raise ZoteroError(f"Zotero answered HTTP {resp.status}.", resp.status)
        return resp

    def _all(self, path: str, params: dict, since: int, limit: int = MAX_ITEMS):
        """Every page of a multi-object request: (version, objects, complete), or None when unchanged."""
        out: list[dict] = []
        start = 0
        version = since
        while True:
            query = {**params, "format": "json", "limit": PAGE, "start": start}
            if since:
                query["since"] = since
            resp = self._get(path, query, {"If-Modified-Since-Version": str(since)} if since else None)
            if resp.status == 304:
                return None
            version = int(resp.header("last-modified-version") or version)
            try:
                page = json.loads(resp.body)
            except ValueError:
                raise ZoteroError("Zotero sent JSON that can't be read.") from None
            if not isinstance(page, list):
                raise ZoteroError("Zotero sent JSON that can't be read.")
            out.extend(o for o in page if isinstance(o, dict))
            total = resp.header("total-results")
            start += PAGE
            if len(out) >= limit:
                return version, out[:limit], False
            if not page or (total is not None and total.isdigit() and start >= int(total)) or len(page) < PAGE:
                return version, out, True

    def changes(self, library_type: str, library_id: str, collection: str | None, since: int) -> Changes | None:
        """What changed since the library version *since*; None if nothing did."""
        path = library_path(library_type, library_id, collection)
        got = self._all(f"{path}/items", {"include": "data,csljson", "itemType": SKIP_TYPES}, since)
        if got is None:
            return None
        version, items, complete = got
        base = library_path(library_type, library_id)
        cols = self._all(f"{base}/collections", {}, since, 5_000)
        changes = Changes(version, items, cols[1] if cols else [], complete=complete)
        if since:
            resp = self._get(f"{base}/deleted", {"since": since})
            try:
                data = json.loads(resp.body)
                changes.deleted_items = [k for k in data.get("items", []) if isinstance(k, str)]
                changes.deleted_collections = [k for k in data.get("collections", []) if isinstance(k, str)]
            except (ValueError, AttributeError):
                pass
        return changes

    def children(self, library_type: str, library_id: str, item_key: str) -> list[dict]:
        path = library_path(library_type, library_id) + f"/items/{quote(item_key, safe='')}/children"
        resp = self._get(path, {"format": "json", "include": "data", "itemType": SKIP_TYPES})
        try:
            data = json.loads(resp.body)
        except ValueError:
            raise ZoteroError("Zotero sent JSON that can't be read.") from None
        return [o for o in data if isinstance(o, dict)] if isinstance(data, list) else []

    def file(self, library_type: str, library_id: str, attachment_key: str) -> bytes:
        """The stored file of an attachment, from Zotero's file storage."""
        if self.local:
            raise ZoteroError("Zotero on a computer only hands out file:// links. Add the WebDAV folder Zotero"
                              " syncs files to, or open the file in Zotero.", 409)
        path = library_path(library_type, library_id) + f"/items/{quote(attachment_key, safe='')}/file"
        resp = self._get(path, {}, follow=False, max_bytes=MAX_FILE_BYTES, ok=(200, 301, 302, 303, 307))
        if resp.status == 200:
            return resp.body
        location = resp.header("location")
        if not location:
            raise ZoteroError("Zotero didn't say where the file is.")
        # The download URL is signed; the API key must not go along to the storage host.
        file_resp = self.policy.fetch("GET", urljoin(resp.url, location), timeout=120, max_bytes=MAX_FILE_BYTES)
        if file_resp.status != 200:
            raise ZoteroError(f"Zotero's file storage answered HTTP {file_resp.status}.", file_resp.status)
        return file_resp.body


def item_key_of(item: dict) -> str | None:
    """CSL-JSON ids from the Zotero API look like "<library id>/<item key>"."""
    raw = item.get("id")
    if not isinstance(raw, str) or not raw:
        return None
    return raw.rsplit("/", 1)[-1]


def pinned_key(item: dict) -> str | None:
    key = item.get("citation-key") or item.get("citationKey")
    if isinstance(key, str) and KEY_RE.match(key.strip()):
        return key.strip()
    for field_name in ("note", "extra"):
        text = item.get(field_name)
        if isinstance(text, str):
            m = re.search(r"^\s*Citation Key:\s*(\S+)\s*$", text, re.M | re.I)
            if m and KEY_RE.match(m.group(1)):
                return m.group(1)
    return None


def _ascii(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower())


def fold(text: str) -> str:
    """Lower case without accents, for search."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).lower()


def search_text(key: str, csl: dict) -> str:
    parts = [key]
    for role in ("author", "editor", "translator"):
        for person in csl.get(role) or []:
            if isinstance(person, dict):
                parts += [str(person.get(k, "")) for k in ("family", "given", "literal")]
    issued = csl.get("issued")
    if isinstance(issued, dict):
        dp = issued.get("date-parts")
        if isinstance(dp, list) and dp and isinstance(dp[0], list) and dp[0]:
            parts.append(str(dp[0][0]))
    for name in ("title", "title-short", "container-title", "publisher", "DOI", "ISBN"):
        if isinstance(csl.get(name), str):
            parts.append(csl[name])
    return fold(" ".join(p for p in parts if p))[:2000]


def generated_base(item: dict) -> str:
    name = ""
    for role in ("author", "editor"):
        people = item.get(role)
        if isinstance(people, list) and people and isinstance(people[0], dict):
            name = people[0].get("family") or people[0].get("literal") or ""
            break
    if not name and isinstance(item.get("title"), str):
        name = item["title"].split()[0] if item["title"].split() else ""
    year = ""
    issued = item.get("issued")
    if isinstance(issued, dict):
        parts = issued.get("date-parts")
        if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
            year = str(parts[0][0])
    return (_ascii(name)[:30] or "item") + (year if year.isdigit() else "nd")


def assign_keys(conn, source_id: int, items: dict[str, dict]) -> dict[str, str]:
    """item key -> citation key, storing new assignments so they never change."""
    rows = conn.execute("SELECT item_key, citation_key FROM citation_keys WHERE source_id = ?", (source_id,))
    assigned = {r["item_key"]: r["citation_key"] for r in rows}
    used = set(assigned.values())
    for item_key in sorted(items):
        if item_key in assigned:
            continue
        item = items[item_key]
        key = pinned_key(item)
        if key is None or key in used:
            base = generated_base(item)
            key = base
            n = 0
            while key in used:
                key = base + _suffix(n)
                n += 1
        assigned[item_key] = key
        used.add(key)
        conn.execute("INSERT INTO citation_keys(source_id, item_key, citation_key) VALUES (?, ?, ?)",
                     (source_id, item_key, key))
    return assigned


def _suffix(n: int) -> str:
    letters = "abcdefghijklmnopqrstuvwxyz"
    out = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        out = letters[r] + out
    return out


def attachment_row(obj: dict) -> dict | None:
    """The stored fields of a Zotero attachment object, or None for anything else."""
    data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
    if data.get("itemType") != "attachment":
        return None
    key = obj.get("key") or data.get("key")
    parent = data.get("parentItem")
    if not (isinstance(key, str) and ITEM_KEY_RE.match(key) and isinstance(parent, str) and parent):
        return None

    def text(name, limit=500):
        value = data.get(name)
        return value[:limit] if isinstance(value, str) else ""

    return {"attachment_key": key, "parent_key": parent, "title": text("title"),
            "content_type": text("contentType", 100), "filename": text("filename"),
            "link_mode": text("linkMode", 40), "url": text("url", 2000) or None, "md5": text("md5", 64) or None}


class Sources:
    """Refreshes Zotero sources and hands their CSL-JSON to renders and the editor."""

    def __init__(self, app):
        self.app = app
        self.base_url = API  # tests point this at a fake
        self._locks: dict[int, asyncio.Lock] = {}
        self._summaries: dict[int, tuple[int, list[dict]]] = {}  # source id -> (library version, summaries)

    # -- connections -----------------------------------------------------------
    def client(self, src) -> Client:
        config = json.loads(self.app.secrets.open(src["config_enc"], context_for("citation_sources", src["id"])))
        client = Client(self.app.net, config.get("api_key", ""), src["api_url"] or self.base_url)
        client.local = bool(src["api_url"])
        return client

    def storage(self, src) -> dict | None:
        """The WebDAV folder Zotero syncs this library's files to, if one is set."""
        if not src["storage_enc"]:
            return None
        try:
            return json.loads(self.app.secrets.open(src["storage_enc"], context_for("citation_storage", src["id"])))
        except (SecretError, ValueError):
            return None

    def seal_storage(self, source_id: int, config: dict) -> str:
        return self.app.secrets.seal(json.dumps(config), context_for("citation_storage", source_id))

    # -- refresh -----------------------------------------------------------------
    async def refresh(self, source_id: int) -> dict:
        lock = self._locks.setdefault(source_id, asyncio.Lock())
        async with lock:
            src = await self.app.db.fetchone("SELECT * FROM citation_sources WHERE id = ?", (source_id,))
            if src is None:
                return {}
            since = src["library_version"]
            try:
                client = self.client(src)
                args = (src["library_type"], src["library_id"], src["collection"])
                result = await asyncio.to_thread(client.changes, *args, since)
                if src["api_url"] and since and client.server_id != src["server_id"]:
                    # Another Zotero installation: its versions say nothing about ours.
                    since = 0
                    result = await asyncio.to_thread(client.changes, *args, 0)
            except (ZoteroError, FetchError, PolicyError, SecretError, ValueError, KeyError) as exc:
                await self.app.db.execute(
                    "UPDATE citation_sources SET status = 'error', last_error = ?, last_sync_at = ? WHERE id = ?",
                    (str(exc)[:300], time.time(), source_id))
                log.warning("zotero refresh failed", extra=fields(source=source_id, error=str(exc)))
                return {"error": str(exc)}
            if result is None:
                await self.app.db.execute("UPDATE citation_sources SET status = 'ok', last_error = NULL,"
                                          " last_sync_at = ? WHERE id = ?", (time.time(), source_id))
                return {"changed": 0, "deleted": 0}

            regular: dict[str, dict] = {}
            datas: dict[str, dict] = {}
            attachments: list[dict] = []
            for obj in result.items:
                data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
                key = obj.get("key") or data.get("key")
                if not isinstance(key, str) or not key:
                    continue
                if data.get("itemType") == "attachment":
                    row = attachment_row(obj)
                    if row:
                        attachments.append(row)
                    continue
                csl = obj.get("csljson")
                if not isinstance(csl, dict):
                    continue
                csl = dict(csl)
                native = data.get("citationKey")
                if isinstance(native, str) and native.strip() and not csl.get("citation-key"):
                    csl["citation-key"] = native.strip()
                regular[key] = csl
                datas[key] = data
            full = since == 0 and result.complete

            def work(c):
                keys = assign_keys(c, source_id, regular)
                for item_key, item in regular.items():
                    cite = keys[item_key]
                    csl = {**item, "id": cite}
                    csl.pop("citation-key", None)
                    cols = [k for k in datas[item_key].get("collections") or [] if isinstance(k, str)]
                    added = datas[item_key].get("dateAdded")
                    c.execute("INSERT INTO citation_items(source_id, item_key, csl_json, collections, added_at,"
                              " search_text) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(source_id, item_key) DO UPDATE"
                              " SET csl_json = excluded.csl_json, collections = excluded.collections,"
                              " added_at = excluded.added_at, search_text = excluded.search_text",
                              (source_id, item_key, json.dumps(csl, ensure_ascii=False), json.dumps(cols),
                               added if isinstance(added, str) else None, search_text(cite, csl)))
                for row in attachments:
                    c.execute("INSERT OR REPLACE INTO citation_attachments(source_id, attachment_key, parent_key,"
                              " title, content_type, filename, link_mode, url, md5) VALUES (:sid, :attachment_key,"
                              " :parent_key, :title, :content_type, :filename, :link_mode, :url, :md5)",
                              {"sid": source_id, **row})
                seen_cols = set()
                for col in result.collections:
                    data = col.get("data") if isinstance(col.get("data"), dict) else col
                    key, name = col.get("key") or data.get("key"), data.get("name")
                    if not (isinstance(key, str) and isinstance(name, str)):
                        continue
                    parent = data.get("parentCollection")
                    seen_cols.add(key)
                    c.execute("INSERT OR REPLACE INTO citation_collections(source_id, collection_key, name,"
                              " parent_key) VALUES (?, ?, ?, ?)",
                              (source_id, key, name[:300], parent if isinstance(parent, str) and parent else None))
                for key in result.deleted_items:
                    c.execute("DELETE FROM citation_items WHERE source_id = ? AND item_key = ?", (source_id, key))
                    c.execute("DELETE FROM citation_attachments WHERE source_id = ? AND (attachment_key = ?"
                              " OR parent_key = ?)", (source_id, key, key))
                for key in result.deleted_collections:
                    c.execute("DELETE FROM citation_collections WHERE source_id = ? AND collection_key = ?",
                              (source_id, key))
                if full:
                    # A complete listing: whatever it didn't name is gone.
                    for table, column, keep in (("citation_items", "item_key", set(regular)),
                                                ("citation_attachments", "attachment_key",
                                                 {a["attachment_key"] for a in attachments}),
                                                ("citation_collections", "collection_key", seen_cols)):
                        stored = {r[0] for r in c.execute(f"SELECT {column} FROM {table} WHERE source_id = ?",
                                                          (source_id,))}
                        for key in stored - keep:
                            c.execute(f"DELETE FROM {table} WHERE source_id = ? AND {column} = ?", (source_id, key))
                c.execute("UPDATE citation_sources SET library_version = ?, server_id = ?, status = 'ok',"
                          " last_error = NULL, last_sync_at = ? WHERE id = ?",
                          (result.version, client.server_id, time.time(), source_id))

            await self.app.db.write(work)
            self._summaries.pop(source_id, None)
            return {"changed": len(regular), "deleted": len(result.deleted_items)}

    async def run_forever(self) -> None:
        while True:
            await asyncio.sleep(60)
            rows = await self.app.db.fetchall(
                "SELECT id FROM citation_sources WHERE last_sync_at IS NULL OR last_sync_at < ?",
                (time.time() - REFRESH_SECONDS,))
            for r in rows:
                try:
                    await self.refresh(r["id"])
                except Exception:  # noqa: BLE001
                    log.exception("zotero refresh crashed", extra=fields(source=r["id"]))

    async def csl_for_project(self, project_id: str, names: set[str]) -> dict[str, str]:
        """name -> CSL-JSON text for the zotero:<name> sources a project's documents use.

        A name resolves to a source of the project's owner first, then of its editors.
        """
        if not names:
            return {}

        def load(c):
            out = {}
            for name, sid in project_sources(c, project_id, names).items():
                rows = c.execute("SELECT csl_json FROM citation_items WHERE source_id = ? ORDER BY item_key",
                                 (sid,)).fetchall()
                out[name] = "[" + ",\n".join(r["csl_json"] for r in rows) + "]"
            return out

        return await self.app.db.read(load)

    async def summaries_for_project(self, project_id: str, names: set[str]) -> dict[str, list[dict]]:
        """name -> editor summaries of the items in each zotero:<name> source a project uses."""
        if not names:
            return {}
        resolved = await self.app.db.read(lambda c: {
            name: c.execute("SELECT id, name, owner_id, library_version, library_type, library_id"
                            " FROM citation_sources WHERE id = ?", (sid,)).fetchone()
            for name, sid in project_sources(c, project_id, names).items()})
        out = {}
        for name, src in resolved.items():
            cached = self._summaries.get(src["id"])
            if cached and cached[0] == src["library_version"]:
                out[name] = cached[1]
                continue
            rows = await self.app.db.fetchall(
                "SELECT i.item_key, i.csl_json, EXISTS(SELECT 1 FROM citation_attachments a WHERE a.source_id ="
                " i.source_id AND a.parent_key = i.item_key AND a.content_type = 'application/pdf') AS has_pdf"
                " FROM citation_items i WHERE i.source_id = ?", (src["id"],))
            items = [s for r in rows if (s := item_summary(src, r)) is not None]
            self._summaries[src["id"]] = (src["library_version"], items)
            out[name] = items
        return out


def project_sources(c, project_id: str, names) -> dict[str, int]:
    """zotero:<name> -> source id, from the sources of the project's owner first, then its editors."""
    out = {}
    for name in sorted(names):
        src = c.execute(
            "SELECT s.id FROM citation_sources s JOIN project_members m ON m.user_id = s.owner_id"
            " WHERE m.project_id = ? AND s.name = ? AND m.role IN ('owner', 'editor')"
            " ORDER BY m.role = 'owner' DESC, s.id LIMIT 1", (project_id, name)).fetchone()
        if src is not None:
            out[name] = src["id"]
    return out


def item_summary(src, row) -> dict | None:
    """The editor's summary of a stored Zotero item, with where it came from."""
    from .library import label, summarize

    try:
        csl = json.loads(row["csl_json"])
    except ValueError:
        return None
    s = summarize(csl)
    if s is None:
        return None
    s["label"] = label(s)
    s["source"] = f"zotero:{src['name']}"
    s["zotero"] = {"source": src["name"], "item": row["item_key"], "pdf": bool(row["has_pdf"])}
    return s


FRONT_MATTER = re.compile(r"\A---[ \t]*\n(.*?)\n(?:---|\.\.\.)[ \t]*(?:\n|\Z)", re.S)
ZOTERO_REF = re.compile(r"zotero:([A-Za-z0-9_-]{1,40})")


def referenced_sources(texts: dict[str, str]) -> set[str]:
    """Names in zotero:<name> bibliography entries of the Markdown documents' front matter."""
    names: set[str] = set()
    for path, text in texts.items():
        if not path.endswith((".md", ".markdown")):
            continue
        m = FRONT_MATTER.match(text)
        if m:
            names.update(ZOTERO_REF.findall(m.group(1)))
    return names

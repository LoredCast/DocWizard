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
from urllib.parse import quote, urlencode

from ..logs import fields
from ..netpolicy import FetchError, PolicyError
from ..secretbox import SecretError, context_for

log = logging.getLogger("docwizard.zotero")

API = "https://api.zotero.org"
PAGE = 100
MAX_ITEMS = 20_000
REFRESH_SECONDS = 15 * 60
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
KEY_RE = re.compile(r"^[A-Za-z0-9_:.#$%&+?<>~/-]{1,100}$")


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


class Client:
    """Blocking: call from a thread."""

    def __init__(self, policy, api_key: str, base: str = API):
        self.policy = policy
        self.base = base.rstrip("/")
        self.headers = {"Zotero-API-Version": "3", "Zotero-API-Key": api_key}

    def _get(self, path: str, params: dict, extra: dict | None = None):
        url = f"{self.base}{path}?{urlencode(params)}"
        resp = self.policy.fetch("GET", url, headers={**self.headers, **(extra or {})}, timeout=30,
                                 max_bytes=50_000_000)
        if resp.status in (403, 401):
            raise ZoteroError("Zotero didn't accept the API key for this library.", resp.status)
        if resp.status == 404:
            raise ZoteroError("Zotero doesn't know this library or collection.", 404)
        if resp.status == 429 or resp.status >= 500:
            raise ZoteroError(f"Zotero is busy (HTTP {resp.status}). Trying again later.", resp.status)
        if resp.status not in (200, 304):
            raise ZoteroError(f"Zotero answered HTTP {resp.status}.", resp.status)
        return resp

    def changes(self, library_type: str, library_id: str, collection: str | None, since: int):
        """(new library version, changed CSL items, deleted item keys); None if nothing changed."""
        path = library_path(library_type, library_id, collection)
        items: list[dict] = []
        start = 0
        version = since
        while True:
            params = {"format": "csljson", "limit": PAGE, "start": start, "itemType": "-attachment || note"}
            if since:
                params["since"] = since
            resp = self._get(f"{path}/items", params, {"If-Modified-Since-Version": str(since)} if since else None)
            if resp.status == 304:
                return None
            version = int(resp.header("last-modified-version") or version)
            try:
                page = json.loads(resp.body).get("items", [])
            except (ValueError, AttributeError):
                raise ZoteroError("Zotero sent CSL-JSON that can't be read.") from None
            items.extend(i for i in page if isinstance(i, dict))
            total = int(resp.header("total-results") or 0)
            start += PAGE
            if not page or start >= total or len(items) >= MAX_ITEMS:
                break
        deleted: list[str] = []
        if since:
            base = library_path(library_type, library_id)
            resp = self._get(f"{base}/deleted", {"since": since})
            try:
                deleted = [k for k in json.loads(resp.body).get("items", []) if isinstance(k, str)]
            except (ValueError, AttributeError):
                deleted = []
        return version, items, deleted


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
    for field in ("note", "extra"):
        text = item.get(field)
        if isinstance(text, str):
            m = re.search(r"^\s*Citation Key:\s*(\S+)\s*$", text, re.M | re.I)
            if m and KEY_RE.match(m.group(1)):
                return m.group(1)
    return None


def _ascii(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower())


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


class Sources:
    """Refreshes Zotero sources and hands their CSL-JSON to renders and the editor."""

    def __init__(self, app):
        self.app = app
        self.base_url = API  # tests point this at a fake
        self._locks: dict[int, asyncio.Lock] = {}

    async def refresh(self, source_id: int) -> dict:
        lock = self._locks.setdefault(source_id, asyncio.Lock())
        async with lock:
            src = await self.app.db.fetchone("SELECT * FROM citation_sources WHERE id = ?", (source_id,))
            if src is None:
                return {}
            try:
                config = json.loads(self.app.secrets.open(src["config_enc"], context_for("citation_sources",
                                                                                         source_id)))
                client = Client(self.app.net, config["api_key"], self.base_url)
                result = await asyncio.to_thread(client.changes, src["library_type"], src["library_id"],
                                                 src["collection"], src["library_version"])
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
            version, items, deleted = result
            changed = {k: i for i in items if (k := item_key_of(i))}

            def work(c):
                keys = assign_keys(c, source_id, changed)
                for item_key, item in changed.items():
                    csl = {**item, "id": keys[item_key]}
                    csl.pop("citation-key", None)
                    c.execute("INSERT INTO citation_items(source_id, item_key, csl_json) VALUES (?, ?, ?)"
                              " ON CONFLICT(source_id, item_key) DO UPDATE SET csl_json = excluded.csl_json",
                              (source_id, item_key, json.dumps(csl, ensure_ascii=False)))
                for item_key in deleted:
                    c.execute("DELETE FROM citation_items WHERE source_id = ? AND item_key = ?", (source_id, item_key))
                c.execute("UPDATE citation_sources SET library_version = ?, status = 'ok', last_error = NULL,"
                          " last_sync_at = ? WHERE id = ?", (version, time.time(), source_id))

            await self.app.db.write(work)
            return {"changed": len(changed), "deleted": len(deleted)}

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
            for name in sorted(names):
                src = c.execute(
                    "SELECT s.id FROM citation_sources s JOIN project_members m ON m.user_id = s.owner_id"
                    " WHERE m.project_id = ? AND s.name = ? AND m.role IN ('owner', 'editor')"
                    " ORDER BY m.role = 'owner' DESC, s.id LIMIT 1", (project_id, name)).fetchone()
                if src is None:
                    continue
                rows = c.execute("SELECT csl_json FROM citation_items WHERE source_id = ? ORDER BY item_key",
                                 (src["id"],)).fetchall()
                out[name] = "[" + ",\n".join(r["csl_json"] for r in rows) + "]"
            return out

        return await self.app.db.read(load)


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

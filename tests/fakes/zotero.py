"""A fake Zotero API (v3) for the parts DocWizard uses.

Items as Zotero JSON with ``include=data,csljson``, ``since``, deleted objects, collections,
child attachments and attachment files (redirected to a signed storage URL, like
api.zotero.org). With ``local=True`` it answers like Zotero desktop's local API: user 0,
no API key, a Zotero-Server-ID and no file downloads.
"""

from __future__ import annotations

import http.server
import json
import threading
from urllib.parse import parse_qs, urlsplit


class FakeZotero:
    def __init__(self, user_id="12345", api_key="k" * 24, *, local=False, server_id="srv1"):
        self.user_id = "0" if local else user_id
        self.api_key = None if local else api_key
        self.local = local
        self.server_id = server_id
        self.version = 1
        self.items: dict[str, tuple[int, dict]] = {}        # key -> (version, Zotero JSON object)
        self.collections: dict[str, tuple[int, dict]] = {}
        self.files: dict[str, bytes] = {}
        self.deleted: dict[str, int] = {}
        self.deleted_collections: dict[str, int] = {}
        self.requests: list[str] = []
        self.storage_headers: list[dict] = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://zotero.test:{self.port}" + ("/api" if self.local else "")

    def stop(self):
        self.server.shutdown()

    def put(self, key: str, csl: dict, *, collections=(), added="2024-01-01T00:00:00Z", **data) -> None:
        self.version += 1
        obj = {"key": key, "version": self.version,
               "data": {"key": key, "itemType": csl.get("type", "book"), "collections": list(collections),
                        "dateAdded": added, **data},
               "csljson": {**csl, "id": f"{self.user_id}/{key}"}}
        self.items[key] = (self.version, obj)

    def put_attachment(self, key: str, parent: str, filename: str = "paper.pdf", content: bytes | None = None,
                       content_type: str = "application/pdf", link_mode: str = "imported_file",
                       md5: str | None = "0" * 32) -> None:
        self.version += 1
        obj = {"key": key, "version": self.version,
               "data": {"key": key, "itemType": "attachment", "parentItem": parent, "title": "Full Text PDF",
                        "contentType": content_type, "filename": filename, "linkMode": link_mode, "md5": md5}}
        self.items[key] = (self.version, obj)
        if content is not None:
            self.files[key] = content

    def put_collection(self, key: str, name: str, parent: str | None = None) -> None:
        self.version += 1
        self.collections[key] = (self.version, {"key": key, "version": self.version,
                                                "data": {"key": key, "name": name,
                                                         "parentCollection": parent or False}})

    def delete(self, key: str) -> None:
        self.version += 1
        self.items.pop(key, None)
        self.deleted[key] = self.version

    def _handler(self):
        z = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, data=None, headers=None, raw: bytes | None = None):
                body = raw if raw is not None else (json.dumps(data).encode() if data is not None else b"")
                self.send_response(status)
                if z.local:
                    self.send_header("Zotero-Server-ID", z.server_id)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def page(self, rows, q):
                start, limit = int(q.get("start", 0)), int(q.get("limit", 25))
                return self.reply(200, rows[start:start + limit],
                                  {"Last-Modified-Version": str(z.version), "Total-Results": str(len(rows))})

            def do_GET(self):
                z.requests.append(self.path)
                url = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                if url.path.startswith("/storage/"):
                    z.storage_headers.append(dict(self.headers))
                    key = url.path.rsplit("/", 1)[-1]
                    if q.get("sig") != "ok" or key not in z.files:
                        return self.reply(403, {"error": "bad signature"})
                    return self.reply(200, raw=z.files[key], headers={"Content-Type": "application/pdf"})
                if z.local:
                    if not url.path.startswith("/api/"):
                        return self.reply(404, {"error": "not found"})
                    path = url.path[4:]
                elif self.headers.get("Zotero-API-Key") != z.api_key:
                    return self.reply(403, {"error": "forbidden"})
                else:
                    path = url.path
                base = f"/users/{z.user_id}"
                since = int(q.get("since", 0))
                if path == f"{base}/items":
                    if int(self.headers.get("If-Modified-Since-Version", "0") or 0) >= z.version:
                        return self.reply(304, headers={"Last-Modified-Version": str(z.version)})
                    rows = [o for v, o in sorted(z.items.values(), key=lambda t: t[1]["key"]) if v > since]
                    return self.page(rows, q)
                if path == f"{base}/collections":
                    rows = [o for v, o in sorted(z.collections.values(), key=lambda t: t[1]["key"]) if v > since]
                    return self.page(rows, q)
                if path.startswith(f"{base}/items/") and path.endswith("/children"):
                    parent = path.split("/")[-2]
                    rows = [o for _, o in z.items.values() if o["data"].get("parentItem") == parent]
                    return self.reply(200, rows, {"Last-Modified-Version": str(z.version)})
                if path.startswith(f"{base}/items/") and path.endswith("/file"):
                    key = path.split("/")[-2]
                    if z.local:
                        return self.reply(302, headers={"Location": f"file:///home/me/Zotero/storage/{key}/a.pdf"})
                    if key not in z.files:
                        return self.reply(404, {"error": "no file"})
                    return self.reply(302, headers={"Location": f"http://storage.test:{z.port}/storage/{key}?sig=ok"})
                if path == f"{base}/deleted":
                    keys = [k for k, v in z.deleted.items() if v > since]
                    cols = [k for k, v in z.deleted_collections.items() if v > since]
                    return self.reply(200, {"items": keys, "collections": cols},
                                      {"Last-Modified-Version": str(z.version)})
                return self.reply(404, {"error": "not found"})

        return Handler

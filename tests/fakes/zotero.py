"""A fake Zotero Web API (v3) for the parts DocWizard uses: CSL-JSON items, since, deleted."""

from __future__ import annotations

import http.server
import json
import threading
from urllib.parse import parse_qs, urlsplit


class FakeZotero:
    def __init__(self, user_id="12345", api_key="k" * 24):
        self.user_id = user_id
        self.api_key = api_key
        self.version = 1
        self.items: dict[str, tuple[int, dict]] = {}   # key -> (version, csl)
        self.deleted: dict[str, int] = {}
        self.requests: list[str] = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://zotero.test:{self.port}"

    def stop(self):
        self.server.shutdown()

    def put(self, key: str, csl: dict) -> None:
        self.version += 1
        self.items[key] = (self.version, {**csl, "id": f"{self.user_id}/{key}"})

    def delete(self, key: str) -> None:
        self.version += 1
        self.items.pop(key, None)
        self.deleted[key] = self.version

    def _handler(self):
        z = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, data=None, headers=None):
                body = json.dumps(data).encode() if data is not None else b""
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                z.requests.append(self.path)
                url = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                if self.headers.get("Zotero-API-Key") != z.api_key:
                    return self.reply(403, {"error": "forbidden"})
                base = f"/users/{z.user_id}"
                since = int(q.get("since", 0))
                if url.path == f"{base}/items":
                    if int(self.headers.get("If-Modified-Since-Version", "0") or 0) >= z.version:
                        return self.reply(304, headers={"Last-Modified-Version": str(z.version)})
                    rows = [csl for v, csl in sorted(z.items.values(), key=lambda t: t[1]["id"]) if v > since]
                    start, limit = int(q.get("start", 0)), int(q.get("limit", 25))
                    return self.reply(200, {"items": rows[start:start + limit]},
                                      {"Last-Modified-Version": str(z.version), "Total-Results": str(len(rows))})
                if url.path == f"{base}/deleted":
                    keys = [k for k, v in z.deleted.items() if v > since]
                    return self.reply(200, {"items": keys, "collections": []},
                                      {"Last-Modified-Version": str(z.version)})
                return self.reply(404, {"error": "not found"})

        return Handler

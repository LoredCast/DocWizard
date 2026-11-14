"""An in-memory WebDAV server that behaves like Nextcloud for the parts sync uses.

Files and folders live under /remote.php/dav/files/<user>/. Every change gives the
file a new ETag and a stable oc:fileid, and changes the ETags of every folder above
it. Login Flow v2 is at /index.php/login/v2.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import itertools
import json
import threading
from urllib.parse import parse_qs, quote, unquote, urlsplit
from xml.sax.saxutils import escape

PREFIX = "/remote.php/dav/files/"


class FakeDav:
    def __init__(self, user="jane", password="app-password"):
        self.user = user
        self.password = password
        self.files: dict[str, dict] = {}    # path -> {data, etag, fileid}
        self.dirs: dict[str, dict] = {"": {"etag": "d0", "fileid": "1"}}
        self.ids = itertools.count(100)
        self.etags = itertools.count(1)
        self.lock = threading.Lock()
        self.requests: list[tuple[str, str]] = []
        self.flow_granted = False
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    # -- test helpers -----------------------------------------------------
    @property
    def origin(self) -> str:
        return f"http://cloud.test:{self.port}"

    @property
    def root(self) -> str:
        return f"{self.origin}{PREFIX}{self.user}"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()

    def put(self, path: str, data: bytes | str, fileid: str | None = None) -> None:
        with self.lock:
            self._put(path, data.encode() if isinstance(data, str) else data, fileid)

    def read(self, path: str) -> bytes:
        return self.files[path]["data"]

    def remove(self, path: str) -> None:
        with self.lock:
            del self.files[path]
            self._touch_parents(path)

    def rename(self, src: str, dest: str) -> None:
        with self.lock:
            self.files[dest] = self.files.pop(src)
            self._ensure_dirs(dest)
            self._touch_parents(src)
            self._touch_parents(dest)

    # -- storage -------------------------------------------------------------
    def _ensure_dirs(self, path: str) -> None:
        parts = path.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            d = "/".join(parts[:i]) + "/"
            self.dirs.setdefault(d, {"etag": f"d{next(self.etags)}", "fileid": str(next(self.ids))})

    def _touch_parents(self, path: str) -> None:
        parts = path.rstrip("/").split("/")[:-1]
        self.dirs[""]["etag"] = f"d{next(self.etags)}"
        for i in range(1, len(parts) + 1):
            d = "/".join(parts[:i]) + "/"
            if d in self.dirs:
                self.dirs[d]["etag"] = f"d{next(self.etags)}"

    def _put(self, path: str, data: bytes, fileid: str | None = None) -> str:
        self._ensure_dirs(path)
        old = self.files.get(path)
        etag = f'"{hashlib.sha1(data).hexdigest()[:8]}-{next(self.etags)}"'
        self.files[path] = {"data": data, "etag": etag,
                            "fileid": fileid or (old["fileid"] if old else str(next(self.ids)))}
        self._touch_parents(path)
        return etag

    # -- HTTP ----------------------------------------------------------------
    def _handler(self):
        dav = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def reply(self, status, body=b"", headers=None):
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def body(self) -> bytes:
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def rel(self) -> str | None:
                path = unquote(urlsplit(self.path).path)
                base = PREFIX + dav.user
                if path == base or path == base + "/":
                    return ""
                if not path.startswith(base + "/"):
                    return None
                return path[len(base) + 1:]

            def authorized(self) -> bool:
                token = base64.b64encode(f"{dav.user}:{dav.password}".encode()).decode()
                return self.headers.get("Authorization") == f"Basic {token}"

            def handle_one(self, method):
                dav.requests.append((method, self.path))
                if self.path.startswith("/index.php/login/v2"):
                    return self.login_flow(method)
                rel = self.rel()
                if rel is None:
                    return self.reply(404)
                if not self.authorized():
                    return self.reply(401, headers={"WWW-Authenticate": 'Basic realm="dav"'})
                with dav.lock:
                    getattr(self, "dav_" + method.lower())(rel)

            def login_flow(self, method):
                if self.path == "/index.php/login/v2" and method == "POST":
                    data = {"poll": {"token": "tok", "endpoint": f"{dav.origin}/index.php/login/v2/poll"},
                            "login": f"{dav.origin}/index.php/login/v2/flow/abc"}
                    return self.reply(200, json.dumps(data).encode(), {"Content-Type": "application/json"})
                if self.path == "/index.php/login/v2/poll" and method == "POST":
                    form = parse_qs(self.body().decode())
                    if form.get("token") != ["tok"] or not dav.flow_granted:
                        return self.reply(404)
                    data = {"server": dav.origin, "loginName": dav.user, "appPassword": dav.password}
                    return self.reply(200, json.dumps(data).encode(), {"Content-Type": "application/json"})
                return self.reply(404)

            def dav_propfind(self, rel):
                self.body()
                folder = rel.rstrip("/") + "/" if rel else ""
                if folder not in dav.dirs:
                    return self.reply(404)
                entries = [(folder, None)]
                for path, f in dav.files.items():
                    if path.startswith(folder) and "/" not in path[len(folder):]:
                        entries.append((path, f))
                for d in dav.dirs:
                    if d != folder and d.startswith(folder) and "/" not in d[len(folder):].rstrip("/"):
                        entries.append((d, None))
                parts = ['<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">']
                for path, f in entries:
                    href = escape(quote(f"{PREFIX}{dav.user}/{path}"))
                    if f is None:
                        meta = dav.dirs[path]
                        props = (f"<d:resourcetype><d:collection/></d:resourcetype><d:getetag>\"{meta['etag']}\""
                                 f"</d:getetag><oc:fileid>{meta['fileid']}</oc:fileid>")
                    else:
                        props = (f"<d:resourcetype/><d:getetag>{f['etag']}</d:getetag>"
                                 f"<d:getcontentlength>{len(f['data'])}</d:getcontentlength>"
                                 f"<oc:fileid>{f['fileid']}</oc:fileid>")
                    parts.append(f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{props}</d:prop>"
                                 "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")
                parts.append("</d:multistatus>")
                self.reply(207, "".join(parts).encode(), {"Content-Type": "application/xml"})

            def dav_get(self, rel):
                f = dav.files.get(rel)
                if f is None:
                    return self.reply(404)
                self.reply(200, f["data"], {"ETag": f["etag"]})

            def dav_put(self, rel):
                data = self.body()
                f = dav.files.get(rel)
                if self.headers.get("If-None-Match") == "*" and f is not None:
                    return self.reply(412)
                match = self.headers.get("If-Match")
                if match and (f is None or f["etag"] != match):
                    return self.reply(412)
                parent = rel.rsplit("/", 1)[0] + "/" if "/" in rel else ""
                if parent not in dav.dirs:
                    return self.reply(409)
                etag = dav._put(rel, data)
                self.reply(201 if f is None else 204, headers={"ETag": etag, "OC-ETag": etag})

            def dav_delete(self, rel):
                f = dav.files.get(rel)
                if f is None:
                    return self.reply(404)
                match = self.headers.get("If-Match")
                if match and f["etag"] != match:
                    return self.reply(412)
                del dav.files[rel]
                dav._touch_parents(rel)
                self.reply(204)

            def dav_mkcol(self, rel):
                d = rel.rstrip("/") + "/"
                if d in dav.dirs:
                    return self.reply(405)
                parent = d.rstrip("/").rsplit("/", 1)[0] + "/" if "/" in d.rstrip("/") else ""
                if parent not in dav.dirs:
                    return self.reply(409)
                dav.dirs[d] = {"etag": f"d{next(dav.etags)}", "fileid": str(next(dav.ids))}
                dav._touch_parents(d)
                self.reply(201)

            def dav_move(self, rel):
                dest = unquote(urlsplit(self.headers["Destination"]).path)[len(PREFIX + dav.user) + 1:]
                if rel not in dav.files:
                    return self.reply(404)
                if dest in dav.files and self.headers.get("Overwrite") == "F":
                    return self.reply(412)
                dav.files[dest] = dav.files.pop(rel)
                dav._touch_parents(rel)
                dav._touch_parents(dest)
                self.reply(201)

            def do_PROPFIND(self): self.handle_one("PROPFIND")  # noqa: E704
            def do_GET(self): self.handle_one("GET")  # noqa: E704
            def do_PUT(self): self.handle_one("PUT")  # noqa: E704
            def do_DELETE(self): self.handle_one("DELETE")  # noqa: E704
            def do_MKCOL(self): self.handle_one("MKCOL")  # noqa: E704
            def do_MOVE(self): self.handle_one("MOVE")  # noqa: E704
            def do_POST(self): self.handle_one("POST")  # noqa: E704

        return Handler

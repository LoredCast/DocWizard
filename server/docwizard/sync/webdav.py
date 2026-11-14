"""A small WebDAV client (§12): PROPFIND, GET, PUT, DELETE, MKCOL and MOVE.

Every request goes through the outbound request policy. Blocking: call from a thread.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

from defusedxml import ElementTree

from ..netpolicy import Policy

PROPFIND_BODY = (b'<?xml version="1.0"?>'
                 b'<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"><d:prop>'
                 b'<d:resourcetype/><d:getetag/><d:getcontentlength/><d:getlastmodified/><oc:fileid/>'
                 b'</d:prop></d:propfind>')
DAV = "{DAV:}"
OC = "{http://owncloud.org/ns}"


class DavError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


@dataclass
class Entry:
    path: str          # relative to the client's root; folders end in "/"; the root is ""
    is_dir: bool
    etag: str | None
    size: int
    fileid: str | None


class WebDav:
    def __init__(self, policy: Policy, root_url: str, username: str = "", password: str = "",
                 timeout: float = 30.0, max_bytes: int = 50_000_000):
        self.policy = policy
        self.root = root_url.rstrip("/") + "/"
        self.root_path = unquote(urlsplit(self.root).path)
        self.headers = {}
        if username or password:
            token = base64.b64encode(f"{username}:{password}".encode()).decode()
            self.headers["Authorization"] = f"Basic {token}"
        self.timeout = timeout
        self.max_bytes = max_bytes

    def url(self, path: str) -> str:
        return self.root + quote(path.lstrip("/"), safe="/")

    def _request(self, method: str, path: str, *, headers=None, body=None, ok=(200, 201, 204)):
        resp = self.policy.fetch(method, self.url(path), headers={**self.headers, **(headers or {})}, body=body,
                                 timeout=self.timeout, max_bytes=self.max_bytes, follow_redirects=False)
        if resp.status not in ok:
            raise DavError(f"{method} {path or '/'}: HTTP {resp.status}", resp.status)
        return resp

    # -- reading ---------------------------------------------------------
    def propfind(self, path: str = "") -> list[Entry]:
        """The folder at *path* and its direct children (Depth: 1)."""
        if path and not path.endswith("/"):
            path += "/"
        resp = self._request("PROPFIND", path, headers={"Depth": "1", "Content-Type": "application/xml"},
                             body=PROPFIND_BODY, ok=(207,))
        try:
            tree = ElementTree.fromstring(resp.body)
        except Exception as exc:  # defusedxml raises its own errors for entity tricks
            raise DavError(f"The server sent XML that can't be read: {exc}") from exc
        out = []
        for r in tree.iter(f"{DAV}response"):
            href = r.findtext(f"{DAV}href") or ""
            rel = self._relative(href)
            if rel is None:
                continue
            props = {}
            for ps in r.iter(f"{DAV}propstat"):
                status = ps.findtext(f"{DAV}status") or ""
                if " 200 " not in status + " ":
                    continue
                prop = ps.find(f"{DAV}prop")
                if prop is not None:
                    props.update({child.tag: child for child in prop})
            rtype = props.get(f"{DAV}resourcetype")
            is_dir = rtype is not None and rtype.find(f"{DAV}collection") is not None
            if is_dir and rel and not rel.endswith("/"):
                rel += "/"
            etag = props[f"{DAV}getetag"].text if f"{DAV}getetag" in props else None
            size_text = props[f"{DAV}getcontentlength"].text if f"{DAV}getcontentlength" in props else None
            fileid = props[f"{OC}fileid"].text if f"{OC}fileid" in props else None
            out.append(Entry(rel, is_dir, etag.strip() if etag else None,
                             int(size_text) if size_text and size_text.strip().isdigit() else 0,
                             fileid.strip() if fileid else None))
        return out

    def _relative(self, href: str) -> str | None:
        path = unquote(urlsplit(href).path)
        if not path.startswith(self.root_path):
            if path + "/" == self.root_path:
                return ""
            return None
        return path[len(self.root_path):]

    def get(self, path: str) -> tuple[bytes, str | None]:
        resp = self._request("GET", path, ok=(200,))
        return resp.body, resp.header("etag")

    # -- writing ---------------------------------------------------------
    def put(self, path: str, data: bytes, *, if_match: str | None = None, if_none_match: bool = False) -> str | None:
        """Upload; returns the new ETag when the server sends one. Raises DavError(412) on a lost race."""
        headers = {"Content-Type": "application/octet-stream"}
        if if_match:
            headers["If-Match"] = if_match
        if if_none_match:
            headers["If-None-Match"] = "*"
        resp = self._request("PUT", path, headers=headers, body=data, ok=(200, 201, 204))
        return resp.header("etag") or resp.header("oc-etag")

    def delete(self, path: str, *, if_match: str | None = None) -> None:
        headers = {"If-Match": if_match} if if_match else {}
        self._request("DELETE", path, headers=headers, ok=(200, 204, 404))

    def mkcol(self, path: str) -> None:
        self._request("MKCOL", path.rstrip("/") + "/", ok=(201, 405))  # 405: it exists

    def makedirs(self, path: str) -> None:
        """MKCOL every folder above *path* (a file path)."""
        parts = path.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            self.mkcol("/".join(parts[:i]))

    def move(self, src: str, dest: str) -> None:
        self._request("MOVE", src, headers={"Destination": self.url(dest), "Overwrite": "F"}, ok=(201, 204))

    def walk(self, known_dir_etags: dict[str, str] | None = None) -> tuple[dict[str, Entry], dict[str, Entry], set[str]]:
        """List the tree below the root.

        Returns (files, folders, skipped): folders whose ETag equals the one in
        *known_dir_etags* are not listed again (Nextcloud changes folder ETags when
        anything below changes), and their paths are returned in *skipped*.
        """
        known = known_dir_etags or {}
        files: dict[str, Entry] = {}
        folders: dict[str, Entry] = {}
        skipped: set[str] = set()
        queue = [""]
        while queue:
            folder = queue.pop()
            for e in self.propfind(folder):
                if e.path == folder:
                    folders[folder] = e
                    continue
                if e.is_dir:
                    folders[e.path] = e
                    if e.etag and known.get(e.path) == e.etag:
                        skipped.add(e.path)
                    else:
                        queue.append(e.path)
                else:
                    files[e.path] = e
        return files, folders, skipped

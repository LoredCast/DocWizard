"""Files attached to Zotero items (§10.4): PDFs to read and find a citation's page in.

A file comes from Zotero's own file storage through the Web API, or from the WebDAV
folder that Zotero syncs files to (``<url>/zotero/<attachment key>.zip``, one zip per
attachment). Zotero desktop's local API only hands out ``file://`` links, so files of
a local library need the WebDAV folder. Downloaded files are kept in a size-limited
cache, keyed by the attachment's MD5 when Zotero reports one.
"""

from __future__ import annotations

import asyncio
import io
import os
import time
import zipfile
from pathlib import Path

from ..netpolicy import FetchError, PolicyError
from ..sync.webdav import DavError, WebDav
from .zotero import MAX_FILE_BYTES, ZoteroError

CACHE_BYTES = 1_000_000_000
UNVERSIONED_SECONDS = 3600  # files without an MD5 are fetched again after this long


def storage_root(url: str) -> str:
    """The folder that holds the zips. Zotero's setting names the folder above ``zotero/``."""
    url = url.strip().rstrip("/")
    return url if url.endswith("/zotero") else url + "/zotero"


def unzip_attachment(data: bytes, filename: str) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            files = [i for i in zf.infolist() if not i.is_dir()]
            pick = (next((i for i in files if i.filename == filename), None)
                    or next((i for i in files if i.filename.lower().endswith(".pdf")), None)
                    or (files[0] if files else None))
            if pick is None:
                raise ZoteroError("The file's zip on the WebDAV server is empty.")
            if pick.file_size > MAX_FILE_BYTES:
                raise ZoteroError("The file is too large to open here.")
            return zf.read(pick)
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        raise ZoteroError(f"The file's zip on the WebDAV server can't be read: {exc}") from None


def check_storage(policy, url: str, username: str, password: str) -> None:
    """Raise ZoteroError unless the WebDAV folder answers with these credentials."""
    dav = WebDav(policy, storage_root(url), username, password, timeout=15, max_bytes=2_000_000)
    try:
        dav._request("PROPFIND", "", headers={"Depth": "0", "Content-Type": "application/xml"},
                            body=b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/>'
                                 b"</d:prop></d:propfind>", ok=(207,))
    except DavError as exc:
        if exc.status in (401, 403):
            raise ZoteroError("The WebDAV server didn't accept these credentials.") from None
        if exc.status == 404:
            raise ZoteroError("There is no zotero folder at this address. Enter the address you gave Zotero,"
                              " without /zotero at the end.") from None
        raise ZoteroError(f"The server didn't answer like a WebDAV server: {exc}") from None
    except (FetchError, PolicyError) as exc:
        raise ZoteroError(str(exc)) from None


class AttachmentFiles:
    def __init__(self, app):
        self.app = app
        self.root = app.cfg.cache_dir / "zotero-files"

    def _path(self, src, att) -> Path:
        tag = att["md5"] or "latest"
        return self.root / str(src["id"]) / f"{att['attachment_key']}-{tag}"

    async def get(self, src, att) -> bytes:
        path = self._path(src, att)
        try:
            st = path.stat()
            if att["md5"] or time.time() - st.st_mtime < UNVERSIONED_SECONDS:
                os.utime(path)
                return await asyncio.to_thread(path.read_bytes)
        except OSError:
            pass
        data = await asyncio.to_thread(self._download, src, att)
        await asyncio.to_thread(self._store, path, data)
        return data

    def _download(self, src, att) -> bytes:
        if att["link_mode"] in ("linked_url", "linked_file"):
            raise ZoteroError("This attachment only links to a file outside Zotero's storage.", 409)
        storage = self.app.zotero.storage(src)
        try:
            if storage and src["library_type"] == "user":
                dav = WebDav(self.app.net, storage_root(storage["url"]), storage.get("username", ""),
                             storage.get("password", ""), timeout=120, max_bytes=MAX_FILE_BYTES)
                try:
                    body, _ = dav.get(f"{att['attachment_key']}.zip")
                except DavError as exc:
                    if exc.status == 404:
                        raise ZoteroError("The WebDAV folder has no file for this attachment yet. Let Zotero"
                                          " sync its files, then try again.", 404) from None
                    raise ZoteroError(f"The WebDAV server answered: {exc}") from None
                return unzip_attachment(body, att["filename"])
            client = self.app.zotero.client(src)
            return client.file(src["library_type"], src["library_id"], att["attachment_key"])
        except (FetchError, PolicyError) as exc:
            raise ZoteroError(str(exc)) from None

    def _store(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        for old in path.parent.glob(path.name.rsplit("-", 1)[0] + "-*"):
            old.unlink(missing_ok=True)  # earlier versions of the same attachment
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        self._trim()

    def _trim(self) -> None:
        files = []
        for p in self.root.glob("*/*"):
            try:
                st = p.stat()
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, p))
        total = sum(f[1] for f in files)
        for _, size, p in sorted(files):
            if total <= CACHE_BYTES:
                break
            p.unlink(missing_ok=True)
            total -= size

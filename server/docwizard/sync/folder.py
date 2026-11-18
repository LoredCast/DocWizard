"""Sync with a plain folder on the server (§12.6).

The folder looks to the sync engine like a WebDAV server: the same calls, with a
file's modification time and size as its ETag. An admin lists the folders that
projects may use (``sync.folder_roots``), typically ones that Syncthing, Dropbox,
a mounted share or a git checkout (§12.7) also use, so the files are plain files
on disk that any other program can read and change.

Every path stays inside the folder: symlinks are never followed, and writes are
atomic, as for project folders (§11.3). Blocking: call from a thread.
"""

from __future__ import annotations

import os
import stat as stat_mod
from pathlib import Path

from ..projects import files
from .webdav import DavError, Entry


def etag_of(st: os.stat_result) -> str:
    return f'"{st.st_mtime_ns:x}-{st.st_size:x}"'


def allowed_root(roots: list[str], folder: str) -> tuple[Path, str]:
    """Split *folder* into (configured root, path below it); raises ValueError when it's outside every root.

    *folder* is either an absolute path below a root, or a path relative to the first root.
    """
    folder = folder.strip()
    if not roots:
        raise ValueError("Folder sync isn't enabled on this server.")
    candidates = [Path(r).resolve() for r in roots]
    raw = Path(folder)
    if not raw.is_absolute():
        raw = candidates[0] / folder
    raw = Path(os.path.normpath(raw))  # ".." is resolved by name; symlinks are checked when it's opened
    for root in candidates:
        try:
            rel = raw.relative_to(root).as_posix()
        except ValueError:
            continue
        if rel in ("", "."):
            raise ValueError("Pick a folder inside the shared folder, not the shared folder itself.")
        try:
            rel = files.normalize(rel)
        except files.PathError as exc:
            raise ValueError(str(exc)) from None
        if any(part.startswith(".") for part in rel.split("/")):
            raise ValueError("The folder can't be hidden (start with a dot).")
        return root, rel
    allowed = ", ".join(str(c) for c in candidates)
    raise ValueError(f"The folder has to be inside {allowed}.")


class FolderRemote:
    """A folder as a sync remote: *base* is a trusted root, *prefix* the project's folder below it."""

    label = "Folder"

    def __init__(self, base: Path, prefix: str):
        self.base = Path(base)
        self.prefix = prefix.strip("/")

    def _rel(self, path: str) -> str:
        path = path.strip("/")
        return f"{self.prefix}/{path}" if self.prefix else path

    def _stat(self, path: str) -> os.stat_result | None:
        parts = files.normalize(self._rel(path)).split("/")
        try:
            dfd = files._open_dir(self.base, parts[:-1], create=False)
        except OSError:
            return None
        try:
            st = os.stat(parts[-1], dir_fd=dfd, follow_symlinks=False)
        except OSError:
            return None
        finally:
            os.close(dfd)
        return st if stat_mod.S_ISREG(st.st_mode) else None

    def _check(self, path: str, if_match: str | None, if_none_match: bool) -> None:
        st = self._stat(path)
        if if_none_match and st is not None:
            raise DavError(f"{path} exists already.", 412)
        if if_match and (st is None or etag_of(st) != if_match):
            raise DavError(f"{path} changed meanwhile.", 412)

    # -- the calls the sync engine makes ------------------------------------
    def ensure_root(self) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        if self.prefix:
            fd = files._open_dir(self.base, self.prefix.split("/"), create=True)
            os.close(fd)

    def walk(self, known_dir_etags: dict[str, str] | None = None):
        self.ensure_root()
        fd = files._open_dir(self.base, self.prefix.split("/") if self.prefix else [], create=False)
        os.close(fd)  # opened without following symlinks: the walk below starts at the real folder
        root = self.base / self.prefix if self.prefix else self.base
        found: dict[str, Entry] = {}
        for rel in files.walk(root):
            st = self._stat(rel)
            if st is not None:
                found[rel] = Entry(rel, False, etag_of(st), st.st_size, None)
        return found, {}, set()

    def get(self, path: str) -> tuple[bytes, str | None]:
        try:
            data = files.read_bytes(self.base, self._rel(path))
        except FileNotFoundError:
            raise DavError(f"{path} doesn't exist.", 404) from None
        st = self._stat(path)
        return data, etag_of(st) if st else None

    def put(self, path: str, data: bytes, *, if_match: str | None = None, if_none_match: bool = False) -> str:
        self._check(path, if_match, if_none_match)
        files.atomic_write(self.base, self._rel(path), data)
        st = self._stat(path)
        return etag_of(st) if st else None

    def delete(self, path: str, *, if_match: str | None = None) -> None:
        self._check(path, if_match, False)
        files.remove(self.base, self._rel(path))
        self._prune_dirs(path)

    def makedirs(self, path: str) -> None:
        parts = files.normalize(self._rel(path)).split("/")[:-1]
        if parts:
            os.close(files._open_dir(self.base, parts, create=True))

    def move(self, src: str, dest: str) -> None:
        if self._stat(dest) is not None:
            raise DavError(f"{dest} exists already.", 412)
        src_parts = files.normalize(self._rel(src)).split("/")
        dst_parts = files.normalize(self._rel(dest)).split("/")
        sfd = files._open_dir(self.base, src_parts[:-1], create=False)
        try:
            dfd = files._open_dir(self.base, dst_parts[:-1], create=True)
            try:
                os.rename(src_parts[-1], dst_parts[-1], src_dir_fd=sfd, dst_dir_fd=dfd)
            finally:
                os.close(dfd)
        finally:
            os.close(sfd)
        self._prune_dirs(src)

    def _prune_dirs(self, path: str) -> None:
        """Remove folders that a delete or move left empty, up to the project's folder."""
        parts = path.strip("/").split("/")[:-1]
        while parts:
            rel = self._rel("/".join(parts))
            parent = rel.split("/")
            try:
                dfd = files._open_dir(self.base, parent[:-1], create=False)
            except OSError:
                return
            try:
                os.rmdir(parent[-1], dir_fd=dfd)
            except OSError:
                return  # not empty
            finally:
                os.close(dfd)
            parts.pop()

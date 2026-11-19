"""Safe paths, atomic writes and the blob store (§11.3).

Paths are relative, NFC-normalized, never contain ``..`` or control
characters and avoid names reserved on Windows. Symlinks are never followed
and every resolved path stays inside the project folder. Writes go to a
temporary file that is fsynced and renamed into place.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat as stat_mod
import unicodedata
from pathlib import Path

O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)

_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.IGNORECASE)
_BAD_CHARS = set('<>:"\\|?*')

TEXT_KINDS = {
    ".md": "markdown", ".markdown": "markdown",
    ".tex": "latex", ".sty": "latex", ".cls": "latex",
    ".bib": "bib",
    ".txt": "text", ".json": "text", ".csl": "text", ".css": "text", ".html": "text",
    ".typ": "text", ".lua": "text", ".yaml": "text", ".yml": "text", ".ris": "text", ".xml": "text",
    # Data sheets: text documents the editor shows as a grid (§6.3); data.lua draws tables and charts from them.
    ".csv": "text", ".tsv": "text",
}
SHEET_EXTENSIONS = (".csv", ".tsv")


def is_sheet(path: str) -> bool:
    return path.lower().endswith(SHEET_EXTENSIONS)


class PathError(ValueError):
    pass


def normalize(path: str) -> str:
    """Validate and normalize a relative project path."""
    if not isinstance(path, str) or not path or len(path) > 1024:
        raise PathError("Paths must be 1 to 1024 characters long.")
    path = unicodedata.normalize("NFC", path)
    if path.startswith("/") or path.startswith("\\") or re.match(r"^[A-Za-z]:", path):
        raise PathError("Paths must be relative.")
    parts = path.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise PathError("Paths can't contain empty parts, '.' or '..'.")
        if len(part.encode()) > 255:
            raise PathError("A path part is too long.")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in part) or _BAD_CHARS.intersection(part):
            raise PathError("Paths can't contain control characters or any of < > : \" \\ | ? *")
        if _RESERVED.match(part) or part.endswith(".") or part.endswith(" "):
            raise PathError(f"'{part}' is a reserved name on some systems.")
    return "/".join(parts)


def kind_for(path: str) -> str | None:
    """Document kind for a text file, or None for a binary asset."""
    return TEXT_KINDS.get(os.path.splitext(path)[1].lower())


def _open_dir(root: Path, parts: list[str], create: bool) -> int:
    """Open the directory ``root/parts…`` without following symlinks."""
    fd = os.open(root, os.O_RDONLY | O_DIRECTORY)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            nfd = os.open(part, os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nfd
    except OSError:
        os.close(fd)
        raise
    return fd


def read_bytes(root: Path, rel: str) -> bytes:
    parts = normalize(rel).split("/")
    dfd = _open_dir(root, parts[:-1], create=False)
    try:
        fd = os.open(parts[-1], os.O_RDONLY | O_NOFOLLOW, dir_fd=dfd)
    finally:
        os.close(dfd)
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat_mod.S_ISREG(st.st_mode):
            raise PathError("Not a regular file.")
        return fh.read()


def stat_of(root: Path, rel: str) -> tuple[int, int, int] | None:
    """``(mtime_ns, inode, size)`` of a regular file, or None if missing."""
    parts = normalize(rel).split("/")
    try:
        dfd = _open_dir(root, parts[:-1], create=False)
    except OSError:
        return None
    try:
        st = os.stat(parts[-1], dir_fd=dfd, follow_symlinks=False)
    except OSError:
        return None
    finally:
        os.close(dfd)
    if not stat_mod.S_ISREG(st.st_mode):
        return None
    return (st.st_mtime_ns, st.st_ino, st.st_size)


def atomic_write(root: Path, rel: str, data: bytes) -> tuple[int, int, int]:
    """Write via temp file, fsync and rename. Returns the new file's stat."""
    root.mkdir(parents=True, exist_ok=True)
    parts = normalize(rel).split("/")
    dfd = _open_dir(root, parts[:-1], create=True)
    try:
        tmp = f".{parts[-1]}.{secrets.token_hex(6)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_NOFOLLOW, 0o644, dir_fd=dfd)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.rename(tmp, parts[-1], src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
            raise
        os.fsync(dfd)
        st = os.stat(parts[-1], dir_fd=dfd, follow_symlinks=False)
    finally:
        os.close(dfd)
    return (st.st_mtime_ns, st.st_ino, st.st_size)


def remove(root: Path, rel: str) -> None:
    parts = normalize(rel).split("/")
    try:
        dfd = _open_dir(root, parts[:-1], create=False)
    except OSError:
        return
    try:
        os.unlink(parts[-1], dir_fd=dfd)
    except FileNotFoundError:
        pass
    finally:
        os.close(dfd)


def walk(root: Path) -> list[str]:
    """All regular files below *root* as relative paths, skipping dotfiles and symlinks."""
    out: list[str] = []
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")
                       and not os.path.islink(os.path.join(dirpath, d))]
        for name in filenames:
            if name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            try:
                out.append(normalize(rel))
            except PathError:
                continue
    return sorted(out)


# -- blobs ---------------------------------------------------------------

def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def blob_path(data_dir: Path, digest: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise PathError("invalid blob id")
    return data_dir / "blobs" / digest


def put_blob(data_dir: Path, data: bytes) -> str:
    digest = sha256(data)
    path = blob_path(data_dir, digest)
    if not path.exists():
        atomic_write(data_dir / "blobs", digest, data)
    return digest


def get_blob(data_dir: Path, digest: str) -> bytes:
    return blob_path(data_dir, digest).read_bytes()


# -- text encoding -----------------------------------------------------------

def decode_text(data: bytes) -> tuple[str, str]:
    """UTF-8 bytes -> (text with \\n line endings, original line ending)."""
    text = data.decode("utf-8")  # raises UnicodeDecodeError for invalid UTF-8
    if text.startswith("﻿"):
        text = text[1:]
    ending = "crlf" if "\r\n" in text else "lf"
    return text.replace("\r\n", "\n").replace("\r", "\n"), ending


def encode_text(text: str, ending: str) -> bytes:
    if ending == "crlf":
        text = text.replace("\n", "\r\n")
    return text.encode("utf-8")

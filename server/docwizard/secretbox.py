"""Secrets at rest (§15): AES-256-GCM with a master key kept outside the database.

The key file holds one key per line as ``<key id>:<base64 of 32 bytes>``; the first
line is the current key, the others still decrypt older values. Every sealed value
records the id of its key (``v1.<key id>.<base64 nonce + ciphertext>``), so keys can
be rotated: ``docwizard rotate-key`` adds a new first line and re-encrypts every
``*_enc`` column with it.

Without a configured key file, development setups get ``<data_dir>/secret.key``,
created on first use.
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger("docwizard.secretbox")

PREFIX = "v1"

# Every column that holds a sealed value, with the columns that identify its row.
# The row identity is bound into the ciphertext, so a value can't be moved to another row.
ENCRYPTED_COLUMNS = [
    ("sync_targets", "secret_enc", "id"),
    ("citation_sources", "config_enc", "id"),
    ("users", "totp_secret_enc", "id"),
]


class SecretError(Exception):
    pass


def _parse(text: str) -> list[tuple[str, bytes]]:
    keys = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        kid, _, b64 = line.partition(":")
        key = base64.b64decode(b64)
        if not kid.isalnum() or len(key) != 32:
            raise SecretError("each key line must be '<alphanumeric id>:<base64 of 32 bytes>'")
        keys.append((kid, key))
    if not keys:
        raise SecretError("the key file holds no keys")
    return keys


def new_key_line() -> str:
    return f"k{secrets.token_hex(4)}:{base64.b64encode(AESGCM.generate_key(bit_length=256)).decode()}"


class SecretBox:
    def __init__(self, keys: list[tuple[str, bytes]]):
        self.keys = dict(keys)
        self.current = keys[0][0]

    @classmethod
    def from_config(cls, cfg) -> SecretBox:
        path = cfg.security.secret_key_file
        if path:
            return cls(_parse(Path(path).read_text()))
        dev = cfg.data_dir / "secret.key"
        if not dev.exists():
            dev.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(dev, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(new_key_line() + "\n")
            log.warning("no security.secret_key_file configured: created %s; keep it with your backups", dev)
        return cls(_parse(dev.read_text()))

    def seal(self, plaintext: str | bytes, context: str = "") -> str:
        data = plaintext.encode() if isinstance(plaintext, str) else plaintext
        nonce = secrets.token_bytes(12)
        ct = AESGCM(self.keys[self.current]).encrypt(nonce, data, context.encode())
        return f"{PREFIX}.{self.current}.{base64.urlsafe_b64encode(nonce + ct).decode()}"

    def open_bytes(self, sealed: str, context: str = "") -> bytes:
        try:
            prefix, kid, b64 = sealed.split(".", 2)
        except ValueError:
            raise SecretError("not a sealed value") from None
        if prefix != PREFIX or kid not in self.keys:
            raise SecretError(f"unknown key id {kid!r}")
        raw = base64.urlsafe_b64decode(b64)
        try:
            return AESGCM(self.keys[kid]).decrypt(raw[:12], raw[12:], context.encode())
        except Exception as exc:  # cryptography raises InvalidTag
            raise SecretError("the value can't be decrypted with this key") from exc

    def open(self, sealed: str, context: str = "") -> str:
        return self.open_bytes(sealed, context).decode()

    def needs_rotation(self, sealed: str) -> bool:
        return sealed.split(".", 2)[1:2] != [self.current]


def context_for(table: str, row_id) -> str:
    return f"{table}:{row_id}"


def rotate(cfg, db) -> int:
    """Add a new current key and re-encrypt every sealed value with it. Returns the count."""
    path = cfg.security.secret_key_file or str(cfg.data_dir / "secret.key")
    SecretBox.from_config(cfg)  # creates the development key if needed
    old = Path(path).read_text()
    new_text = new_key_line() + "\n" + old
    box = SecretBox(_parse(new_text))

    def work(c):
        count = 0
        for table, column, key in ENCRYPTED_COLUMNS:
            rows = c.execute(f"SELECT {key} AS k, {column} AS v FROM {table} WHERE {column} IS NOT NULL").fetchall()
            for r in rows:
                ctx = context_for(table, r["k"])
                value = box.seal(box.open_bytes(r["v"], ctx), ctx)
                c.execute(f"UPDATE {table} SET {column} = ? WHERE {key} = ?", (value, r["k"]))
                count += 1
        return count

    # Write the key file first (old keys stay in it), then re-encrypt: a crash in
    # between leaves values that the new file still decrypts.
    tmp = Path(path + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(new_text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return db.write_sync(work)

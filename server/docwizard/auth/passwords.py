"""Password hashing with hashlib.scrypt and the password policy (§13.2)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import threading
from functools import lru_cache
from pathlib import Path

DEFAULT_LOG_N = 17
R, P = 8, 1
_SEMAPHORE = threading.BoundedSemaphore(2)  # each hash uses ~128 MiB at N=2^17


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text + "=" * (-len(text) % 4))


def _scrypt(password: str, salt: bytes, log_n: int, r: int, p: int) -> bytes:
    n = 1 << log_n
    with _SEMAPHORE:
        return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p,
                              maxmem=256 * 1024 * 1024 if log_n >= 17 else 0, dklen=32)


def hash_password(password: str, log_n: int | None = None) -> str:
    log_n = log_n or int(os.environ.get("DOCWIZARD_SCRYPT_LOG_N", DEFAULT_LOG_N))
    salt = os.urandom(16)
    digest = _scrypt(password, salt, log_n, R, P)
    return f"scrypt${log_n}${R}${P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        # Spend comparable time so missing accounts aren't revealed by timing.
        _scrypt(password, b"\0" * 16, int(os.environ.get("DOCWIZARD_SCRYPT_LOG_N", DEFAULT_LOG_N)), R, P)
        return False
    try:
        scheme, log_n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        computed = _scrypt(password, _unb64(salt), int(log_n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(computed, _unb64(digest))


def needs_rehash(stored: str) -> bool:
    try:
        _, log_n, r, p, *_ = stored.split("$")
    except ValueError:
        return True
    target = int(os.environ.get("DOCWIZARD_SCRYPT_LOG_N", DEFAULT_LOG_N))
    return int(log_n) < target or int(r) != R or int(p) != P


@lru_cache(maxsize=1)
def _common() -> frozenset[str]:
    path = Path(__file__).with_name("common-passwords.txt")
    if not path.exists():
        return frozenset()
    return frozenset(line.strip().lower() for line in path.read_text().splitlines() if line.strip())


def check_policy(password: str, min_length: int = 12, username: str = "") -> str | None:
    """Return an error message, or None if the password is acceptable."""
    if len(password) < min_length:
        return f"Use at least {min_length} characters."
    if len(password) > 1024:
        return "Use at most 1024 characters."
    lowered = password.lower()
    if lowered in _common() or (username and lowered == username.lower()):
        return "This password is too common. Choose another one."
    return None

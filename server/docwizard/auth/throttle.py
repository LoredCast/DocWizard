"""In-memory token buckets for login throttling (§13.2)."""

from __future__ import annotations

import time


class TokenBucket:
    def __init__(self, capacity: float, refill_per_second: float):
        self.capacity = capacity
        self.rate = refill_per_second
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str, cost: float = 1.0, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        tokens, last = self._buckets.get(key, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - last) * self.rate)
        if tokens < cost:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - cost, now)
        if len(self._buckets) > 100_000:
            self._buckets.clear()  # crude bound on memory; buckets refill anyway
        return True


class LoginThrottle:
    """10 attempts per account and 30 per IP, refilling over 15 minutes."""

    def __init__(self) -> None:
        self.per_account = TokenBucket(10, 10 / 900)
        self.per_ip = TokenBucket(30, 30 / 900)

    def allow(self, username: str, ip: str) -> bool:
        ok_ip = self.per_ip.allow(ip)
        ok_account = self.per_account.allow(username.lower())
        return ok_ip and ok_account

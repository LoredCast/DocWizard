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


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


class LoginThrottle:
    """Logins: 10 attempts per account and 30 per IP, refilling over 15 minutes.

    Registrations and password reset requests have buckets of their own, keyed by the
    address they come from and the email address or name they are for, so people who
    sign up at the same time never share one bucket for the whole server.
    """

    def __init__(self) -> None:
        self.per_account = TokenBucket(10, 10 / 900)
        self.per_ip = TokenBucket(30, 30 / 900)
        # 10 new accounts per address an hour (a classroom behind one NAT signs up in bursts,
        # and the bucket refills), 3 per email address.
        self.register_ip = TokenBucket(10, 10 / 3600)
        self.register_email = TokenBucket(3, 3 / 3600)
        # Reset mails: 5 per address and 3 per account name or email address an hour.
        self.forgot_ip = TokenBucket(5, 5 / 3600)
        self.forgot_ident = TokenBucket(3, 3 / 3600)

    def allow(self, username: str, ip: str) -> bool:
        ok_ip = self.per_ip.allow(ip)
        ok_account = self.per_account.allow(username.lower())
        return ok_ip and ok_account

    def allow_register(self, ip: str, email: str | None = None) -> bool:
        ok_ip = self.register_ip.allow(ip)
        email = normalize_email(email)
        ok_email = self.register_email.allow(email) if email else True
        return ok_ip and ok_email

    def allow_forgot(self, ip: str, identifier: str) -> bool:
        ok_ip = self.forgot_ip.allow(ip)
        ident = normalize_email(identifier)
        ok_ident = self.forgot_ident.allow(ident) if ident else True
        return ok_ip and ok_ident

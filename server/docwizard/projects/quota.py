"""Storage quotas and object limits (§7.7).

What counts against an account is what the projects it owns take up
(``service.user_storage_bytes``): uploads to a shared project count for its owner.
The limit is ``users.quota_bytes``, or ``limits.default_quota_bytes`` when that is
NULL. A limit of 0 or less means no limit, for the default as for one account.

Uploads, new documents and versions check the exact figure. Live edits come in
many small pushes, so they use a figure at most ``LIVE_TTL`` seconds old plus what
was added since, and only text that grows a document is checked: deleting always
works, so nobody gets stuck over their quota.
"""

from __future__ import annotations

import time

from . import service as svc

LIVE_TTL = 30.0
QUOTA_MESSAGE = "Storage quota reached. Delete files or empty the trash to free space."


class QuotaExceeded(Exception):
    def __init__(self, message: str = QUOTA_MESSAGE, code: str = "quota_exceeded"):
        super().__init__(message)
        self.message = message
        self.code = code


def effective_limit(quota_bytes: int | None, default: int) -> int | None:
    """The limit in bytes, or None for none."""
    value = quota_bytes if quota_bytes is not None else default
    return value if value and value > 0 else None


class Quotas:
    def __init__(self, app):
        self.app = app
        self._live: dict[int, tuple[float, int]] = {}  # owner -> (measured at, bytes)
        self.rejections = 0  # for /metrics

    @property
    def limits(self):
        return self.app.cfg.limits

    def forget(self, user_id: int | None = None) -> None:
        """Drop cached figures, after deletions or an emptied trash."""
        if user_id is None:
            self._live.clear()
        else:
            self._live.pop(user_id, None)

    async def usage(self, user_id: int) -> dict:
        """Used bytes and the limit, for the home page and settings."""

        def load(c):
            row = c.execute("SELECT quota_bytes FROM users WHERE id = ?", (user_id,)).fetchone()
            return svc.user_storage_bytes(c, user_id), row["quota_bytes"] if row else None

        used, quota = await self.app.db.read(load)
        self._live[user_id] = (time.monotonic(), used)
        limit = effective_limit(quota, self.limits.default_quota_bytes)
        return {"used": used, "limit": limit,
                "percent": min(100, round(used * 100 / limit)) if limit else None,
                "full": limit is not None and used >= limit}

    def _reject(self, message: str = QUOTA_MESSAGE, code: str = "quota_exceeded"):
        self.rejections += 1
        raise QuotaExceeded(message, code)

    async def check(self, project_id: str, growth: int, *, new_files: int = 0, live: bool = False) -> None:
        """Raise QuotaExceeded if adding *growth* bytes (and *new_files* files) to the project
        would go over its owner's quota or the file limit."""
        max_files = self.limits.max_files_per_project
        count_files = bool(new_files) and max_files > 0
        if growth <= 0 and not count_files:
            return
        now = time.monotonic()
        live_cache = dict(self._live) if live else {}

        def load(c):
            owner = c.execute("SELECT p.owner_id, u.quota_bytes FROM projects p JOIN users u ON u.id = p.owner_id"
                              " WHERE p.id = ?", (project_id,)).fetchone()
            if owner is None:
                return None, None, None, 0
            files_now = svc.count_files(c, project_id) if count_files else 0
            used = None
            if growth > 0:
                entry = live_cache.get(owner["owner_id"])
                if entry is not None and now - entry[0] < LIVE_TTL:
                    used = entry[1]
                else:
                    used = svc.user_storage_bytes(c, owner["owner_id"])
            return owner["owner_id"], owner["quota_bytes"], used, files_now

        owner, quota, used, files_now = await self.app.db.read(load)
        if owner is None:
            return
        if count_files and files_now + new_files > max_files:
            self._reject("This project has reached the limit on the number of files.", "file_limit")
        if growth <= 0 or used is None:
            return
        limit = effective_limit(quota, self.limits.default_quota_bytes)
        if limit is not None and used + growth > limit:
            self._reject()
        if live:
            # Count what this edit adds, so the figure stays close until it is measured again.
            at = self._live.get(owner, (now, used))[0]
            self._live[owner] = (at if now - at < LIVE_TTL else now, used + growth)

    async def check_new_project(self, user_id: int, growth: int = 1) -> None:
        """A new project for *user_id*, starting with *growth* bytes of files."""
        max_projects = self.limits.max_projects_per_user

        def load(c):
            row = c.execute("SELECT quota_bytes FROM users WHERE id = ?", (user_id,)).fetchone()
            owned = svc.count_owned_projects(c, user_id) if max_projects > 0 else 0
            return owned, row["quota_bytes"] if row else None, svc.user_storage_bytes(c, user_id)

        owned, quota, used = await self.app.db.read(load)
        limit = effective_limit(quota, self.limits.default_quota_bytes)
        if limit is not None and used + max(1, growth) > limit:
            self._reject()
        if max_projects > 0 and owned >= max_projects:
            self._reject("You have reached the limit on the number of projects. Delete one to start another.",
                         "project_limit")

"""The single place for access checks (§13.5).

Handlers and the collaboration authority call :func:`require` with the
actor's role on a project and the capability they need.
"""

from __future__ import annotations

from ..http.request import HTTPError

ROLES = ("viewer", "commenter", "editor", "owner")
_RANK = {role: i for i, role in enumerate(ROLES)}

# capability -> lowest role that has it
CAPABILITIES = {
    "read": "viewer",
    "preview": "viewer",
    "export": "viewer",
    "presence": "viewer",
    "comment": "commenter",
    "resolve_comments": "editor",
    "edit": "editor",
    "upload": "editor",
    "change_layout": "editor",
    "versions": "editor",
    "share": "owner",
    "publish": "owner",
    "sync": "owner",
    "manage_members": "owner",
    "delete_project": "owner",
}


def rank(role: str | None) -> int:
    return _RANK.get(role or "", -1)


def can(role: str | None, capability: str) -> bool:
    needed = CAPABILITIES[capability]
    return rank(role) >= _RANK[needed]


def require(role: str | None, capability: str) -> None:
    if role is None:
        # Don't reveal whether a project exists to people without access.
        raise HTTPError(404, "not_found", "This project doesn't exist or you don't have access.")
    if not can(role, capability):
        raise HTTPError(403, "forbidden", "Your role on this project doesn't allow this.")


def higher(a: str | None, b: str | None) -> str | None:
    return a if rank(a) >= rank(b) else b

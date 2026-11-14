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


# Capabilities owners can extend to editors per project (§13.5: "configurable").
CONFIGURABLE = {"share": "editors_can_share", "publish": "editors_can_publish"}


def can(role: str | None, capability: str, project=None) -> bool:
    needed = CAPABILITIES[capability]
    if rank(role) >= _RANK[needed]:
        return True
    column = CONFIGURABLE.get(capability)
    if column and role == "editor" and project is not None:
        try:
            return bool(project[column])
        except (KeyError, IndexError):
            return False
    return False


def require(role: str | None, capability: str, project=None) -> None:
    if role is None:
        # Don't reveal whether a project exists to people without access.
        raise HTTPError(404, "not_found", "This project doesn't exist or you don't have access.")
    if not can(role, capability, project):
        raise HTTPError(403, "forbidden", "Your role on this project doesn't allow this.")


def higher(a: str | None, b: str | None) -> str | None:
    return a if rank(a) >= rank(b) else b

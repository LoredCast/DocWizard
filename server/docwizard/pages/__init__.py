"""Handlers for server-rendered pages."""

from __future__ import annotations


def register(app) -> None:
    from . import account, integrations, misc, offline, projects, share

    account.register(app)
    integrations.register(app)
    misc.register(app)
    offline.register(app)
    projects.register(app)
    share.register(app)

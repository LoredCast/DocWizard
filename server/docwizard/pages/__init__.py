"""Handlers for server-rendered pages."""

from __future__ import annotations


def register(app) -> None:
    from . import account, misc, projects, share

    account.register(app)
    misc.register(app)
    projects.register(app)
    share.register(app)

"""Handlers for server-rendered pages."""

from __future__ import annotations


def register(app) -> None:
    from . import (account, admin, integrations, layouts, lifecycle, misc, offline, oidc, projects, share, tokens,
                   twostep)

    account.register(app)
    admin.register(app)
    integrations.register(app)
    lifecycle.register(app)
    layouts.register(app)
    oidc.register(app)
    misc.register(app)
    offline.register(app)
    projects.register(app)
    share.register(app)
    twostep.register(app)
    tokens.register(app)

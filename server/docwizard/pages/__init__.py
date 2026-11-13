"""Handlers for server-rendered pages."""

from __future__ import annotations


def register(app) -> None:
    from . import account, misc

    account.register(app)
    misc.register(app)

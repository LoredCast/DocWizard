"""JSON API handlers, one module per area."""

from __future__ import annotations


def register(app) -> None:
    from . import browser_render, citations, comments, grammar, layouts, offline, projects, publish, render, sync

    projects.register(app)
    layouts.register(app)
    render.register(app)
    comments.register(app)
    sync.register(app)
    publish.register(app)
    citations.register(app)
    grammar.register(app)
    offline.register(app)
    browser_render.register(app)

"""JSON API handlers, one module per area."""

from __future__ import annotations


def register(app) -> None:
    from . import (activity, assist, browser_render, citations, comments, data, deck, grammar, layouts, offline,
                   preflight, project_text, projects, publish, render, sync)

    projects.register(app)
    activity.register(app)
    project_text.register(app)
    preflight.register(app)
    layouts.register(app)
    render.register(app)
    comments.register(app)
    sync.register(app)
    publish.register(app)
    citations.register(app)
    grammar.register(app)
    offline.register(app)
    data.register(app)
    deck.register(app)
    browser_render.register(app)
    assist.register(app)

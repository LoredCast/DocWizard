"""JSON API handlers, one module per area."""

from __future__ import annotations


def register(app) -> None:
    from . import (activity, citations, comments, grammar, layouts, offline, project_text, projects, publish, render,
                   sync)

    projects.register(app)
    activity.register(app)
    project_text.register(app)
    layouts.register(app)
    render.register(app)
    comments.register(app)
    sync.register(app)
    publish.register(app)
    citations.register(app)
    grammar.register(app)
    offline.register(app)

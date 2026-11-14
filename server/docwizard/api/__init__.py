"""JSON API handlers, one module per area."""

from __future__ import annotations


def register(app) -> None:
    from . import comments, layouts, projects, render

    projects.register(app)
    layouts.register(app)
    render.register(app)
    comments.register(app)

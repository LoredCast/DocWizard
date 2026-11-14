"""Layouts available to the user (§9.3)."""

from __future__ import annotations

from ..http.request import Request
from ..http.response import Response, json_response


def register(app) -> None:
    async def list_layouts(req: Request) -> Response:
        return json_response({"layouts": [lay.public() for lay in app.layouts.list()],
                              "default": app.policy("default_layout")})

    app.router.add("GET", "/api/layouts", list_layouts)

"""The layout gallery and the layout editor (§9.3)."""

from __future__ import annotations

from ..api.layouts import _get, can_edit
from ..http.request import Request
from ..http.response import Response
from ..render.layouts import style_labels


def register(app) -> None:
    async def gallery(req: Request) -> Response:
        uid = req.user.id
        layouts = app.layouts.list() if req.user.is_admin else app.layouts.visible(uid)
        builtin = [lay for lay in layouts if not lay.custom]
        mine = [lay for lay in layouts if lay.custom and lay.owner == uid]
        shared = [lay for lay in layouts if lay.custom and lay.owner != uid]
        owners = {}
        if shared:
            ids = sorted({lay.owner for lay in shared})
            rows = await app.db.fetchall(f"SELECT id, display_name FROM users WHERE id IN ({','.join('?' * len(ids))})",
                                         tuple(ids))
            owners = {r["id"]: r["display_name"] for r in rows}
        return app.page(req, "layouts.html", builtin=builtin, mine=mine, shared=shared, owners=owners,
                        styles=style_labels(), default_layout=app.policy("default_layout"),
                        names={lay.id: lay.name for lay in app.layouts.list()},
                        can_edit=lambda lay: can_edit(req, lay))

    async def editor(req: Request) -> Response:
        layout = _get(app, req, req.param("id"))
        return app.page(req, "layout_editor.html", layout=layout, editable=can_edit(req, layout),
                        layouts=app.layouts.visible(req.user.id))

    app.router.add("GET", "/layouts", gallery)
    app.router.add("GET", "/layouts/{id}", editor)

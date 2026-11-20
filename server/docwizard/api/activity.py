"""Project activity and who is where (§8.12).

``GET /api/projects/{id}/activity`` pages through the timeline, each event with a sentence
in the interface language. ``GET /api/projects/{id}/presence`` lists the people editing
the project's documents right now, from the rooms in memory, so the editor can show who is
working in another chapter and jump there.
"""

from __future__ import annotations

from .. import activity
from ..http.request import Request, bad_request
from ..http.response import Response, json_response
from .projects import project_access


def register(app) -> None:
    r = app.router

    async def timeline(req: Request) -> Response:
        project, _role = await project_access(app, req, req.param("id"), "read")
        if req.guest is not None:
            # Who did what is about the members; guests don't see the member list either (§14.2).
            return json_response({"items": [], "more": False})
        try:
            before = int(req.query["before"]) if req.query.get("before") else None
            limit = int(req.query.get("limit") or 50)
        except ValueError:
            raise bad_request("before and limit are numbers.") from None
        items = await app.db.read(lambda c: activity.list_for(c, project["id"], before=before, limit=limit + 1))
        more = len(items) > limit
        items = items[:limit]
        gettext = lambda message, **values: app.t(req, message, **values)  # noqa: E731
        for item in items:
            item["text"] = activity.describe(item, gettext)
            item["when"] = activity.iso(item["at"])
        return json_response({"items": items, "more": more})

    async def presence(req: Request) -> Response:
        project, _role = await project_access(app, req, req.param("id"), "read")
        me = req.actor_key
        docs = []
        for room in list(app.rooms.rooms.values()):
            if room.closed or room.project_id != project["id"]:
                continue
            people, seen = [], set()
            for peer in room.peers.values():
                if peer.identity in seen:
                    continue
                seen.add(peer.identity)
                people.append({"name": peer.name, "color": peer.color, "me": peer.identity == me})
            if people:
                docs.append({"docId": room.doc_id, "path": room.path, "people": people})
        docs.sort(key=lambda d: d["path"])
        return json_response({"docs": docs})

    r.add("GET", "/api/projects/{id}/activity", timeline, guest=True)
    r.add("GET", "/api/projects/{id}/presence", presence, guest=True)

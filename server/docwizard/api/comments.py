"""JSON API: comment threads, conflicts and share links (§7.5, §8.8, §12.3, §14.1)."""

from __future__ import annotations

from .. import notifications
from ..auth import permissions
from ..collab.authority import PushError
from ..http.request import HTTPError, Request, bad_request, not_found
from ..http.response import Response, json_response
from ..pages.projects import create_share_link
from ..sharing import links
from .projects import document_access, project_access

MAX_BODY = 10_000


def _int(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise bad_request("Expected a number.")
    return value


def _body(value) -> str:
    text = str(value or "").strip()
    if not text:
        raise bad_request("Write a comment first.")
    if len(text) > MAX_BODY:
        raise bad_request("Comments are limited to 10,000 characters.")
    return text


def _author(req: Request) -> tuple[int | None, int | None, str]:
    if req.user is not None:
        return req.user.id, None, req.user.display_name
    return None, req.guest.id, req.guest.label


def _announce(room, doc_id: str, users: list[int]) -> None:
    """Tell open editors whose people just got a notification, so their counts update."""
    if users:
        room.broadcast({"t": "notifications", "doc": doc_id, "users": [f"u{u}" for u in users]})


def register(app) -> None:
    r = app.router

    # -- notifications (§8.9) -------------------------------------------------
    async def list_notifications(req: Request) -> Response:
        uid = req.user.id
        unread_only = req.query.get("unread") == "1"

        def load(c):
            return notifications.list_for(c, uid, unread_only=unread_only), notifications.unread_count(c, uid)

        items, unread = await app.db.read(load)
        from ..pages.projects import doc_url

        return json_response({"unread": unread, "items": [
            {"id": i["id"], "kind": i["kind"], "actor": i["actor"], "excerpt": i["excerpt"],
             "createdAt": i["created_at"], "read": i["read_at"] is not None, "docId": i["doc_id"],
             "path": i["path"], "project": i["project"], "threadId": i["thread_id"],
             "url": f"{doc_url(i['project_id'], i['path'])}#comment-{i['thread_id']}"} for i in items]})

    async def read_notifications(req: Request) -> Response:
        body = await req.json_object()
        uid = req.user.id
        ids = body.get("ids")
        if ids is not None and (not isinstance(ids, list) or not all(isinstance(i, int) for i in ids)):
            raise bad_request("ids must be a list of numbers.")
        doc_id = body.get("doc") if isinstance(body.get("doc"), str) else None
        thread_id = body.get("thread") if isinstance(body.get("thread"), int) else None
        if ids is None and doc_id is None and body.get("all") is not True:
            raise bad_request("Say which notifications to mark as read.")
        n = await app.db.write(lambda c: notifications.mark_read(c, uid, ids=ids, doc_id=doc_id,
                                                                  thread_id=thread_id))
        unread = await app.db.read(lambda c: notifications.unread_count(c, uid))
        return json_response({"marked": n, "unread": unread})

    r.add("GET", "/api/notifications", list_notifications)
    r.add("POST", "/api/notifications/read", read_notifications)

    # -- comments ------------------------------------------------------------
    async def list_comments(req: Request) -> Response:
        doc, project, role = await document_access(app, req, req.param("id"), "read")
        room = await app.rooms.get(doc["id"])
        version, anchors = await room.call(room.anchor_snapshot)
        rows = await app.db.fetchall(
            "SELECT id, thread_id, author_user_id, author_guest_id, author_name, body, quote, anchor_from, anchor_to,"
            " orphaned, created_at, updated_at, resolved_at, resolved_by FROM comments WHERE doc_id = ?"
            " ORDER BY thread_id, id", (doc["id"],))
        me_user, me_guest, _ = _author(req)
        unread = await app.db.read(lambda c: notifications.unread_threads(c, me_user, doc["id"])) \
            if me_user is not None else set()
        threads: dict[int, dict] = {}
        for row in rows:
            c = dict(row)
            mine = (c["author_user_id"] is not None and c["author_user_id"] == me_user) or (
                c["author_guest_id"] is not None and c["author_guest_id"] == me_guest)
            item = {"id": c["id"], "author": c["author_name"], "body": c["body"], "createdAt": c["created_at"],
                    "updatedAt": c["updated_at"], "mine": mine}
            if c["id"] == c["thread_id"]:
                anchor = anchors.get(f"comment:{c['id']}")
                threads[c["id"]] = {
                    "id": c["id"], "quote": c["quote"] or "", "orphaned": bool(c["orphaned"]),
                    "resolved": c["resolved_at"] is not None, "resolvedBy": c["resolved_by"],
                    "unread": c["id"] in unread,
                    "from": anchor[0] if anchor else None, "to": anchor[1] if anchor else None,
                    "comments": [item]}
            elif c["thread_id"] in threads:
                threads[c["thread_id"]]["comments"].append(item)
        ordered = sorted(threads.values(), key=lambda t: (t["from"] is None, t["from"] or 0, t["id"]))
        return json_response({"version": version, "threads": ordered,
                              "canComment": permissions.can(role, "comment"),
                              "canResolve": permissions.can(role, "resolve_comments")})

    async def add_comment(req: Request) -> Response:
        doc, project, role = await document_access(app, req, req.param("id"), "comment")
        body = await req.json_object(64 * 1024)
        text = _body(body.get("body"))
        uid, gid, name = _author(req)
        room = await app.rooms.get(doc["id"])
        if body.get("threadId") is not None:
            thread_id = _int(body["threadId"])
            root = await app.db.fetchone("SELECT id FROM comments WHERE id = ? AND thread_id = id AND doc_id = ?",
                                         (thread_id, doc["id"]))
            if root is None:
                raise not_found("This thread doesn't exist.")
            def reply(c):
                cid = c.execute(
                    "INSERT INTO comments(doc_id, thread_id, author_user_id, author_guest_id, author_name, body)"
                    " VALUES (?, ?, ?, ?, ?, ?)", (doc["id"], thread_id, uid, gid, name, text)).lastrowid
                return cid, notifications.record(c, project_id=project["id"], doc_id=doc["id"], comment_id=cid,
                                                 thread_id=thread_id, actor_user_id=uid, actor=name, body=text)

            cid, notified = await app.db.write(reply)
            room.broadcast({"t": "comments", "doc": doc["id"]})
            _announce(room, doc["id"], notified)
            return json_response({"id": cid}, 201)

        version, frm, to = _int(body.get("version")), _int(body.get("from")), _int(body.get("to"))

        def insert(c, a, b, v, quote):
            cid = c.execute(
                "INSERT INTO comments(doc_id, author_user_id, author_guest_id, author_name, body, quote, anchor_from,"
                " anchor_to, anchor_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (doc["id"], uid, gid, name, text, quote, a, b, v)).lastrowid
            c.execute("UPDATE comments SET thread_id = id WHERE id = ?", (cid,))
            notified.extend(notifications.record(c, project_id=project["id"], doc_id=doc["id"], comment_id=cid,
                                                 thread_id=cid, actor_user_id=uid, actor=name, body=text))
            return cid

        notified: list[int] = []
        try:
            result = await room.call(lambda: room.add_thread(version, frm, to, insert))
        except PushError as exc:
            raise HTTPError(409, exc.code, exc.message) from None
        _announce(room, doc["id"], notified)
        return json_response(result, 201)

    async def patch_comment(req: Request) -> Response:
        doc, project, role = await document_access(app, req, req.param("id"), "comment")
        cid = req.param("cid")
        row = await app.db.fetchone("SELECT * FROM comments WHERE id = ? AND doc_id = ?",
                                    (int(cid) if cid.isdigit() else -1, doc["id"]))
        if row is None:
            raise not_found("This comment doesn't exist.")
        body = await req.json_object(64 * 1024)
        uid, gid, name = _author(req)
        if "resolved" in body:
            permissions.require(role, "resolve_comments")
            if row["id"] != row["thread_id"]:
                raise bad_request("Resolve the thread, not a reply.")
            resolved = bool(body["resolved"])

            def update(c, anchor):
                if resolved:
                    if anchor:
                        (a, b), v = anchor
                        c.execute("UPDATE comments SET anchor_from = ?, anchor_to = ?, anchor_version = ? WHERE id = ?",
                                  (a, b, v, row["id"]))
                    c.execute("UPDATE comments SET resolved_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),"
                              " resolved_by = ? WHERE id = ?", (name, row["id"]))
                else:
                    c.execute("UPDATE comments SET resolved_at = NULL, resolved_by = NULL WHERE id = ?", (row["id"],))

            room = await app.rooms.get(doc["id"])
            if not row["orphaned"]:
                await room.call(lambda: room.set_resolved("comment", row["id"], resolved, update))
            else:
                await app.db.write(lambda c: update(c, None))
                room.broadcast({"t": "comments", "doc": doc["id"]})
            return json_response({"ok": True})
        if "body" in body:
            mine = (uid is not None and row["author_user_id"] == uid) or (gid is not None and row["author_guest_id"] == gid)
            if not mine:
                raise HTTPError(403, "forbidden", "You can only edit your own comments.")
            text = _body(body["body"])
            await app.db.execute("UPDATE comments SET body = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
                                 " WHERE id = ?", (text, row["id"]))
            (await app.rooms.get(doc["id"])).broadcast({"t": "comments", "doc": doc["id"]})
            return json_response({"ok": True})
        raise bad_request("Nothing to change.")

    async def delete_comment(req: Request) -> Response:
        doc, project, role = await document_access(app, req, req.param("id"), "comment")
        cid = req.param("cid")
        row = await app.db.fetchone("SELECT * FROM comments WHERE id = ? AND doc_id = ?",
                                    (int(cid) if cid.isdigit() else -1, doc["id"]))
        if row is None:
            raise not_found("This comment doesn't exist.")
        uid, gid, _ = _author(req)
        mine = (uid is not None and row["author_user_id"] == uid) or (gid is not None and row["author_guest_id"] == gid)
        if not mine and role != "owner":
            raise HTTPError(403, "forbidden", "You can only delete your own comments.")
        room = await app.rooms.get(doc["id"])
        if row["id"] == row["thread_id"]:
            async def drop():
                room.anchors.pop(("comment", row["id"]), None)
                await app.db.execute("DELETE FROM comments WHERE thread_id = ?", (row["id"],))
            await room.call(drop)
        else:
            await app.db.execute("DELETE FROM comments WHERE id = ?", (row["id"],))
        room.broadcast({"t": "comments", "doc": doc["id"]})
        return json_response({"ok": True})

    # -- conflicts -------------------------------------------------------------
    async def conflicts(req: Request) -> Response:
        doc, _, role = await document_access(app, req, req.param("id"), "read")
        room = await app.rooms.get(doc["id"])
        version, anchors = await room.call(room.anchor_snapshot)
        rows = await app.db.fetchall(
            "SELECT id, source, base_text, theirs_text, version_label, created_at FROM conflicts"
            " WHERE doc_id = ? AND resolved_at IS NULL ORDER BY id", (doc["id"],))
        out = []
        for row in rows:
            item = dict(row)
            anchor = anchors.get(f"conflict:{row['id']}")
            item["from"], item["to"] = (anchor[0], anchor[1]) if anchor else (None, None)
            item["ours_text"] = room.buf.slice(anchor[0], anchor[1]) if anchor else ""
            out.append(item)
        return json_response({"version": version, "conflicts": out})

    async def resolve_conflict(req: Request) -> Response:
        doc, _, _ = await document_access(app, req, req.param("id"), "edit")
        cid = req.param("cid")
        row = await app.db.fetchone("SELECT id FROM conflicts WHERE id = ? AND doc_id = ? AND resolved_at IS NULL",
                                    (int(cid) if cid.isdigit() else -1, doc["id"]))
        if row is None:
            return json_response({"ok": True})
        uid = req.user.id if req.user else None

        def update(c, anchor):
            c.execute("UPDATE conflicts SET resolved_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), resolved_by = ?"
                      " WHERE id = ?", (uid, row["id"]))

        room = await app.rooms.get(doc["id"])
        await room.call(lambda: room.set_resolved("conflict", row["id"], True, update))
        return json_response({"ok": True})

    # -- share links -----------------------------------------------------------
    async def list_shares(req: Request) -> Response:
        project, role = await project_access(app, req, req.param("id"), "share")
        items = await app.db.read(lambda c: links.list_for_project(c, project["id"]))
        from ..pages.projects import _link_roles

        return json_response({"links": items, "roles": _link_roles(app, role), "guests": app.policy("guests")})

    async def create_share(req: Request) -> Response:
        project, role = await project_access(app, req, req.param("id"), "share")
        body = await req.json_object()
        try:
            url = await create_share_link(app, req, project, role, body)
        except ValueError as exc:
            raise bad_request(str(exc)) from None
        return json_response({"url": url}, 201)

    async def revoke_share(req: Request) -> Response:
        project, _ = await project_access(app, req, req.param("id"), "share")
        sid = req.param("sid")
        await app.db.write(lambda c: links.revoke(c, project["id"], int(sid) if sid.isdigit() else -1))
        return json_response({"ok": True})

    r.add("GET", "/api/documents/{id}/comments", list_comments, guest=True)
    r.add("POST", "/api/documents/{id}/comments", add_comment, guest=True)
    r.add("PATCH", "/api/documents/{id}/comments/{cid}", patch_comment, guest=True)
    r.add("DELETE", "/api/documents/{id}/comments/{cid}", delete_comment, guest=True)
    r.add("GET", "/api/documents/{id}/conflicts", conflicts, guest=True)
    r.add("PATCH", "/api/documents/{id}/conflicts/{cid}", resolve_conflict, guest=True)
    r.add("GET", "/api/projects/{id}/shares", list_shares)
    r.add("POST", "/api/projects/{id}/shares", create_share)
    r.add("DELETE", "/api/projects/{id}/shares/{sid}", revoke_share)

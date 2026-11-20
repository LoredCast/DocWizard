"""Project-wide text tools: find and replace across documents, and spelling dictionaries.

Find and replace (§8.12): the browser fetches the text of every document
(``GET /api/projects/{id}/texts``), finds and previews the matches itself, so patterns
never run on the server, then sends the chosen ranges with the text each one should
still hold (``POST /api/projects/{id}/replace``). Every document goes through its room:
the ranges are mapped through edits made since the preview, a named version is kept
before, and the replacement is one update that everyone editing sees at once.

Dictionaries (§6.8): words the spelling checkers accept, for everyone in a project and
for one person in all their projects.
"""

from __future__ import annotations

import time
import unicodedata

from .. import activity
from ..collab.authority import PushError
from ..http.request import HTTPError, Request, bad_request
from ..http.response import Response, json_response
from ..projects import service as svc
from .projects import author_of, project_access

MAX_TOTAL = 20_000_000  # UTF-16 units sent to the browser at once
MAX_EDITS = 20_000
MAX_WORDS = 5000
MAX_WORD = 64


def clean_word(value) -> str | None:
    if not isinstance(value, str):
        return None
    word = unicodedata.normalize("NFC", value.strip())
    if not word or len(word) > MAX_WORD or any(ch.isspace() for ch in word):
        return None
    return word


def words_for(c, project_id: str, user_id: int | None) -> dict:
    project = [r["word"] for r in c.execute(
        "SELECT word FROM project_words WHERE project_id = ? ORDER BY word COLLATE NOCASE", (project_id,))]
    mine = [] if user_id is None else [r["word"] for r in c.execute(
        "SELECT word FROM user_words WHERE user_id = ? ORDER BY word COLLATE NOCASE", (user_id,))]
    return {"project": project, "mine": mine}


def notify_dictionary(app, project_id: str) -> None:
    """Tell everyone editing the project to fetch its dictionary again."""
    for room in list(app.rooms.rooms.values()):
        if room.project_id == project_id and not room.closed:
            room.broadcast({"t": "dictionary", "doc": room.doc_id})


async def add_words(app, project_id: str, user_id: int | None, scope: str, raw: list) -> dict:
    """Add words to the project's dictionary or to *user_id*'s; returns both lists."""
    words = [w for w in (clean_word(v) for v in raw[:MAX_WORDS]) if w]
    if not words:
        raise bad_request("Words are 1 to 64 characters without spaces.")
    now = time.time()

    def work(c):
        if scope == "project":
            have = c.execute("SELECT count(*) FROM project_words WHERE project_id = ?", (project_id,)).fetchone()[0]
        else:
            have = c.execute("SELECT count(*) FROM user_words WHERE user_id = ?", (user_id,)).fetchone()[0]
        if have + len(words) > MAX_WORDS:
            raise HTTPError(413, "too_many_words", "A dictionary holds at most 5,000 words.")
        for w in words:
            if scope == "project":
                c.execute("INSERT OR IGNORE INTO project_words(project_id, word, added_by, created_at)"
                          " VALUES (?, ?, ?, ?)", (project_id, w, user_id, now))
            else:
                c.execute("INSERT OR IGNORE INTO user_words(user_id, word, created_at) VALUES (?, ?, ?)",
                          (user_id, w, now))
        return words_for(c, project_id, user_id)

    result = await app.db.write(work)
    if scope == "project":
        notify_dictionary(app, project_id)
    return result


async def remove_word(app, project_id: str, user_id: int | None, scope: str, value) -> dict:
    word = clean_word(value)
    if word is None:
        raise bad_request("Which word?")

    def work(c):
        if scope == "project":
            c.execute("DELETE FROM project_words WHERE project_id = ? AND word = ?", (project_id, word))
        else:
            c.execute("DELETE FROM user_words WHERE user_id = ? AND word = ?", (user_id, word))
        return words_for(c, project_id, user_id)

    result = await app.db.write(work)
    if scope == "project":
        notify_dictionary(app, project_id)
    return result


def register(app) -> None:
    r = app.router

    async def texts(req: Request) -> Response:
        project, _role = await project_access(app, req, req.param("id"), "read")
        docs = await app.db.read(lambda c: c.execute(
            "SELECT id, path, kind FROM documents WHERE project_id = ? AND deleted_at IS NULL ORDER BY path",
            (project["id"],)).fetchall())
        out, total = [], 0
        for doc in docs:
            room = app.rooms.loaded(doc["id"])
            if room is not None:
                async def now(room=room):
                    return room.text(), room.version
                text, version = await room.call(now)
            else:
                state = await app.db.read(lambda c, d=doc["id"]: svc.load_state(c, d))
                text, version = state.text, state.version
            total += len(text)
            if total > MAX_TOTAL:
                raise HTTPError(413, "too_large", "This project is too large to search all at once.")
            out.append({"id": doc["id"], "path": doc["path"], "kind": doc["kind"], "version": version, "text": text})
        return json_response({"docs": out})

    async def replace(req: Request) -> Response:
        project, _role = await project_access(app, req, req.param("id"), "edit")
        body = await req.json_object(8 << 20)
        query, replacement = body.get("query"), body.get("replacement")
        docs = body.get("docs")
        if not isinstance(query, str) or not query or not isinstance(replacement, str) or not isinstance(docs, list):
            raise bad_request("Send what was searched, the replacement and the documents.")
        plan, count = [], 0
        for item in docs:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) \
                    or not isinstance(item.get("version"), int) or not isinstance(item.get("edits"), list):
                raise bad_request("Each document needs an id, a version and its edits.")
            edits = []
            for e in item["edits"]:
                try:
                    frm, to, expected, insert = int(e["from"]), int(e["to"]), e["expect"], e["insert"]
                except (KeyError, TypeError, ValueError):
                    raise bad_request("Each edit needs from, to, expect and insert.") from None
                if not isinstance(expected, str) or not isinstance(insert, str) or isinstance(e["from"], bool):
                    raise bad_request("Each edit needs from, to, expect and insert.")
                edits.append((frm, to, expected, insert))
            count += len(edits)
            plan.append((item["id"], item["version"], edits))
        if count > MAX_EDITS:
            raise bad_request("Replace at most 20,000 matches at a time.")
        ids = [d for d, _v, _e in plan]
        known = await app.db.read(lambda c: {row["id"]: row["path"] for row in c.execute(
            f"SELECT id, path FROM documents WHERE project_id = ? AND deleted_at IS NULL AND id IN"
            f" ({','.join('?' * len(ids))})", (project["id"], *ids))} if ids else {})
        if set(ids) - set(known):
            raise bad_request("A document isn't in this project.")
        author = author_of(req)
        short = query if len(query) <= 40 else query[:39] + "…"
        label = f"Before replacing “{short}”, {time.strftime('%d %b %H:%M', time.gmtime())}"
        results, replaced, touched = [], 0, []
        for doc_id, version, edits in plan:
            room = await app.rooms.get(doc_id)
            try:
                done, skipped = await room.call(
                    lambda room=room, version=version, edits=edits: room.replace_ranges(
                        version, edits, author=author, label=label))
            except PushError as exc:
                results.append({"id": doc_id, "replaced": 0, "skipped": len(edits), "error": exc.message})
                continue
            results.append({"id": doc_id, "replaced": done, "skipped": skipped})
            replaced += done
            if done:
                touched.append(known[doc_id])
        if replaced:
            one = len(touched) == 1
            await activity.log(app, project["id"], "replace", req, count=replaced, path=touched[0] if one else None,
                               doc_id=next((d for d, p in known.items() if p == touched[0]), None) if one else None,
                               detail={"query": activity.excerpt(query), "replacement": activity.excerpt(replacement),
                                       "docs": len(touched)})
        return json_response({"replaced": replaced, "documents": results})

    # -- dictionaries ------------------------------------------------------
    async def get_words(req: Request) -> Response:
        project, _role = await project_access(app, req, req.param("id"), "read")
        uid = req.user.id if req.user is not None else None
        return json_response(await app.db.read(lambda c: words_for(c, project["id"], uid)))

    async def scope_of(req: Request, body: dict):
        scope = body.get("scope")
        if scope == "project":
            project, _role = await project_access(app, req, req.param("id"), "edit")
        elif scope == "mine":
            project, _role = await project_access(app, req, req.param("id"), "read")
            if req.user is None:
                raise HTTPError(403, "forbidden", "Guests keep their words in this browser.")
        else:
            raise bad_request("The scope is project or mine.")
        return project, scope

    async def add(req: Request) -> Response:
        body = await req.json_object()
        project, scope = await scope_of(req, body)
        raw = body.get("words") if isinstance(body.get("words"), list) else [body.get("word")]
        result = await add_words(app, project["id"], req.user.id if req.user else None, scope, raw)
        return json_response(result)

    async def remove(req: Request) -> Response:
        body = await req.json_object()
        project, scope = await scope_of(req, body)
        result = await remove_word(app, project["id"], req.user.id if req.user else None, scope, body.get("word"))
        return json_response(result)

    r.add("GET", "/api/projects/{id}/texts", texts, guest=True)
    r.add("POST", "/api/projects/{id}/replace", replace, guest=True)
    r.add("GET", "/api/projects/{id}/dictionary", get_words, guest=True)
    r.add("POST", "/api/projects/{id}/dictionary", add, guest=True)
    r.add("DELETE", "/api/projects/{id}/dictionary", remove, guest=True)

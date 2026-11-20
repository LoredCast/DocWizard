"""Companion decks (§6.3): a new slides project drafted from a document.

The new project belongs to whoever asks, uses the slides layout and gets copies of the
pictures, data sheets and bibliographies the deck uses, so it renders on its own.
"""

from __future__ import annotations

import asyncio
import os

from ..http.request import Request, bad_request
from ..http.response import Response, json_response
from ..projects import deck, files
from ..projects import service as svc
from .projects import create_project_with_starter, document_access, enforce_project_limit

LAYOUT = "slides"
MAX_COPIES = 200


async def _text(room) -> str:
    return room.text()


def register(app) -> None:
    async def companion_deck(req: Request) -> Response:
        doc, project, _ = await document_access(app, req, req.param("id"), "read")
        if doc["kind"] != "markdown":
            raise bad_request("Slides can be made from Markdown documents.")
        if not app.layouts.exists(LAYOUT):
            raise bad_request("The slides layout isn't installed.")
        room = await app.rooms.get(doc["id"])
        text = await room.call(lambda: _text(room))
        stem = os.path.splitext(os.path.basename(doc["path"]))[0]
        markdown, wanted = deck.companion_deck(text, stem)
        base = os.path.dirname(doc["path"])

        # The files to copy: text documents through their rooms, others as stored blobs.
        def find(c):
            texts, assets = [], []
            for rel in sorted(wanted)[:MAX_COPIES]:
                try:
                    path = files.normalize(os.path.join(base, rel) if base else rel)
                except files.PathError:
                    continue
                d = svc.get_document_by_path(c, project["id"], path)
                if d is not None:
                    texts.append((rel, d["id"]))
                    continue
                a = svc.get_asset(c, project["id"], path)
                if a is not None:
                    assets.append((rel, a["sha256"], a["size"]))
            return texts, assets

        texts, assets = await app.db.read(find)
        copies = []
        for rel, doc_id in texts:
            r = await app.rooms.get(doc_id)
            copies.append((files.normalize(rel), await r.call(lambda r=r: _text(r))))
        growth = len(markdown.encode()) + sum(len(t.encode()) for _, t in copies) + sum(a[2] for a in assets)
        await enforce_project_limit(app, req.user.id, growth)
        name = (project["name"] or stem) + " – slides"
        pid, _, _ = await create_project_with_starter(app, req.user, name, LAYOUT, starter=False,
                                                      lang=app.lang_of(req))

        def write(c):
            main = svc.create_document(c, pid, "slides.md", markdown)
            for path, body in copies:
                if not svc.path_taken(c, pid, path):
                    svc.create_document(c, pid, path, body)
            return main

        main = await app.db.write(write)
        root = app.rooms.project_dir(pid)
        for rel, digest, size in assets:
            path = files.normalize(rel)
            data = await asyncio.to_thread(files.blob_path(app.cfg.data_dir, digest).read_bytes)
            st = await asyncio.to_thread(files.atomic_write, root, path, data)
            await app.db.write(lambda c, path=path, digest=digest, size=size, st=st:
                               svc.put_asset(c, pid, path, digest, size, st))
        for row in await app.db.fetchall("SELECT id FROM documents WHERE project_id = ?", (pid,)):
            r = await app.rooms.get(row["id"])
            await r.call(r.materialize)
        return json_response({"project": pid, "document": main, "url": f"/p/{pid}/slides.md"}, 201)

    app.router.add("POST", "/api/documents/{id}/companion-deck", companion_deck)

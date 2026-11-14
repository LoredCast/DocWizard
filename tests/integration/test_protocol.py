"""Presence and adversarial protocol tests (§8.3, §8.7, §20)."""

import asyncio
import random
import sqlite3

from docwizard.collab.changeset import ChangeSet, u16len
from docwizard.projects import service as svc
from tests.integration.test_collab import connect, insert_at_end, new_project, open_editor


def insert_at(text, pos, ins):
    n = u16len(text)
    return ChangeSet.of([(pos, pos, ins)], n).to_json()


async def test_presence_is_mapped_and_shared(client, app):
    pid = await new_project(client)
    s1, s2 = await open_editor(client, pid), await open_editor(client, pid)
    a = await connect(app, client, s1)
    b = await connect(app, client, s2)
    doc, v = s1["doc"]["id"], s1["version"]
    joined = await a.recv_until("presence")
    assert joined["peer"]["sel"] is None and joined["peer"]["name"] == "Jane Doe"

    await a.send({"t": "presence", "doc": doc, "version": v, "sel": [[10, 12]]})
    got = await b.recv_until("presence")
    assert got["peer"]["sel"] == [[10, 12]] and got["version"] == v
    a_id = got["peer"]["id"]

    # B inserts five characters at the start; A's selection moves with the text.
    await b.send({"t": "push", "doc": doc, "version": v,
                  "updates": [{"clientID": s2["clientID"], "changes": insert_at(s2["text"], 0, "12345")}]})
    await b.recv_until("updates")
    # A still reports at the old version: the server maps it forward.
    await a.send({"t": "presence", "doc": doc, "version": v, "sel": [[0, 0]]})
    got = await b.recv_until("presence")
    assert got["version"] == v + 1 and got["peer"]["sel"] == [[0, 0]]
    await a.send({"t": "presence", "doc": doc, "version": v, "sel": [[11, 11]]})
    got = await b.recv_until("presence")
    assert got["peer"]["sel"] == [[16, 16]]

    # Out-of-range and future positions are dropped, not broadcast.
    await a.send({"t": "presence", "doc": doc, "version": v + 5, "sel": [[0, 0]]})
    await a.send({"t": "presence", "doc": doc, "version": v + 1, "sel": [[10**9, 0]]})
    await a.send({"t": "presence", "doc": doc, "version": v + 1, "sel": "nonsense"})

    # A third connection sees both, with A's selection at the current version.
    s3 = await open_editor(client, pid)
    c = await connect(app, client, s3)
    room = app.rooms.loaded(doc)
    peers = {p.id: p.public() for p in room.peers.values()}
    assert peers[a_id]["sel"] == [[16, 16]]

    await a.close()
    gone = await b.recv_until("presence")
    while not gone["peer"].get("gone"):
        gone = await b.recv_until("presence")
    assert gone["peer"] == {"id": a_id, "gone": True}
    await b.close()
    await c.close()


async def test_opened_lists_peers_with_colors(client, app):
    pid = await new_project(client)
    s1, s2 = await open_editor(client, pid), await open_editor(client, pid)
    a = await connect(app, client, s1)
    await a.send({"t": "presence", "doc": s1["doc"]["id"], "version": s1["version"], "sel": [[3, 3]]})
    await asyncio.sleep(0.05)
    from tests.asgi_client import WebSocket

    b = WebSocket(app, client)
    await b.connect()
    await b.send({"t": "open", "doc": s2["doc"]["id"], "version": s2["version"], "clientID": s2["clientID"]})
    opened = await b.recv()
    assert opened["t"] == "opened" and len(opened["peers"]) == 1
    peer = opened["peers"][0]
    assert peer["sel"] == [[3, 3]] and 0 <= peer["color"] < 8 and opened["peerId"] != peer["id"]
    await a.close()
    await b.close()


async def test_malformed_pushes_are_refused(client, app):
    pid = await new_project(client)
    s = await open_editor(client, pid)
    ws = await connect(app, client, s)
    doc, v, cid = s["doc"]["id"], s["version"], s["clientID"]
    n = u16len(s["text"])
    bad = [
        "not a list",
        [],
        [{"clientID": cid}],
        [{"clientID": cid, "changes": [n, [-1, "x"]]}],
        [{"clientID": cid, "changes": [n, [True, "x"]]}],
        [{"clientID": cid, "changes": [{"a": 1}]}],
        [{"clientID": cid, "changes": insert_at_end(s["text"], "x")}] * 101,
        [{"clientID": "c_someone_else", "changes": insert_at_end(s["text"], "x")}],
        # The first update is fine, the second doesn't fit: nothing is applied.
        [{"clientID": cid, "changes": insert_at_end(s["text"], "x")}, {"clientID": cid, "changes": [n]}],
    ]
    for updates in bad:
        await ws.send({"t": "push", "doc": doc, "version": v, "updates": updates})
        reply = await ws.recv_until("error")
        assert reply["code"] in ("invalid_update", "bad_request"), (updates, reply)
    room = app.rooms.loaded(doc)
    assert room.version == v and room.text() == s["text"]
    # Splitting a surrogate pair is refused.
    await ws.send({"t": "push", "doc": doc, "version": v,
                   "updates": [{"clientID": cid, "changes": insert_at_end(s["text"], "😀")}]})
    await ws.recv_until("updates")
    await ws.send({"t": "push", "doc": doc, "version": v + 1,
                   "updates": [{"clientID": cid, "changes": [n + 1, [0, "x"], 1]}]})
    assert (await ws.recv_until("error"))["code"] == "invalid_update"
    assert room.text().endswith("😀")
    await ws.close()


async def test_oversized_push_is_refused(client, app):
    app.cfg.collab.max_push_churn = 10
    pid = await new_project(client)
    s = await open_editor(client, pid)
    ws = await connect(app, client, s)
    await ws.send({"t": "push", "doc": s["doc"]["id"], "version": s["version"],
                   "updates": [{"clientID": s["clientID"], "changes": insert_at_end(s["text"], "x" * 11)}]})
    assert (await ws.recv_until("error"))["code"] == "too_large"
    await ws.close()


async def test_replayed_push_and_lost_ack(client, app):
    pid = await new_project(client)
    s = await open_editor(client, pid)
    doc, v = s["doc"]["id"], s["version"]
    push = {"t": "push", "doc": doc, "version": v,
            "updates": [{"clientID": s["clientID"], "changes": insert_at_end(s["text"], "once")}]}
    ws = await connect(app, client, s)
    await ws.send(push)
    # The connection drops before the client reads the broadcast.
    await asyncio.sleep(0.1)
    await ws.close()
    ws = await connect(app, client, s)
    # The retry carries the old version and is rejected; the update was not applied twice.
    await ws.send(push)
    assert (await ws.recv_until("rejected"))["version"] == v + 1
    room = app.rooms.loaded(doc)
    assert room.text().count("once") == 1
    # Reopening at the old version returns the client's own update, so it can confirm it.
    await ws.send({"t": "open", "doc": doc, "version": v, "clientID": s["clientID"]})
    up = await ws.recv_until("updates")
    assert up["from"] == v and up["updates"][0]["clientID"] == s["clientID"]
    await ws.close()


async def test_repeated_merge_id_is_answered_once(client, app):
    pid = await new_project(client)
    s = await open_editor(client, pid)
    ws = await connect(app, client, s)
    doc = s["doc"]["id"]
    merge = {"t": "merge", "doc": doc, "mergeId": "m_0123456789ab", "baseVersion": s["version"],
             "base": s["text"], "text": s["text"] + "\nOffline paragraph.\n"}
    await ws.send(merge)
    first = await ws.recv_until("merged")
    await ws.send(merge)
    second = await ws.recv_until("merged")
    assert first["version"] == second["version"]
    room = app.rooms.loaded(doc)
    assert room.text().count("Offline paragraph.") == 1
    await ws.close()


async def test_reads_wait_for_inflight_commit(client, app):
    """No render, snapshot or new connection sees text whose commit hasn't finished (§8.3 rule 2)."""
    pid = await new_project(client)
    s = await open_editor(client, pid)
    ws = await connect(app, client, s)
    doc = s["doc"]["id"]
    room = app.rooms.loaded(doc)
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    release = asyncio.Event()

    def slow(n):
        loop.call_soon_threadsafe(entered.set)
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result(5)

    app.db.before_commit = slow
    await ws.send({"t": "push", "doc": doc, "version": s["version"],
                   "updates": [{"clientID": s["clientID"], "changes": insert_at_end(s["text"], "pending")}]})
    await asyncio.wait_for(entered.wait(), 5)
    reader = asyncio.create_task(app.renderer.snapshot(pid))
    await asyncio.sleep(0.05)
    assert not reader.done()  # queued behind the push
    app.db.before_commit = None
    release.set()
    snap, _ = await asyncio.wait_for(reader, 5)
    assert snap.texts["paper.md"].endswith("pending")
    assert room.text().endswith("pending")
    await ws.close()


async def test_random_pushes_with_failures_never_diverge(client, app):
    """Several clients push at random with injected commit failures; everyone ends with the
    same text as the database, and every acknowledged edit is in it."""
    rng = random.Random(1234)
    pid = await new_project(client)
    snaps = [await open_editor(client, pid) for _ in range(3)]
    sockets = [await connect(app, client, s) for s in snaps]
    doc = snaps[0]["doc"]["id"]
    texts = [s["text"] for s in snaps]
    versions = [s["version"] for s in snaps]
    acknowledged = []

    def failing(n):
        if rng.random() < 0.25:
            raise sqlite3.OperationalError("injected failure")

    app.db.before_commit = failing
    for step in range(60):
        i = rng.randrange(3)
        marker = f"<{step}>"
        # Insert at line starts, so markers never split each other.
        starts = [0] + [k + 1 for k, ch in enumerate(texts[i]) if ch == "\n" and texts[i].isascii()]
        pos = rng.choice(starts)
        await sockets[i].send({"t": "push", "doc": doc, "version": versions[i], "updates": [
            {"clientID": snaps[i]["clientID"], "changes": insert_at(texts[i], pos, marker)}]})
        # Drain everything each client has received so far.
        for j in range(3):
            while True:
                try:
                    msg = await sockets[j].recv(timeout=0.03)
                except asyncio.TimeoutError:
                    break
                if msg["t"] == "updates" and msg["from"] == versions[j]:
                    for u in msg["updates"]:
                        texts[j] = ChangeSet.from_json(u["changes"]).apply(texts[j])
                        versions[j] += 1
                        if u["clientID"] == snaps[j]["clientID"]:
                            acknowledged += ["\n".join(part[1:]) for part in u["changes"]
                                             if isinstance(part, list) and len(part) > 1]
    app.db.before_commit = None
    room = app.rooms.loaded(doc)
    state = await app.db.read(lambda c: svc.load_state(c, doc))
    assert state.text == room.text() and state.version == room.version
    for j in range(3):
        assert versions[j] == room.version and texts[j] == room.text()
    for marker in acknowledged:
        assert marker in state.text
    assert acknowledged  # the test exercised successful pushes too
    for ws in sockets:
        await ws.close()

"""Document history: kept automatic snapshots, who edited, and comparing versions (§8.5)."""

import datetime as dt

from docwizard.projects import history
from tests.integration.test_collab import connect, insert_at_end, new_project, open_editor


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def test_thinning_keeps_recent_and_one_per_period():
    now = 1_000_000_000.0
    rows = []
    # Ten minutes ago, twice: both kept (the last hour keeps everything).
    rows += [{"id": 1, "created_at": iso(now - 600), "authors": ["Ann"]},
             {"id": 2, "created_at": iso(now - 300), "authors": ["Ben"]}]
    # Three within one hour, a day ago: only the last one stays and inherits the authors.
    base = (now - 86400) // 3600 * 3600
    old = [{"id": 10, "created_at": iso(base + 60), "authors": ["Ann"]},
           {"id": 11, "created_at": iso(base + 120), "authors": ["Cleo"]},
           {"id": 12, "created_at": iso(base + 180), "authors": ["Ann"]}]
    # Two on one day a month ago: one stays.
    day = (now - 30 * 86400) // 86400 * 86400
    older = [{"id": 20, "created_at": iso(day + 100), "authors": ["Dan"]},
             {"id": 21, "created_at": iso(day + 5000), "authors": []}]
    drop, merged = history.plan_thinning(older + old + rows, now)
    assert sorted(drop) == [10, 11, 20]
    assert merged == {12: ["Ann", "Cleo"], 21: ["Dan"]}


def test_compare_marks_words_and_skips_unchanged_lines():
    old = "".join(f"Line {i}.\n" for i in range(20)) + "The quick brown fox.\n"
    new = "".join(f"Line {i}.\n" for i in range(20)) + "The slow brown fox jumps.\n"
    diff = history.compare(old, new)
    ops = [s["op"] for s in diff["segments"]]
    assert ops[0] == "skip" and diff["segments"][0]["lines"] == 18
    assert {"op": "del", "text": "quick"} in diff["segments"]
    assert any(s["op"] == "ins" and "slow" in s["text"] for s in diff["segments"])
    assert diff["added"] == 2 and diff["removed"] == 1
    assert history.compare("same\n", "same\n") == {"segments": [{"op": "eq", "text": "same\n"}],
                                                  "added": 0, "removed": 0}


async def test_history_lists_autosaves_with_authors_and_compares(client, app):
    pid = await new_project(client)
    snap = await open_editor(client, pid)
    doc = snap["doc"]["id"]
    ws = await connect(app, client, snap)
    await ws.send({"t": "push", "doc": doc, "version": snap["version"],
                   "updates": [{"clientID": snap["clientID"], "changes": insert_at_end(snap["text"], "First draft.")}]})
    await ws.recv_until("updates")
    room = app.rooms.loaded(doc)
    await room.call(room.snapshot)
    first_text = room.text()
    await ws.send({"t": "push", "doc": doc, "version": room.version,
                   "updates": [{"clientID": snap["clientID"],
                                "changes": insert_at_end(first_text, " Second thoughts.")}]})
    await ws.recv_until("updates")
    await room.call(room.snapshot)
    await ws.close()

    data = (await client.get(f"/api/documents/{doc}/versions")).json()
    autos = data["history"]
    assert data["versions"] == []
    # Both autosaves are kept (the starter text's snapshot too, if any), newest first.
    assert len(autos) >= 2 and autos[0]["authors"] == ["Jane Doe"]
    older = next(v for v in autos if v["version"] == autos[1]["version"])
    text = (await client.get(f"/api/documents/{doc}/versions/{older['id']}")).json()["text"]
    assert text == first_text

    diff = (await client.get(f"/api/documents/{doc}/versions/{older['id']}/compare")).json()
    assert any(s["op"] == "ins" and "Second" in s["text"] for s in diff["segments"])
    assert diff["added"] == 2
    against = (await client.get(f"/api/documents/{doc}/versions/{older['id']}/compare?with={autos[0]['id']}")).json()
    assert against == diff
    assert (await client.get(f"/api/documents/{doc}/versions/999999/compare")).status == 404

    # Restoring an autosave works like restoring a named version.
    r = await client.post(f"/api/documents/{doc}/versions/{older['id']}/restore", json={})
    assert r.status == 200, r.text
    assert room.text() == first_text

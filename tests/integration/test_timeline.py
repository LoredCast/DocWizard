"""The drafting timeline (§8.5): points to scrub through, the text at each and who wrote what."""

from docwizard.collab.changeset import ChangeSet
from docwizard.projects import timeline
from tests.integration.test_collab import connect, insert_at_end, new_project, open_editor


async def test_timeline_replays_edits_with_authors(client, app):
    pid = await new_project(client)
    snap = await open_editor(client, pid)
    ws = await connect(app, client, snap)
    doc, v, text = snap["doc"]["id"], snap["version"], snap["text"]
    for word in ("Alpha", " beta"):
        await ws.send({"t": "push", "doc": doc, "version": v,
                       "updates": [{"clientID": snap["clientID"], "changes": insert_at_end(text, word)}]})
        await ws.recv_until("updates")
        text, v = text + word, v + 1
    await ws.close()

    r = await client.get(f"/api/documents/{doc}/timeline")
    assert r.status == 200, r.text
    data = r.json()
    assert data["version"] == v
    assert data["points"][-1]["version"] == v
    assert "Jane Doe" in data["points"][-1]["authors"]
    assert data["people"][0]["name"] == "Jane Doe" and data["people"][0]["inserted"] == len("Alpha beta")

    # The text at the start, and at the end with what Jane wrote since then.
    r = await client.get(f"/api/documents/{doc}/timeline/{snap['version']}")
    assert r.json()["text"] == snap["text"]
    r = await client.get(f"/api/documents/{doc}/timeline/{v}?since={snap['version']}")
    out = r.json()
    assert out["text"] == text and out["exact"]
    assert out["marks"] == [{"from": len(snap["text"]), "to": len(text), "author": "Jane Doe"}]
    assert (await client.get(f"/api/documents/{doc}/timeline/x")).status == 400


def test_inserted_word_ranges():
    assert timeline.inserted("One two.\n", "One new two.\nAnd more.\n") == [(4, 7), (13, 22)]
    # UTF-16 offsets, as the editor counts.
    assert timeline.inserted("😀 a\n", "😀 a b\n") == [(5, 6)]


def test_points_merge_when_too_many():
    pts = [{"version": i, "kind": "edit", "authors": ["A"]} for i in range(timeline.MAX_POINTS + 50)]
    pts.append({"version": 10_000, "kind": "snapshot", "authors": [], "label": "Draft"})
    out = timeline._merge_oldest(pts)
    assert len(out) <= timeline.MAX_POINTS + 1 and out[-1]["label"] == "Draft"
    assert [p["version"] for p in out] == sorted(p["version"] for p in out)


def test_map_marks_through_a_change():
    cs = ChangeSet.of([(0, 0, "xx")], 10)
    assert timeline._map_marks([{"from": 2, "to": 4, "author": "A"}], cs) == [{"from": 4, "to": 6, "author": "A"}]

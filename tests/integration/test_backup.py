"""Backup taken while edits run, then restore into a fresh data directory (§11.4)."""

import asyncio
import copy

from tests.integration.test_collab import connect, insert_at_end, new_project, open_editor


async def test_backup_during_edits_and_restore(client, app, cfg, tmp_path):
    from docwizard.projects import backup

    pid = await new_project(client)
    await client.request("PUT", f"/api/projects/{pid}/assets/fig/map.png", body=b"\x89PNG fake image")
    s = await open_editor(client, pid)
    ws = await connect(app, client, s)
    doc = s["doc"]["id"]
    text, version = s["text"], s["version"]

    async def typing():
        nonlocal text, version
        for i in range(20):
            await ws.send({"t": "push", "doc": doc, "version": version,
                           "updates": [{"clientID": s["clientID"], "changes": insert_at_end(text, str(i % 10))}]})
            await ws.recv_until("updates")
            text += str(i % 10)
            version += 1

    dest = tmp_path / "backup"
    edits = asyncio.create_task(typing())
    await asyncio.sleep(0.01)
    await asyncio.to_thread(backup.backup, cfg, dest)
    await edits
    await ws.close()

    restored = copy.deepcopy(cfg)
    restored.server.data_dir = str(tmp_path / "restored")
    await asyncio.to_thread(backup.restore, restored, dest)
    folder = restored.data_dir / "projects" / pid
    restored_text = (folder / "paper.md").read_text()
    # Every edit committed before the backup is present, in order.
    assert text.startswith(restored_text)
    assert restored_text.startswith(s["text"])
    assert (folder / "fig" / "map.png").read_bytes() == b"\x89PNG fake image"
    assert (folder / "references.json").exists()

"""The render worker's protocol loop (§9.2)."""

import asyncio
import json
import socket

import docwizard_worker


async def test_crashed_job_still_gets_a_reply(tmp_path, monkeypatch):
    worker = docwizard_worker.Worker(tmp_path / "sock", tmp_path / "jobs", tmp_path / "cache", 1, 61000)

    async def boom(job_id):
        raise RuntimeError("preexec failed")

    monkeypatch.setattr(worker, "run_job", boom)
    ours, theirs = socket.socketpair()
    reader, writer = await asyncio.open_connection(sock=theirs)
    serving = asyncio.create_task(worker.handle(reader, writer))
    r, w = await asyncio.open_connection(sock=ours)
    w.write(json.dumps({"run": "j1"}).encode() + b"\n")
    await w.drain()
    reply = json.loads(await asyncio.wait_for(r.readline(), 5))
    assert reply["done"] == "j1" and reply["status"] == "error" and reply["diagnostics"]
    w.close()
    await asyncio.wait_for(serving, 5)

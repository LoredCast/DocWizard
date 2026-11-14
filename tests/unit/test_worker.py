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


def _limits_seen_by_child(tmp_path, limits):
    import subprocess
    import sys

    preexec = docwizard_worker._make_preexec(None, None, limits, tmp_path)
    code = "import resource; print(resource.getrlimit(resource.RLIMIT_DATA)[0], resource.getrlimit(resource.RLIMIT_AS)[0])"
    out = subprocess.run([sys.executable, "-c", code], preexec_fn=preexec, capture_output=True, text=True, check=True)
    return [int(v) for v in out.stdout.split()]


def test_memory_is_capped_without_an_address_space_limit(tmp_path):
    """Threaded pandoc reserves address space at startup, so RLIMIT_AS breaks it (§9.2)."""
    import resource

    data, address_space = _limits_seen_by_child(tmp_path, {"memory_mb": 300})
    assert data == 300 * 1024 * 1024
    assert address_space == resource.getrlimit(resource.RLIMIT_AS)[0]


def test_jobs_over_the_memory_limit_fail(tmp_path):
    import subprocess
    import sys

    preexec = docwizard_worker._make_preexec(None, None, {"memory_mb": 64}, tmp_path)
    r = subprocess.run([sys.executable, "-c", "x = bytearray(200 * 1024 * 1024)"], preexec_fn=preexec,
                       capture_output=True, text=True)
    assert r.returncode != 0 and "MemoryError" in r.stderr

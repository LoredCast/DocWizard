"""Several render workers (§9.2): jobs go to the least busy one, one that doesn't answer is left out."""

import asyncio
from pathlib import Path

import pytest

from docwizard.render.worker_client import WorkerClient, WorkerPool, WorkerUnavailable


def pool(tmp_path: Path, n: int) -> WorkerPool:
    return WorkerPool([WorkerClient(tmp_path / f"w{i}" / "worker.sock", tmp_path / f"w{i}") for i in range(n)])


async def test_least_busy_worker_takes_the_job(tmp_path):
    p = pool(tmp_path, 3)
    async with p.lease() as a:
        async with p.lease() as b:
            async with p.lease() as c:
                assert {a, b, c} == set(p.members)
                assert a.run_dir == tmp_path / "w0" / "run"
        async with p.lease() as d:
            assert d is p.members[1]  # a is still busy; the first idle one
    assert all(m.inflight == 0 for m in p.members)


async def test_unreachable_worker_is_left_out(tmp_path):
    p = pool(tmp_path, 2)
    with pytest.raises(WorkerUnavailable):
        async with p.lease() as w:
            assert w is p.members[0]
            await w.run("j_" + "0" * 32, 1)  # nothing listens on the socket
    async with p.lease() as w:
        assert w is p.members[1]
    # hello() fails only when no worker answers at all.
    with pytest.raises(WorkerUnavailable):
        await asyncio.wait_for(p.hello(), 5)
    assert [s["up"] for s in p.status()] == [False, False]


def test_from_config(tmp_path):
    from docwizard import config
    cfg = config.load(env={"DOCWIZARD_SERVER_JOBS_DIR": str(tmp_path / "jobs"),
                           "DOCWIZARD_RENDER_EXTRA_WORKERS": f"{tmp_path / 'jobs2'},{tmp_path / 'jobs3'}"})
    p = WorkerPool.from_config(cfg)
    assert len(p) == 3
    assert p.members[0].socket_path == tmp_path / "jobs" / "worker.sock"
    assert p.members[2].socket_path == tmp_path / "jobs3" / "worker.sock"

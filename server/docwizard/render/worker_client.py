"""The only module that talks to the render workers (§9.2).

One worker is the usual setup. More can be attached (``[render] extra_workers``): each has its own
jobs directory with its socket in it, ``<jobs>/worker.sock``, on this machine or on shared storage
with another one. Jobs go to the worker with the fewest jobs in flight; a worker that doesn't answer
is left out for a while, and the others take its jobs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("docwizard.render")

WORKER_SCRIPT = Path(__file__).resolve().parents[3] / "worker" / "docwizard_worker.py"


class WorkerUnavailable(Exception):
    pass


# How long a worker that didn't answer is left out before it gets jobs again.
DOWN_SECONDS = 15.0


class WorkerClient:
    def __init__(self, socket_path: Path, jobs_dir: Path | None = None):
        self.socket_path = socket_path
        self.jobs_dir = jobs_dir if jobs_dir is not None else socket_path.parent
        self.versions: dict = {}
        self.inflight = 0
        self.down_until = 0.0
        self._proc: subprocess.Popen | None = None

    @property
    def run_dir(self) -> Path:
        return self.jobs_dir / "run"

    async def _connect(self):
        try:
            return await asyncio.open_unix_connection(str(self.socket_path), limit=1 << 20)
        except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
            raise WorkerUnavailable(f"The render worker is not reachable ({exc.__class__.__name__}).") from None

    async def hello(self) -> dict:
        reader, writer = await self._connect()
        try:
            writer.write(b'{"hello": 1}\n')
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), 30)
            self.versions = json.loads(line).get("versions", {})
            self.down_until = 0.0
            return self.versions
        finally:
            writer.close()

    async def run(self, job_id: str, timeout: float) -> dict:
        reader, writer = await self._connect()
        try:
            writer.write(json.dumps({"run": job_id}).encode() + b"\n")
            await writer.drain()
            try:
                line = await asyncio.wait_for(reader.readline(), timeout + 30)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                await self.cancel(job_id)
                raise
            if not line:
                raise WorkerUnavailable("The render worker closed the connection.")
            return json.loads(line)
        finally:
            writer.close()

    async def cancel(self, job_id: str) -> None:
        try:
            reader, writer = await self._connect()
        except WorkerUnavailable:
            return
        try:
            writer.write(json.dumps({"cancel": job_id}).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()

    # -- development: run the worker as a child process ----------------------
    async def spawn(self, jobs_dir: Path, cache_dir: Path, concurrency: int, uid_base: int | None = None) -> None:
        self._proc = subprocess.Popen(
            [sys.executable, str(WORKER_SCRIPT), "--socket", str(self.socket_path), "--jobs", str(jobs_dir),
             "--cache", str(cache_dir), "--concurrency", str(concurrency),
             *(["--uid-base", str(uid_base)] if uid_base is not None else [])],
            stdout=None, stderr=None, env={**os.environ})
        for _ in range(100):
            if self.socket_path.exists():
                try:
                    await self.hello()
                    return
                except (WorkerUnavailable, ValueError, OSError):
                    pass
            await asyncio.sleep(0.05)
        raise WorkerUnavailable("The render worker didn't start.")

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None


class WorkerPool:
    """The render workers: a primary one and any extra ones, with the WorkerClient interface for
    callers that only need versions or a health check. Jobs take a worker with ``lease()``."""

    def __init__(self, members: list[WorkerClient]):
        if not members:
            raise ValueError("a worker pool needs at least one worker")
        self.members = members

    @classmethod
    def from_config(cls, cfg) -> "WorkerPool":
        members = [WorkerClient(cfg.worker_socket, cfg.jobs_dir)]
        for entry in cfg.render.extra_workers:
            jobs_dir = Path(entry)
            members.append(WorkerClient(jobs_dir / "worker.sock", jobs_dir))
        return cls(members)

    def __len__(self) -> int:
        return len(self.members)

    @property
    def primary(self) -> WorkerClient:
        return self.members[0]

    @property
    def versions(self) -> dict:
        """The tools of the first worker that answered; they are part of every render's cache key."""
        for m in self.members:
            if m.versions:
                return m.versions
        return {}

    @property
    def socket_path(self) -> Path:
        return self.primary.socket_path

    async def hello(self) -> dict:
        """Ask every worker; fine as long as one answers. Workers with other tool versions than the
        first are reported, since their outputs would share cache entries."""
        results = await asyncio.gather(*(m.hello() for m in self.members), return_exceptions=True)
        answered = [(m, r) for m, r in zip(self.members, results) if not isinstance(r, BaseException)]
        for m, r in zip(self.members, results):
            if isinstance(r, BaseException):
                m.down_until = time.monotonic() + DOWN_SECONDS
        if not answered:
            raise WorkerUnavailable("No render worker is reachable.")
        first = answered[0][1]
        for m, r in answered[1:]:
            if r != first:
                log.warning("render workers run different tool versions", extra={"socket": str(m.socket_path)})
        return first

    def pick(self) -> WorkerClient:
        """The worker with the fewest jobs in flight among those not left out; the primary if all are."""
        now = time.monotonic()
        up = [m for m in self.members if m.down_until <= now] or self.members
        return min(up, key=lambda m: (m.inflight, self.members.index(m)))

    @contextlib.asynccontextmanager
    async def lease(self):
        """A worker for one job: write the job into its ``run_dir`` and ``run`` it there."""
        member = self.pick()
        member.inflight += 1
        try:
            yield member
        except WorkerUnavailable:
            member.down_until = time.monotonic() + DOWN_SECONDS
            raise
        finally:
            member.inflight -= 1

    async def run(self, job_id: str, timeout: float) -> dict:
        """Run a job written into the primary worker's jobs directory (callers that predate the pool)."""
        return await self.primary.run(job_id, timeout)

    async def cancel(self, job_id: str) -> None:
        await asyncio.gather(*(m.cancel(job_id) for m in self.members), return_exceptions=True)

    def status(self) -> list[dict]:
        now = time.monotonic()
        return [{"socket": str(m.socket_path), "jobs": str(m.jobs_dir), "inflight": m.inflight,
                 "up": bool(m.versions) and m.down_until <= now} for m in self.members]

    async def spawn(self, cache_dir: Path, concurrency: int) -> None:
        """Development: every worker as a child process, each on its own jobs directory."""
        for i, m in enumerate(self.members):
            (m.jobs_dir / "run").mkdir(parents=True, exist_ok=True)
            await m.spawn(m.jobs_dir, cache_dir, concurrency, uid_base=None if i == 0 else 20000 + 100 * i)

    def stop(self) -> None:
        for m in self.members:
            m.stop()

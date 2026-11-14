"""The only module that talks to the render worker (§9.2)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("docwizard.render")

WORKER_SCRIPT = Path(__file__).resolve().parents[3] / "worker" / "docwizard_worker.py"


class WorkerUnavailable(Exception):
    pass


class WorkerClient:
    def __init__(self, socket_path: Path):
        self.socket_path = socket_path
        self.versions: dict = {}
        self._proc: subprocess.Popen | None = None

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
    async def spawn(self, jobs_dir: Path, cache_dir: Path, concurrency: int) -> None:
        self._proc = subprocess.Popen(
            [sys.executable, str(WORKER_SCRIPT), "--socket", str(self.socket_path), "--jobs", str(jobs_dir),
             "--cache", str(cache_dir), "--concurrency", str(concurrency)],
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

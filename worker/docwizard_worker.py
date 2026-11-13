#!/usr/bin/env python3
"""DocWizard render worker (§9.2). Standard library only.

Listens on a Unix socket for newline-delimited JSON:

    {"run": "<job id>"}     run jobs/run/<id>/job.json
    {"cancel": "<job id>"}  stop a running job
    {"hello": 1}            report tool versions

and answers ``{"done": "<id>", "status": "ok|error|timeout|cancelled",
"diagnostics": [...]}``.

The job is the isolation boundary. When the worker runs as root it takes a
Unix user from a pool (one per concurrency slot), gives the job directory to
that user with mode 0700 and runs every tool as that user with
``no_new_privs`` and resource limits. When the job ends it kills every process
of that user, removes the user's files from /tmp and hands the job directory
back. Without root (development) jobs run as the worker's own user with the
same limits.

The worker shares no code with the app; only the job format is shared.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import re
import resource
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

ALLOWED_TOOLS = {"pandoc", "typst", "latexmk"}
JOB_ID = re.compile(r"^j_[0-9a-f]{32}$")
PR_SET_NO_NEW_PRIVS = 38
MAX_DIAG = 50


def log(**kw) -> None:
    kw.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    kw.setdefault("logger", "docwizard.worker")
    print(json.dumps(kw), flush=True)


def _no_new_privs() -> None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    except OSError:
        pass


def _make_preexec(uid: int | None, gid: int | None, limits: dict):
    cpu = int(limits.get("cpu_seconds", 60))
    mem = int(limits.get("memory_mb", 1536)) * 1024 * 1024
    fsize = int(limits.get("file_mb", 512)) * 1024 * 1024
    nproc = int(limits.get("processes", 64))

    def preexec() -> None:
        os.setsid()
        _no_new_privs()
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if uid is not None:
            # Process limits count per user, so they count per job.
            resource.setrlimit(resource.RLIMIT_NPROC, (nproc, nproc))
            os.setgroups([])
            os.setgid(gid)
            os.setuid(uid)

    return preexec


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    """lchown every entry below *path* (never follows symlinks)."""
    os.lchown(path, uid, gid)
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in dirnames + filenames:
            os.lchown(os.path.join(dirpath, name), uid, gid)


def _kill_user(uid: int) -> None:
    """Kill every process of *uid*: become that user and signal -1."""
    try:
        subprocess.run([sys.executable, "-I", "-c", "import os, signal; os.kill(-1, signal.SIGKILL)"],
                       user=uid, group=uid, extra_groups=[], timeout=10, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass


def _clean_tmp(uid: int) -> None:
    for entry in Path("/tmp").iterdir():
        try:
            if entry.lstat().st_uid == uid:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
        except OSError:
            pass


_TYPST_ERR = re.compile(r"^(error|warning): (.+)$")
_TYPST_LOC = re.compile(r"┌─ (.+?):(\d+):(\d+)")


def parse_diagnostics(tool: str, stderr: str) -> list[dict]:
    diags: list[dict] = []
    lines = stderr.splitlines()
    if tool == "typst":
        for i, line in enumerate(lines):
            m = _TYPST_ERR.match(line.strip())
            if not m:
                continue
            d = {"tool": "typst", "severity": m.group(1), "message": m.group(2)}
            for nxt in lines[i + 1:i + 3]:
                loc = _TYPST_LOC.search(nxt)
                if loc:
                    d.update(file=os.path.basename(loc.group(1)), line=int(loc.group(2)), col=int(loc.group(3)))
                    break
            diags.append(d)
    else:
        for line in lines:
            text = line.strip()
            if not text:
                continue
            severity = "warning" if text.startswith("[WARNING]") else "error"
            diags.append({"tool": tool, "severity": severity, "message": text.removeprefix("[WARNING] ")})
    return diags[:MAX_DIAG]


class Worker:
    def __init__(self, socket_path: Path, jobs_dir: Path, cache_dir: Path, concurrency: int, uid_base: int):
        self.socket_path = socket_path
        self.jobs_dir = jobs_dir
        self.cache_dir = cache_dir
        self.root = os.geteuid() == 0
        self.slots: asyncio.Queue = asyncio.Queue()
        for i in range(concurrency):
            self.slots.put_nowait(uid_base + i)
        self.running: dict[str, asyncio.subprocess.Process] = {}
        self.cancelled: set[str] = set()
        if not self.root:
            log(level="warning", msg="not running as root: jobs run as the worker's own user (development mode)")

    def versions(self) -> dict:
        out = {}
        for tool in sorted(ALLOWED_TOOLS):
            path = shutil.which(tool)
            if not path:
                continue
            try:
                res = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
                out[tool] = (res.stdout or res.stderr).splitlines()[0].strip()
            except (OSError, subprocess.SubprocessError, IndexError):
                out[tool] = "unknown"
        return out

    async def run_job(self, job_id: str) -> dict:
        if not JOB_ID.match(job_id):
            return {"done": job_id, "status": "error", "diagnostics": [{"severity": "error", "message": "bad id"}]}
        job_dir = self.jobs_dir / "run" / job_id
        try:
            st = os.lstat(job_dir)
            if not stat.S_ISDIR(st.st_mode):
                raise OSError("not a directory")
            job = json.loads((job_dir / "job.json").read_text())
        except (OSError, ValueError) as exc:
            return {"done": job_id, "status": "error", "diagnostics": [{"severity": "error", "message": str(exc)}]}
        owner_uid, owner_gid = st.st_uid, st.st_gid
        uid = await self.slots.get()
        run_uid = uid if self.root else None
        try:
            if self.root:
                _chown_tree(job_dir, uid, uid)
                os.chmod(job_dir, 0o700)
            return await self._steps(job_id, job_dir, job, run_uid)
        finally:
            self.running.pop(job_id, None)
            if self.root:
                _kill_user(uid)
                _clean_tmp(uid)
                _chown_tree(job_dir, owner_uid, owner_gid)
            self.cancelled.discard(job_id)
            self.slots.put_nowait(uid)

    async def _steps(self, job_id: str, job_dir: Path, job: dict, uid: int | None) -> dict:
        limits = job.get("limits", {})
        wall = float(limits.get("wall_seconds", 90))
        deadline = time.monotonic() + wall
        home = job_dir / "home"
        tmp = job_dir / "tmp"
        for d in (home, tmp, job_dir / "out"):
            d.mkdir(exist_ok=True)
            if uid is not None:
                os.lchown(d, uid, uid)
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(home), "TMPDIR": str(tmp), "TEXMFVAR": str(home / "texmf-var"),
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "LUA_PATH": job.get("lua_path", ""), "LUA_PATH_5_4": job.get("lua_path", ""),
            "LUA_CPATH": "", "LUA_CPATH_5_4": "",
            "openin_any": "p", "openout_any": "p",
            "TYPST_PACKAGE_CACHE_PATH": str(self.cache_dir / "typst-packages"),
        }
        diagnostics: list[dict] = []
        for step in job.get("steps", []):
            tool, args = step.get("tool"), step.get("args", [])
            if tool not in ALLOWED_TOOLS or not all(isinstance(a, str) for a in args):
                return {"done": job_id, "status": "error",
                        "diagnostics": [{"severity": "error", "message": f"tool not allowed: {tool}"}]}
            exe = shutil.which(tool)
            if exe is None:
                return {"done": job_id, "status": "error",
                        "diagnostics": [{"severity": "error", "message": f"{tool} is not installed in the worker"}]}
            cwd = job_dir / step.get("cwd", "src")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"done": job_id, "status": "timeout", "diagnostics": diagnostics}
            proc = await asyncio.create_subprocess_exec(
                exe, *args, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                preexec_fn=_make_preexec(uid, uid, limits), close_fds=True)
            self.running[job_id] = proc
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=remaining)
            except asyncio.TimeoutError:
                _killpg(proc)
                await proc.wait()
                return {"done": job_id, "status": "timeout", "diagnostics": diagnostics + [
                    {"severity": "error", "message": f"The render took longer than {int(wall)} s and was stopped."}]}
            if job_id in self.cancelled:
                return {"done": job_id, "status": "cancelled", "diagnostics": []}
            if step.get("stdout"):
                out_path = job_dir / step["stdout"]
                out_path.write_bytes(stdout)
            diagnostics += parse_diagnostics(tool, stderr.decode("utf-8", "replace"))
            if proc.returncode != 0:
                if not any(d["severity"] == "error" for d in diagnostics):
                    diagnostics.append({"tool": tool, "severity": "error",
                                        "message": f"{tool} failed with exit code {proc.returncode}"})
                return {"done": job_id, "status": "error", "diagnostics": diagnostics[:MAX_DIAG]}
        return {"done": job_id, "status": "ok", "diagnostics": diagnostics[:MAX_DIAG]}

    def cancel(self, job_id: str) -> None:
        proc = self.running.get(job_id)
        if proc is not None:
            self.cancelled.add(job_id)
            _killpg(proc)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        tasks: set[asyncio.Task] = set()
        lock = asyncio.Lock()

        async def reply(message: dict) -> None:
            async with lock:
                writer.write(json.dumps(message).encode() + b"\n")
                await writer.drain()

        async def run(job_id: str) -> None:
            result = await self.run_job(job_id)
            log(level="info", msg="job finished", job=job_id, status=result["status"])
            try:
                await reply(result)
            except (ConnectionError, OSError):
                pass

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if "run" in msg:
                    task = asyncio.create_task(run(str(msg["run"])))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                elif "cancel" in msg:
                    self.cancel(str(msg["cancel"]))
                elif "hello" in msg:
                    await reply({"hello": 1, "versions": await asyncio.to_thread(self.versions)})
        finally:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            writer.close()

    async def serve(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        (self.jobs_dir / "run").mkdir(parents=True, exist_ok=True)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        old = os.umask(0o177)  # socket mode 0600
        try:
            server = await asyncio.start_unix_server(self.handle, path=str(self.socket_path))
        finally:
            os.umask(old)
        log(level="info", msg="worker listening", socket=str(self.socket_path), root=self.root)
        async with server:
            await server.serve_forever()


def _killpg(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="DocWizard render worker")
    parser.add_argument("--socket", default=os.environ.get("DOCWIZARD_WORKER_SOCKET",
                                                           "/var/lib/docwizard-jobs/worker.sock"))
    parser.add_argument("--jobs", default=os.environ.get("DOCWIZARD_JOBS_DIR", "/var/lib/docwizard-jobs"))
    parser.add_argument("--cache", default=os.environ.get("DOCWIZARD_CACHE_DIR", "/var/lib/docwizard-cache"))
    parser.add_argument("--concurrency", type=int, default=int(os.environ.get("DOCWIZARD_WORKER_CONCURRENCY", "2")))
    parser.add_argument("--uid-base", type=int, default=20000)
    args = parser.parse_args(argv)
    worker = Worker(Path(args.socket), Path(args.jobs), Path(args.cache), args.concurrency, args.uid_base)
    try:
        asyncio.run(worker.serve())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()

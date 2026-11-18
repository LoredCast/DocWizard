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
# Job directories older than this at startup belong to jobs a restart interrupted.
ORPHAN_AGE = 60
# kpathsea settings for TeX (§9.6), in a texmf.cnf of the job's own that kpathsea reads before the
# system one: no reading or writing outside the job directory (TEXMFOUTPUT) or of dot files, and no
# shell escape. The same values are also set in the environment, which takes precedence.
TEXMF_CNF = "% Written by the DocWizard render worker for this job.\nopenin_any = p\nopenout_any = p\nshell_escape = f\n"
SHARED_FONTS_DIR = "/usr/local/share/docwizard/fonts"
# Removes symlinks that lead out of the job directory, run as the job's user before Typst and TeX
# read the files: Typst's --root and kpathsea's paranoid mode check paths, not where links go.
SCRUB_LINKS = """
import os, sys
root = os.path.realpath(sys.argv[1])
for base, dirs, files in os.walk(root):
    for name in dirs + files:
        path = os.path.join(base, name)
        if os.path.islink(path):
            target = os.path.realpath(path)
            if target != root and not target.startswith(root + os.sep):
                os.unlink(path)
                print(os.path.relpath(path, root))
"""
# Upstream pandoc embeds lpeg; distro packages that exclude it load lpeg.so and re.lua via LUA_*PATH.
_SYS_LUA_RE = "/usr/share/lua/5.4/re.lua"
_SYS_LUA_CPATHS = (
    ("/usr/lib/lua/5.4/lpeg.so", "/usr/lib/lua/5.4/?.so"),
    ("/usr/lib64/lua/5.4/lpeg.so", "/usr/lib64/lua/5.4/?.so"),
    ("/usr/lib/x86_64-linux-gnu/lua/5.4/lpeg.so", "/usr/lib/x86_64-linux-gnu/lua/5.4/?.so"),
    ("/usr/lib/aarch64-linux-gnu/lua/5.4/lpeg.so", "/usr/lib/aarch64-linux-gnu/lua/5.4/?.so"),
)


def _lua_env(job_lua_path: str) -> tuple[str, str]:
    """Build LUA_PATH and LUA_CPATH without leaking './' or project directories."""
    lua_paths = [p for p in job_lua_path.split(";") if p]
    lua_cpath = next((pattern for probe, pattern in _SYS_LUA_CPATHS if os.path.exists(probe)), "")
    if lua_cpath and os.path.exists(_SYS_LUA_RE):
        lua_paths.append("/usr/share/lua/5.4/?.lua")
    return ";".join(lua_paths), lua_cpath


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


def _make_preexec(uid: int | None, gid: int | None, limits: dict, cwd: Path):
    cpu = int(limits.get("cpu_seconds", 60))
    mem = int(limits.get("memory_mb", 1536)) * 1024 * 1024
    fsize = int(limits.get("file_mb", 512)) * 1024 * 1024
    nproc = int(limits.get("processes", 64))

    def preexec() -> None:
        os.setsid()
        _no_new_privs()
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))
        # Memory is capped with RLIMIT_DATA, not RLIMIT_AS. Pandoc's runtime reserves as
        # much address space as it can get at startup without using it; under an
        # address-space limit nothing is left for thread stacks, and a threaded pandoc
        # fails with "failed to create OS thread". Reserved but unused memory doesn't
        # count against RLIMIT_DATA, while everything the job actually allocates does.
        resource.setrlimit(resource.RLIMIT_DATA, (mem, mem))
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if uid is not None:
            # Process limits count per user, so they count per job.
            resource.setrlimit(resource.RLIMIT_NPROC, (nproc, nproc))
            os.setgroups([])
            os.setgid(gid)
            os.setuid(uid)
        # After setuid: without CAP_DAC_OVERRIDE the supervisor can't enter the job's directories.
        os.chdir(cwd)

    return preexec


def _chown_tree(path: Path | str, uid: int, gid: int) -> None:
    """lchown *path* and everything below it, never following symlinks.

    The supervisor has CAP_CHOWN but not CAP_DAC_OVERRIDE, so it can only list a
    directory it owns: each directory becomes root's first, then gets its final owner
    once its entries are done.
    """
    os.lchown(path, 0, 0)
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                _chown_tree(entry.path, uid, gid)
            else:
                os.lchown(entry.path, uid, gid)
    os.lchown(path, uid, gid)


def _write_into(path: Path, data: bytes, uid: int | None) -> None:
    """Write a file into a job directory, which the supervisor may not own."""
    if uid is None:
        path.write_bytes(data)
        return
    parent = path.parent
    os.lchown(parent, 0, 0)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.lchown(path, uid, uid)
    finally:
        os.lchown(parent, uid, uid)


def _kill_user(uid: int) -> None:
    """Kill every process of *uid*: become that user and signal -1."""
    try:
        subprocess.run([sys.executable, "-I", "-c", "import os, signal; os.kill(-1, signal.SIGKILL)"],
                       user=uid, group=uid, extra_groups=[], timeout=10, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass


def _remove_tree(path: Path, root: bool) -> None:
    """Delete a job directory a job's user may still own. As root without CAP_DAC_OVERRIDE the
    supervisor first takes every directory over (CAP_CHOWN, CAP_FOWNER) so it can list and empty it."""
    if root:
        def take(p: str) -> None:
            os.lchown(p, 0, 0)
            os.chmod(p, 0o700)
            with os.scandir(p) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        take(entry.path)

        try:
            take(str(path))
        except OSError:
            pass
    shutil.rmtree(path, ignore_errors=True)


def _scrub_links(job_dir: Path, uid: int | None) -> list[str]:
    """Remove symlinks leading out of *job_dir*; returns the ones removed."""
    try:
        res = subprocess.run([sys.executable, "-I", "-c", SCRUB_LINKS, str(job_dir)], capture_output=True,
                             text=True, timeout=30, **({"user": uid, "group": uid, "extra_groups": []} if uid else {}))
    except (OSError, subprocess.SubprocessError):
        return ["?"]
    if res.returncode != 0:
        return ["?"]
    return [line for line in res.stdout.splitlines() if line]


def _inside(path: str, roots: list[str]) -> bool:
    real = os.path.realpath(path)
    return any(real == r or real.startswith(r.rstrip(os.sep) + os.sep) for r in roots)


def _links_out(folder: str) -> bool:
    """Whether a font folder has a symlink leading outside it."""
    top = os.path.realpath(folder)
    for base, dirs, files in os.walk(top):
        for name in dirs + files:
            path = os.path.join(base, name)
            if os.path.islink(path) and not _inside(path, [top]):
                return True
    return False


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
_TEX_FILE_LINE = re.compile(r"^(?:\./)?([^:\s]+\.(?:tex|sty|cls)):(\d+): (.+)$")
_TEX_WARNING = re.compile(r"^(LaTeX|Package \w+) Warning: .*(undefined|multiply defined)")


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
    elif tool == "latexmk":
        seen = set()
        for i, line in enumerate(lines):
            m = _TEX_FILE_LINE.match(line.strip())
            if m:
                d = {"tool": "latex", "severity": "error", "file": os.path.basename(m.group(1)),
                     "line": int(m.group(2)), "message": m.group(3).strip()}
            elif line.startswith("! "):
                d = {"tool": "latex", "severity": "error", "message": line[2:].strip()}
                for nxt in lines[i + 1:i + 12]:
                    loc = re.match(r"^l\.(\d+)", nxt)
                    if loc:
                        d["line"] = int(loc.group(1))
                        break
            elif _TEX_WARNING.search(line):
                d = {"tool": "latex", "severity": "warning", "message": line.strip()}
            else:
                continue
            key = (d.get("line"), d["message"])
            if key not in seen:
                seen.add(key)
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
        except OSError as exc:
            return {"done": job_id, "status": "error", "diagnostics": [{"severity": "error", "message": str(exc)}]}
        owner_uid, owner_gid = st.st_uid, st.st_gid
        uid = await self.slots.get()
        run_uid = uid if self.root else None
        try:
            if self.root:
                _chown_tree(job_dir, 0, 0)
            try:
                job = json.loads((job_dir / "job.json").read_text())
            except (OSError, ValueError) as exc:
                return {"done": job_id, "status": "error",
                        "diagnostics": [{"severity": "error", "message": str(exc)}]}
            for d in ("home", "tmp", "out", "texmf-cnf"):
                (job_dir / d).mkdir(exist_ok=True)
            cnf = job_dir / "texmf-cnf" / "texmf.cnf"
            cnf.unlink(missing_ok=True)
            cnf.write_text(TEXMF_CNF)
            if self.root:
                _chown_tree(job_dir, uid, uid)
                # The job's own texmf.cnf stays the supervisor's: readable, not writable, by the job.
                for p, mode in ((job_dir / "texmf-cnf", 0o755), (cnf, 0o644)):
                    os.lchown(p, 0, 0)
                    os.chmod(p, mode)
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
        lua_path, lua_cpath = _lua_env(job.get("lua_path", ""))
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(home), "TMPDIR": str(tmp), "TEXMFVAR": str(home / "texmf-var"),
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "LUA_PATH": lua_path, "LUA_PATH_5_4": lua_path,
            "LUA_CPATH": lua_cpath, "LUA_CPATH_5_4": lua_cpath,
            "openin_any": "p", "openout_any": "p", "shell_escape": "f",
            # The job's texmf.cnf first; the trailing colon adds the system's default search path.
            "TEXMFCNF": f"{job_dir}/texmf-cnf:",
            "TYPST_PACKAGE_CACHE_PATH": str(self.cache_dir / "typst-packages"),
            "TEXMFOUTPUT": str(job_dir),
        }
        diagnostics: list[dict] = []
        for step in job.get("steps", []):
            tool, args = step.get("tool"), step.get("args", [])
            if tool not in ALLOWED_TOOLS or not all(isinstance(a, str) for a in args):
                return {"done": job_id, "status": "error",
                        "diagnostics": [{"severity": "error", "message": f"tool not allowed: {tool}"}]}
            args = [a.replace("{job}", str(job_dir)) for a in args]
            exe = shutil.which(tool)
            if exe is None:
                return {"done": job_id, "status": "error",
                        "diagnostics": [{"severity": "error", "message": f"{tool} is not installed in the worker"}]}
            rel_cwd = Path(step.get("cwd", "src"))
            cwd = job_dir / rel_cwd
            if rel_cwd.is_absolute() or ".." in rel_cwd.parts:
                return {"done": job_id, "status": "error",
                        "diagnostics": [{"severity": "error", "message": "bad working directory"}]}
            if tool == "typst":
                problem = self._check_typst(job_dir, cwd, args)
                if problem:
                    return {"done": job_id, "status": "error", "diagnostics": [{"severity": "error", "message": problem}]}
            if tool in ("typst", "latexmk"):
                if uid is not None:
                    _kill_user(uid)  # nothing left over from an earlier step can plant links meanwhile
                removed = await asyncio.to_thread(_scrub_links, job_dir, uid)
                if removed:
                    log(level="warning", msg="removed links leading out of the job", job=job_id, links=removed[:20])
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"done": job_id, "status": "timeout", "diagnostics": diagnostics}
            proc = await asyncio.create_subprocess_exec(
                exe, *args, env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                preexec_fn=_make_preexec(uid, uid, limits, cwd), close_fds=True)
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
                rel = Path(step["stdout"])
                if rel.is_absolute() or ".." in rel.parts:
                    return {"done": job_id, "status": "error",
                            "diagnostics": [{"severity": "error", "message": "bad output path"}]}
                _write_into(job_dir / rel, stdout, uid)
            text = stderr.decode("utf-8", "replace")
            if tool == "latexmk":  # TeX reports errors on stdout
                text = stdout.decode("utf-8", "replace") + "\n" + text
            diagnostics += parse_diagnostics(tool, text)
            if proc.returncode != 0:
                if not any(d["severity"] == "error" for d in diagnostics):
                    diagnostics.append({"tool": tool, "severity": "error",
                                        "message": f"{tool} failed with exit code {proc.returncode}"})
                return {"done": job_id, "status": "error", "diagnostics": diagnostics[:MAX_DIAG]}
        return {"done": job_id, "status": "ok", "diagnostics": diagnostics[:MAX_DIAG]}

    def _check_typst(self, job_dir: Path, cwd: Path, args: list[str]) -> str | None:
        """Typst reads only inside its --root, which must be in the job, and fonts from the job, the
        render cache (layout fonts) or the shared font folder, none of them linking elsewhere."""
        if "--root" not in args[:-1]:
            return "Typst runs need --root."
        root = args[args.index("--root") + 1]
        if not _inside(str(cwd / root), [os.path.realpath(job_dir)]):
            return "Typst's --root must be inside the job."
        allowed = [os.path.realpath(p) for p in (job_dir, self.cache_dir, os.environ.get("DOCWIZARD_FONTS_DIR")
                                                 or SHARED_FONTS_DIR)]
        for i, a in enumerate(args[:-1]):
            if a == "--font-path":
                folder = str(cwd / args[i + 1])
                if not _inside(folder, allowed):
                    return "Typst font folders must be in the job, the render cache or the shared fonts."
                if os.path.isdir(folder) and _links_out(folder):
                    return "A font folder links to files outside it."
        return None

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
            try:
                result = await self.run_job(job_id)
            except Exception as exc:  # never leave the app waiting for a reply
                log(level="error", msg="job crashed", job=job_id, error=repr(exc))
                result = {"done": job_id, "status": "error", "diagnostics": [
                    {"severity": "error", "message": "The render worker could not run this job."}]}
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

    def clean_orphans(self) -> int:
        """Delete job directories a restart left behind. Ones younger than ORPHAN_AGE may be a job the
        app is about to send: they are only handed back to the app, which removes them after use."""
        run_dir = self.jobs_dir / "run"
        owner = run_dir.stat()
        if self.root:
            try:
                os.lchown(run_dir, 0, 0)  # to list and change it without CAP_DAC_OVERRIDE
                names = os.listdir(run_dir)
            except OSError:
                os.lchown(run_dir, owner.st_uid, owner.st_gid)
                return 0
        else:
            names = os.listdir(run_dir)
        removed = 0
        try:
            for name in names:
                path = run_dir / name
                try:
                    old = time.time() - os.lstat(path).st_mtime > ORPHAN_AGE
                    if old and not path.is_symlink():
                        _remove_tree(path, self.root)
                        removed += 1
                    elif self.root and not path.is_symlink():
                        _chown_tree(path, owner.st_uid, owner.st_gid)
                except OSError:
                    pass
        finally:
            if self.root:
                os.lchown(run_dir, owner.st_uid, owner.st_gid)
        if removed:
            log(level="info", msg="removed job directories left by a restart", count=removed)
        return removed

    async def serve(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        (self.jobs_dir / "run").mkdir(parents=True, exist_ok=True)
        self.clean_orphans()
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        old = os.umask(0o177)  # socket mode 0600
        try:
            server = await asyncio.start_unix_server(self.handle, path=str(self.socket_path))
        finally:
            os.umask(old)
        if self.root:
            # Only the app, which owns the jobs directory, may connect.
            st = self.jobs_dir.stat()
            os.chown(self.socket_path, st.st_uid, st.st_gid)
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

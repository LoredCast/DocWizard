"""The render worker's protocol loop (§9.2)."""

import asyncio
import json
import socket

import pytest

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


def test_lua_env_avoids_default_cwd_search(tmp_path, monkeypatch):
    # Empty or doubled semicolons in LUA_*PATH make Lua fall back to its compiled-in default,
    # which includes './?.lua' and './?.so' (§9.3).
    for job_path in ("", "/approved/?.lua", "/approved/_builtin/?.lua;/approved/filters/?.lua"):
        lua_path, lua_cpath = docwizard_worker._lua_env(job_path)
        for val in (lua_path, lua_cpath):
            assert "./" not in val
            assert not val.startswith(";") and not val.endswith(";") and ";;" not in val

    monkeypatch.setattr(docwizard_worker, "_SYS_LUA_CPATHS", ((str(tmp_path / "missing.so"), "/nope/?.so"),))
    assert docwizard_worker._lua_env("/approved/?.lua") == ("/approved/?.lua", "")
    assert docwizard_worker._lua_env("") == ("", "")

    lpeg = tmp_path / "lpeg.so"
    re_lua = tmp_path / "re.lua"
    lpeg.write_bytes(b"")
    re_lua.write_text("")
    monkeypatch.setattr(docwizard_worker, "_SYS_LUA_CPATHS", ((str(lpeg), f"{tmp_path}/?.so"),))
    monkeypatch.setattr(docwizard_worker, "_SYS_LUA_RE", str(re_lua))
    assert docwizard_worker._lua_env("") == ("/usr/share/lua/5.4/?.lua", f"{tmp_path}/?.so")
    assert docwizard_worker._lua_env("/approved/?.lua") == (
        "/approved/?.lua;/usr/share/lua/5.4/?.lua",
        f"{tmp_path}/?.so",
    )


# -- the job sandbox (§9.6) ----------------------------------------------------

@pytest.fixture
def shared():
    """A folder the job users reach when the tests run as root (pytest's tmp_path is private)."""
    import os
    import shutil
    import tempfile
    from pathlib import Path

    path = tempfile.mkdtemp(prefix="dw-worker-")
    os.chmod(path, 0o755)
    yield Path(path)
    shutil.rmtree(path, ignore_errors=True)


def _job(tmp_path, steps):
    import uuid

    (tmp_path / "jobs" / "run").mkdir(parents=True, exist_ok=True)
    worker = docwizard_worker.Worker(tmp_path / "sock", tmp_path / "jobs", tmp_path / "cache", 1, 61000)
    job_id = "j_" + uuid.uuid4().hex
    job_dir = tmp_path / "jobs" / "run" / job_id
    (job_dir / "src").mkdir(parents=True)
    (job_dir / "out").mkdir()
    (job_dir / "job.json").write_text(json.dumps({"id": job_id, "steps": steps, "limits": {"wall_seconds": 30}}))
    return worker, job_id, job_dir


def _fake_tool(tmp_path, monkeypatch, name, code):
    import sys

    script = tmp_path / "bin" / name
    script.parent.mkdir(exist_ok=True)
    script.write_text(f"#!{sys.executable}\n{code}")
    script.chmod(0o755)
    real = docwizard_worker.shutil.which
    monkeypatch.setattr(docwizard_worker.shutil, "which", lambda tool: str(script) if tool == name else real(tool))


async def test_tex_gets_a_paranoid_texmf_cnf_and_no_links_out(shared, monkeypatch):
    tmp_path = shared
    _fake_tool(tmp_path, monkeypatch, "latexmk", (
        "import json, os\n"
        "cnf = os.environ['TEXMFCNF'].split(':')[0] + '/texmf.cnf'\n"
        "json.dump({k: os.environ.get(k) for k in ('TEXMFCNF', 'openin_any', 'openout_any', 'shell_escape')}"
        " | {'cnf': open(cnf).read(), 'files': sorted(os.listdir('.'))}, open('../out/seen.json', 'w'))\n"))
    worker, job_id, job_dir = _job(tmp_path, [{"tool": "latexmk", "args": ["main.tex"]}])
    secret = tmp_path / "secret.txt"
    secret.write_text("not for jobs")
    (job_dir / "src" / "main.tex").write_text("\\input{leak}")
    (job_dir / "src" / "leak.tex").symlink_to(secret)
    (job_dir / "src" / "inside.tex").symlink_to(job_dir / "src" / "main.tex")
    (job_dir / "src" / "up").symlink_to("../..")
    reply = await worker.run_job(job_id)
    assert reply["status"] == "ok", reply
    seen = json.loads((job_dir / "out" / "seen.json").read_text())
    assert seen["TEXMFCNF"] == f"{job_dir}/texmf-cnf:"
    assert (seen["openin_any"], seen["openout_any"], seen["shell_escape"]) == ("p", "p", "f")
    for line in ("openin_any = p", "openout_any = p", "shell_escape = f"):
        assert line in seen["cnf"]
    # Links leading out of the job are gone before TeX starts; links inside it stay.
    assert seen["files"] == ["inside.tex", "main.tex"]


def test_typst_is_confined_to_the_job(shared):
    tmp_path = shared
    worker, _, job_dir = _job(tmp_path, [])
    cwd = job_dir / "src"
    fonts = tmp_path / "cache" / "layouts" / "x" / "fonts"
    fonts.mkdir(parents=True)
    ok = ["compile", "--root", "..", "--font-path", str(fonts), "a.typ", "a.pdf"]
    assert worker._check_typst(job_dir, cwd, ok) is None
    assert "--root" in worker._check_typst(job_dir, cwd, ["compile", "a.typ", "a.pdf"])
    assert "inside the job" in worker._check_typst(job_dir, cwd, ["compile", "--root", "/", "a.typ", "a.pdf"])
    assert "inside the job" in worker._check_typst(job_dir, cwd, ["compile", "--root", "../..", "a.typ", "a.pdf"])
    bad_fonts = ["compile", "--root", "..", "--font-path", "/etc", "a.typ", "a.pdf"]
    assert "font folders" in worker._check_typst(job_dir, cwd, bad_fonts)
    (fonts / "evil.ttf").symlink_to("/etc/hostname")
    assert "links to files outside" in worker._check_typst(job_dir, cwd, ok)


def test_orphaned_job_directories_are_removed_at_startup(shared):
    tmp_path = shared
    import os
    import time

    worker, _, old = _job(tmp_path, [])
    (old / "src" / "big.pdf").write_bytes(b"x" * 1000)
    past = time.time() - 3600
    os.utime(old, (past, past))
    _, _, fresh = _job(tmp_path, [])
    assert worker.clean_orphans() == 1
    assert not old.exists() and fresh.exists()

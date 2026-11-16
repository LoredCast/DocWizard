"""Render scheduling: snapshots, cache keys, dedupe, concurrency and previews (§9.2).

Identical requests from several collaborators share one job and one result.
For previews only the newest request per build and target runs; older queued
ones are dropped.
"""

from __future__ import annotations

import asyncio
import json
import re
import logging
import os
import secrets
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from ..citations.zotero import referenced_sources
from ..http.request import HTTPError
from ..logs import fields
from ..projects import service as svc
from . import jobs, sample
from .layouts import Layout, LayoutError
from .worker_client import WorkerClient, WorkerUnavailable

log = logging.getLogger("docwizard.render")

_DOCCLASS = re.compile(r"^[ \t]*\\documentclass", re.M)


def _is_root(text: str) -> bool:
    return bool(_DOCCLASS.search(text))


MAIN_CANDIDATES = ("main.md", "paper.md", "index.md", "thesis.md", "article.md")
PREVIEW_TOKEN_TTL = 6 * 3600


@dataclass
class RenderResult:
    key: str
    status: str
    target: str
    output: str
    diagnostics: list = field(default_factory=list)
    version: int = 0
    project_id: str = ""


@dataclass
class ExportJob:
    id: str
    project_id: str
    user_id: str  # the actor key, "u12" or "g3"
    target: str
    status: str = "queued"
    result: RenderResult | None = None
    created: float = field(default_factory=time.time)


class Renderer:
    SAMPLE_PROJECT = sample.SAMPLE_PROJECT

    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg
        self.worker = WorkerClient(app.cfg.worker_socket)
        self.renders_dir = app.cfg.data_dir / "renders"
        self.sem = asyncio.Semaphore(max(1, min(app.cfg.render.concurrency, 4)))
        self._inflight: dict[str, asyncio.Future] = {}
        self._latest: dict[tuple, int] = {}
        self._seq = 0
        self._preview_tasks: dict[tuple, asyncio.Task] = {}
        self.tokens: dict[str, dict] = {}
        self.exports: dict[str, ExportJob] = {}

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        self.renders_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.jobs_dir / "run").mkdir(parents=True, exist_ok=True)
        os.chmod(self.cfg.jobs_dir / "run", 0o711)
        if self.cfg.render.spawn_worker:
            await self.worker.spawn(self.cfg.jobs_dir, self.cfg.cache_dir, self.cfg.render.concurrency)
        else:
            try:
                await self.worker.hello()
            except (WorkerUnavailable, ValueError, OSError):
                log.warning("render worker not reachable at startup", extra=fields(socket=str(self.cfg.worker_socket)))

    async def stop(self) -> None:
        for task in list(self._preview_tasks.values()):
            task.cancel()
        self.worker.stop()

    # -- inputs ------------------------------------------------------------
    async def snapshot(self, project_id: str) -> tuple[jobs.ProjectSnapshot, dict[str, int]]:
        snap = jobs.ProjectSnapshot(project_id)
        versions: dict[str, int] = {}

        def load(c):
            docs = c.execute("SELECT id, path FROM documents WHERE project_id = ? AND deleted_at IS NULL",
                             (project_id,)).fetchall()
            assets = c.execute("SELECT path, sha256 FROM assets WHERE project_id = ? AND deleted_at IS NULL",
                               (project_id,)).fetchall()
            return [dict(d) for d in docs], [dict(a) for a in assets]

        docs, assets = await self.app.db.read(load)
        for d in docs:
            room = self.app.rooms.loaded(d["id"])
            if room is not None:
                text, version = await room.call(lambda room=room: _text_version(room))
            else:
                state = await self.app.db.read(lambda c, doc_id=d["id"]: svc.load_state(c, doc_id))
                text, version = state.text, state.version
            snap.texts[d["path"]] = text
            versions[d["id"]] = version
        for a in assets:
            snap.assets[a["path"]] = (a["sha256"], self.cfg.data_dir / "blobs" / a["sha256"])
        # zotero:<name> bibliographies become CSL-JSON files in the job (§9.4).
        names = referenced_sources(snap.texts)
        for name, data in (await self.app.zotero.csl_for_project(project_id, names)).items():
            snap.texts.setdefault(f"_zotero/{name}.json", data)
        return snap, versions

    @staticmethod
    def inputs_for(project: dict, doc_path: str | None, snap: jobs.ProjectSnapshot) -> list[str]:
        build = None
        if project.get("build_inputs"):
            try:
                build = json.loads(project["build_inputs"])
            except ValueError:
                build = None
        if doc_path and doc_path.endswith(".tex"):
            # LaTeX always builds the root: the build input, else main.tex, else a file with \documentclass.
            if build and str(build[0]).endswith(".tex") and build[0] in snap.texts:
                return [build[0]]
            if _is_root(snap.texts.get(doc_path, "")):
                return [doc_path]
            roots = sorted((p for p in snap.texts if p.endswith(".tex") and _is_root(snap.texts[p])),
                           key=lambda p: (p != "main.tex", p.count("/"), p))
            if roots:
                return [roots[0]]
            raise HTTPError(400, "no_input", "No LaTeX file in this project has \\documentclass, so there's nothing "
                                             "to build. Set the main document in the project settings.")
        if build:
            build = [p for p in build if not str(p).endswith(".tex")] or build
            return [p for p in build if p in snap.texts] or build
        if doc_path and doc_path.endswith((".md", ".markdown")):
            return [doc_path]
        for name in MAIN_CANDIDATES:
            if name in snap.texts:
                return [name]
        md = sorted(p for p in snap.texts if p.endswith((".md", ".markdown")))
        if md:
            return [md[0]]
        raise HTTPError(400, "no_input", "This project has no Markdown document to render.")

    def resolve_layout(self, project: dict, requested: str | None) -> Layout:
        layout_id = project["layout_id"]
        if requested:
            lid = requested.split("@", 1)[0]
            if self.app.layouts.exists(lid):
                layout_id = lid
        try:
            return self.app.layouts.get(layout_id)
        except LayoutError:
            return self.app.layouts.get(self.app.policy("default_layout"))

    # -- rendering -----------------------------------------------------------
    async def render(self, project: dict, target: str, *, doc_path: str | None = None, options: dict | None = None,
                     layout_id: str | None = None, slot: tuple | None = None) -> RenderResult | None:
        if target not in jobs.TARGETS:
            raise HTTPError(400, "bad_target", "Unknown output format.")
        layout = self.resolve_layout(project, layout_id)
        if options is None:
            try:
                options = json.loads(project.get("layout_options") or "{}")
            except ValueError:
                options = {}
        options = layout.validate_options(options)
        snap, versions = await self.snapshot(project["id"])
        inputs = self.inputs_for(project, doc_path, snap) if target != "md-source" else []
        if inputs and inputs[0].endswith(".tex") and target != "pdf-latex":
            raise HTTPError(400, "bad_target", "LaTeX documents build as PDF with LaTeX, or export as the "
                                               "project source.")
        lang = project.get("lang") or None
        # The project's document language changes the output, so it is part of the key (§9.8).
        key_options = {**options, "@lang": lang} if lang else options
        key = jobs.cache_key(project["id"], snap.digest(), layout, target, inputs, key_options, self.worker.versions)
        version = max(versions.values(), default=0)
        cached = self._cached(key, target)
        if cached is not None:
            cached.version = version
            return cached
        if key in self._inflight:
            return await asyncio.shield(self._inflight[key])
        seq = None
        if slot is not None:
            self._seq += 1
            seq = self._seq
            self._latest[slot] = seq
        fut = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            async with self.sem:
                if slot is not None and self._latest.get(slot) != seq:
                    fut.set_result(None)
                    return None  # a newer request for the same build replaced this one
                result = await self._run(key, project["id"], target, layout, inputs, options, snap, lang=lang)
            result.version = version
            fut.set_result(result)
            return result
        except BaseException as exc:
            if not fut.done():
                fut.set_exception(exc)
                fut.exception()  # mark retrieved
            raise
        finally:
            self._inflight.pop(key, None)

    async def render_sample(self, layout: Layout, target: str = "pdf-typst") -> RenderResult:
        """The layout editor's preview: the sample document with a draft layout (§9.3)."""
        snap = sample.snapshot()
        inputs = ["sample.md"]
        key = jobs.cache_key(sample.SAMPLE_PROJECT, snap.digest(), layout, target, inputs, {}, self.worker.versions)
        cached = self._cached(key, target)
        if cached is not None:
            return cached
        if key in self._inflight:
            return await asyncio.shield(self._inflight[key])
        fut = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            async with self.sem:
                # Drafts get a copy of their own, removed afterwards, so the cache doesn't fill up with them.
                result = await self._run(key, sample.SAMPLE_PROJECT, target, layout, inputs, {}, snap,
                                         layout_folder=f"layout-drafts/{secrets.token_hex(8)}")
            fut.set_result(result)
            return result
        except BaseException as exc:
            if not fut.done():
                fut.set_exception(exc)
                fut.exception()
            raise
        finally:
            self._inflight.pop(key, None)

    def _cached(self, key: str, target: str) -> RenderResult | None:
        meta_path = self.renders_dir / key / "meta.json"
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            return None
        os.utime(meta_path)  # for least-recently-used eviction
        return RenderResult(key, meta["status"], target, meta["output"], meta.get("diagnostics", []),
                            project_id=meta["project_id"])

    async def _run(self, key: str, project_id: str, target: str, layout: Layout, inputs: list[str],
                   options: dict, snap: jobs.ProjectSnapshot, layout_folder: str = "layouts",
                   lang: str | None = None) -> RenderResult:
        dest = self.renders_dir / key
        output = jobs.OUTPUTS[target]
        if target == "md-source":
            await asyncio.to_thread(_zip_snapshot, snap, dest, output)
            return self._finish(key, project_id, target, "ok", [], dest)
        job_id = jobs.new_job_id()
        job_dir = self.cfg.jobs_dir / "run" / job_id
        layout_dir = await asyncio.to_thread(self.app.layouts.cached_dir, layout, layout_folder)
        approved = self.app.layouts.filters_approved(layout)
        steps, copies = jobs.pandoc_steps(layout, layout_dir, target, inputs, options, approved)
        meta = jobs.metadata(layout, options, inputs, layout_dir, lang=lang)
        limits = {"cpu_seconds": self.cfg.render.timeout_seconds, "memory_mb": self.cfg.render.memory_mb,
                  "wall_seconds": self.cfg.render.timeout_seconds}
        job = {"id": job_id, "kind": "render", "inputs": inputs, "target": target, "layout": layout.content_sha256,
               "options": options, "limits": limits, "steps": steps,
               "lua_path": f"{layout_dir}/_builtin/?.lua" + (f";{layout_dir}/filters/?.lua" if approved else "")}

        def prepare():
            (job_dir / "src").mkdir(parents=True)
            (job_dir / "out").mkdir()
            snap.write(job_dir / "src")
            for src, name in copies:
                target_path = job_dir / "out" / name
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, target_path)
            (job_dir / "options.json").write_text(json.dumps(meta))
            (job_dir / "job.json").write_text(json.dumps(job))

        try:
            await asyncio.to_thread(prepare)
            started = time.monotonic()
            try:
                reply = await self.worker.run(job_id, limits["wall_seconds"])
            except WorkerUnavailable as exc:
                return RenderResult(key, "error", target, output,
                                    [{"severity": "error", "message": str(exc)}], project_id=project_id)
            status = reply.get("status", "error")
            diagnostics = reply.get("diagnostics", [])
            log.info("render", extra=fields(target=target, status=status, project=project_id,
                                            ms=round((time.monotonic() - started) * 1000)))
            if status == "ok" and not (job_dir / "out" / output).is_file():
                status = "error"
                diagnostics.append({"severity": "error", "message": "The renderer produced no output."})
            if status != "ok":
                # Failed renders aren't cached, so fixing the document retries.
                return RenderResult(key, status, target, output, diagnostics, project_id=project_id)
            await asyncio.to_thread(_move_outputs, job_dir / "out", dest)
            return self._finish(key, project_id, target, status, diagnostics, dest)
        finally:
            await asyncio.to_thread(shutil.rmtree, job_dir, True)
            if layout_folder != "layouts":
                await asyncio.to_thread(shutil.rmtree, layout_dir.parent, True)

    def _finish(self, key, project_id, target, status, diagnostics, dest: Path) -> RenderResult:
        output = jobs.OUTPUTS[target]
        meta = {"status": status, "output": output, "target": target, "project_id": project_id,
                "diagnostics": diagnostics, "created": time.time()}
        (dest / "meta.json").write_text(json.dumps(meta))
        return RenderResult(key, status, target, output, diagnostics, project_id=project_id)

    # -- previews ------------------------------------------------------------
    def preview_token(self, conn_key: int, project_id: str) -> str:
        now = time.time()
        for tok, info in list(self.tokens.items()):
            if info["expires"] < now:
                del self.tokens[tok]
        for tok, info in self.tokens.items():
            if info["conn"] == conn_key and info["project_id"] == project_id:
                info["expires"] = now + PREVIEW_TOKEN_TTL
                return tok
        tok = secrets.token_hex(16)  # 128 bits
        self.tokens[tok] = {"conn": conn_key, "project_id": project_id, "expires": now + PREVIEW_TOKEN_TTL}
        return tok

    def forget(self, conn_key: int) -> None:
        """Preview capabilities expire with the editor session."""
        for tok, info in list(self.tokens.items()):
            if info["conn"] == conn_key:
                info["expires"] = min(info["expires"], time.time() + 60)

    def token_project(self, token: str) -> str | None:
        info = self.tokens.get(token)
        if info is None or info["expires"] < time.time():
            return None
        return info["project_id"]

    async def request_preview(self, conn, peer, room, msg: dict) -> None:
        target = msg.get("target")
        if target not in jobs.PREVIEW_TARGETS:
            raise HTTPError(400, "bad_target", "Previews are HTML or PDF.")
        options = msg.get("options")
        project = await self.app.db.read(lambda c: svc.get_project(c, room.project_id))
        if project is None:
            raise HTTPError(404, "not_found", "Project not found.")
        project = dict(project)
        layout = self.resolve_layout(project, msg.get("layout"))
        try:
            options = layout.validate_options(options if isinstance(options, dict) else
                                              json.loads(project.get("layout_options") or "{}"))
        except ValueError as exc:
            raise HTTPError(400, "bad_options", str(exc)) from None
        slot = (room.project_id, room.path if room.path.endswith((".md", ".markdown")) else "", target)
        task_key = (conn.key, room.doc_id, target)
        old = self._preview_tasks.get(task_key)
        if old is not None and not old.done():
            old.cancel()

        async def run():
            try:
                result = await self.render(project, target, doc_path=room.path, options=options,
                                           layout_id=layout.id, slot=slot)
            except HTTPError as exc:
                conn.send({"t": "rendered", "doc": room.doc_id, "target": target, "url": None,
                           "diagnostics": [{"severity": "error", "message": exc.message}]})
                return
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                log.exception("preview failed")
                conn.send({"t": "rendered", "doc": room.doc_id, "target": target, "url": None,
                           "diagnostics": [{"severity": "error", "message": "The preview failed on the server."}]})
                return
            if result is None:
                return
            url = None
            if result.status == "ok":
                if target == "html":
                    token = self.preview_token(conn.key, room.project_id)
                    url = f"{self.cfg.server.preview_url.rstrip('/')}/{token}/{result.key}/{result.output}"
                else:
                    url = f"/api/renders/{result.key}/{result.output}"
            conn.send({"t": "rendered", "doc": room.doc_id, "version": result.version, "target": target,
                       "url": url, "status": result.status, "diagnostics": result.diagnostics})

        task = asyncio.create_task(run())
        self._preview_tasks[task_key] = task
        task.add_done_callback(lambda t, k=task_key: self._preview_tasks.pop(k, None)
                               if self._preview_tasks.get(k) is t else None)

    # -- exports -------------------------------------------------------------
    def start_export(self, project: dict, user_id: str, target: str, doc_path: str | None,
                     options: dict | None) -> ExportJob:
        now = time.time()
        for jid, job in list(self.exports.items()):
            if now - job.created > 3600:
                del self.exports[jid]
        job = ExportJob("x_" + secrets.token_hex(12), project["id"], user_id, target)
        self.exports[job.id] = job

        async def run():
            job.status = "running"
            try:
                job.result = await self.render(project, target, doc_path=doc_path, options=options)
                job.status = job.result.status if job.result else "error"
            except HTTPError as exc:
                job.status = "error"
                job.result = RenderResult("", "error", target, "", [{"severity": "error", "message": exc.message}])
            except Exception:  # noqa: BLE001
                log.exception("export failed")
                job.status = "error"
                job.result = RenderResult("", "error", target, "", [{"severity": "error",
                                                                     "message": "The export failed on the server."}])

        self.app.spawn(run(), f"export-{job.id}")
        return job

    # -- maintenance ---------------------------------------------------------
    async def evict(self, max_bytes: int = 2_000_000_000) -> None:
        """Least-recently-used eviction of the render cache."""
        def work():
            entries, total = [], 0
            for d in self.renders_dir.iterdir():
                meta = d / "meta.json"
                try:
                    size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                    entries.append((meta.stat().st_mtime, size, d))
                    total += size
                except OSError:
                    continue
            for _, size, d in sorted(entries):
                if total <= max_bytes:
                    break
                shutil.rmtree(d, ignore_errors=True)
                total -= size

        await asyncio.to_thread(work)
        await asyncio.to_thread(self.sweep_jobs)

    def sweep_jobs(self) -> None:
        """Remove job directories left behind by a crash or a worker restart."""
        cutoff = time.time() - self.cfg.render.timeout_seconds - 300
        try:
            entries = list((self.cfg.jobs_dir / "run").iterdir())
        except OSError:
            return
        for d in entries:
            try:
                if d.lstat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                continue


async def _text_version(room):
    return room.text(), room.version


def _move_outputs(src: Path, dest: Path) -> None:
    tmp = dest.with_name(dest.name + ".tmp-" + secrets.token_hex(4))
    shutil.copytree(src, tmp, symlinks=True, ignore=shutil.ignore_patterns(".*"))
    # Never keep symlinks a job may have planted.
    for p in list(tmp.rglob("*")):
        if p.is_symlink():
            p.unlink()
    if dest.exists():
        shutil.rmtree(tmp, ignore_errors=True)
        return
    tmp.rename(dest)


def _zip_snapshot(snap: jobs.ProjectSnapshot, dest: Path, output: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    tmp = dest / (output + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, text in sorted(snap.texts.items()):
            zf.writestr(path, text)
        for path, (_, blob) in sorted(snap.assets.items()):
            zf.write(blob, path)
    tmp.rename(dest / output)

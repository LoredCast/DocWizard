"""Render scheduling: snapshots, cache keys, dedupe, concurrency and previews (§9.2).

Identical requests from several collaborators share one job and one result.
For previews only the newest request per build and target runs; older queued
ones are dropped.

Fairness: ``render.concurrency`` jobs run at once, but LaTeX builds may not take
the ``render.fast_lane`` slots, so HTML and Typst previews never wait behind a
queue of LaTeX builds. Each person renders ``render.user_previews`` previews at
a time and has at most ``render.user_exports`` exports in flight; optional token
buckets (``render.per_user_rpm``, ``render.latex_daily_limit``) bound how much
one person can render. LaTeX and the time limit can be set per project owner.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
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

from ..auth.throttle import TokenBucket
from ..citations.zotero import referenced_sources
from ..http.request import HTTPError
from ..logs import fields
from ..projects import docx_import, fountain
from ..projects import service as svc
from . import anonymize as anonymizer
from . import jobs, sample, synctex
from .layouts import Layout, LayoutError
from .worker_client import WorkerPool, WorkerUnavailable

log = logging.getLogger("docwizard.render")

_DOCCLASS = re.compile(r"^[ \t]*\\documentclass", re.M)


def _is_root(text: str) -> bool:
    return bool(_DOCCLASS.search(text))


MAIN_CANDIDATES = ("main.md", "paper.md", "index.md", "thesis.md", "article.md")
PREVIEW_TOKEN_TTL = 6 * 3600
# Targets that may take minutes and stay out of the fast lane.
HEAVY_TARGETS = {"pdf-latex"}
DURATION_BUCKETS = (0.5, 1, 2, 5, 10, 30, 60, 120, 300)
RATE_MESSAGE = "You're rendering a lot right now. Wait a minute, then try again."
LATEX_DAILY_MESSAGE = "You've reached today's limit for LaTeX builds. Use the PDF made with Typst, or try again tomorrow."
EXPORT_LIMIT_MESSAGE = "Wait for your other exports to finish, then try again."


class _PerKey:
    """A semaphore per person, dropped again when nobody holds or waits for it."""

    def __init__(self, size: int):
        self.size = size
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._users: dict[str, int] = {}

    @contextlib.asynccontextmanager
    async def hold(self, key: str | None):
        if not key or self.size <= 0:
            yield
            return
        sem = self._sems.get(key)
        if sem is None:
            sem = self._sems[key] = asyncio.Semaphore(self.size)
        self._users[key] = self._users.get(key, 0) + 1
        try:
            async with sem:
                yield
        finally:
            self._users[key] -= 1
            if not self._users[key]:
                del self._users[key]
                del self._sems[key]


class RenderStats:
    """Counters for /metrics (§16): renders by target and outcome, and how long they took."""

    def __init__(self):
        self.totals: dict[tuple[str, str], int] = {}
        self.durations: dict[str, list] = {}  # target -> [count per bucket..., sum, count]

    def record(self, target: str, status: str, seconds: float) -> None:
        self.totals[(target, status)] = self.totals.get((target, status), 0) + 1
        hist = self.durations.setdefault(target, [0] * len(DURATION_BUCKETS) + [0.0, 0])
        for i, bound in enumerate(DURATION_BUCKETS):
            if seconds <= bound:
                hist[i] += 1
        hist[-2] += seconds
        hist[-1] += 1


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
        self.worker = WorkerPool.from_config(app.cfg)
        self.renders_dir = app.cfg.data_dir / "renders"
        self.configure()
        self.stats = RenderStats()
        self.queued = 0  # jobs waiting for a slot
        self.rejections = 0
        self._rate: TokenBucket | None = None
        self._latex_today: dict[str, tuple[str, int]] = {}
        self._inflight: dict[str, asyncio.Future] = {}
        self._latest: dict[tuple, int] = {}
        self._seq = 0
        self._preview_tasks: dict[tuple, asyncio.Task] = {}
        self.boot = secrets.token_hex(6)
        self._tokens: dict[tuple[str, str], tuple[str, float]] = {}  # (conn, project) -> (token, expires)
        self.exports: dict[str, ExportJob] = {}

    def configure(self) -> None:
        """Size the slots from [render]: every job takes one of ``concurrency`` slots, and heavy
        targets also one of the ``concurrency - fast_lane`` heavy ones."""
        cfg = self.cfg.render
        # Each worker runs ``concurrency`` jobs at once (give it the same --concurrency).
        total = max(1, cfg.concurrency) * len(self.worker)
        fast = max(0, min(cfg.fast_lane * len(self.worker), total - 1))
        self.sem = asyncio.Semaphore(total)
        self.heavy = asyncio.Semaphore(total - fast)
        self.previews = _PerKey(cfg.user_previews)

    @contextlib.asynccontextmanager
    async def slot(self, target: str):
        """Wait for a render slot. Heavy jobs first take a heavy slot, so they queue among themselves
        and the fast lane stays free for the rest."""
        self.queued += 1
        waiting = True
        try:
            async with self.heavy if target in HEAVY_TARGETS else contextlib.nullcontext():
                async with self.sem:
                    self.queued -= 1
                    waiting = False
                    yield
        finally:
            if waiting:
                self.queued -= 1

    # -- limits --------------------------------------------------------------
    def _bucket(self) -> TokenBucket | None:
        rpm = self.cfg.render.per_user_rpm
        if rpm <= 0:
            return None
        if self._rate is None or self._rate.capacity != rpm:
            self._rate = TokenBucket(rpm, rpm / 60)
        return self._rate

    def _limited(self, code: str, message: str) -> HTTPError:
        self.rejections += 1
        return HTTPError(429, code, message)

    def charge_rate(self, actor: str | None) -> None:
        bucket = self._bucket()
        if actor and bucket is not None and not bucket.allow(actor):
            raise self._limited("render_limited", RATE_MESSAGE)

    def _latex_used(self, actor: str) -> tuple[str, int]:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        used_day, n = self._latex_today.get(actor, (day, 0))
        return day, n if used_day == day else 0

    def check_latex_quota(self, actor: str | None, *, charge: bool) -> None:
        limit = self.cfg.render.latex_daily_limit
        if not actor:
            return
        day, n = self._latex_used(actor)
        if 0 < limit <= n:
            raise self._limited("latex_limit", LATEX_DAILY_MESSAGE)
        if charge:  # counted without a limit too, so one set during the day applies right away
            if len(self._latex_today) > 100_000:
                self._latex_today.clear()
            self._latex_today[actor] = (day, n + 1)

    async def owner_policy(self, project_id: str) -> dict:
        """What the project's owner may render: LaTeX or not, and the time limit."""
        row = await self.app.db.fetchone(
            "SELECT u.instance_role, u.allow_latex, u.render_timeout_seconds, u.email_verified_at FROM projects p"
            " JOIN users u ON u.id = p.owner_id WHERE p.id = ?", (project_id,))
        cfg = self.cfg.render
        if row is None:
            return {"latex": cfg.allow_latex, "timeout": cfg.timeout_seconds, "unverified": False}
        return {"latex": cfg.allow_latex if row["allow_latex"] is None else bool(row["allow_latex"]),
                "timeout": row["render_timeout_seconds"] or cfg.timeout_seconds,
                "unverified": bool(self.app.policy("require_email_verification") and row["instance_role"] != "admin"
                                   and row["email_verified_at"] is None)}

    @staticmethod
    def check_latex_allowed(policy: dict) -> None:
        if not policy["latex"]:
            raise HTTPError(403, "latex_disabled", "LaTeX builds aren't available for this project. Use the PDF "
                                                   "made with Typst instead, or ask an admin.")
        if policy["unverified"]:
            raise HTTPError(403, "email_unverified", "LaTeX builds start once the project's owner has confirmed "
                                                     "their email address.")

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        self.renders_dir.mkdir(parents=True, exist_ok=True)
        for member in self.worker.members:
            member.run_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(member.run_dir, 0o711)
        if self.cfg.render.spawn_worker:
            await self.worker.spawn(self.cfg.cache_dir, self.cfg.render.concurrency)
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
                     layout_id: str | None = None, slot: tuple | None = None, actor: str | None = None,
                     charge: bool = True, anonymize: bool = False) -> RenderResult | None:
        """Render, or return the cached result. *actor* ("u12", "g3") is who asked: renders that
        actually run count against their limits (*charge* False: the rate was charged already)."""
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
        # Double-blind exports leave out who wrote it; the others leave out the anonymized
        # wording in .blind passages (§9.11).
        # The project source gets every document anonymized.
        blind_inputs = inputs or ([p for p in snap.texts if p.endswith((".md", ".markdown", ".tex"))]
                                  if anonymize else [])
        blind = anonymize or any(anonymizer.has_blind(snap.texts.get(p, "")) for p in blind_inputs)
        if blind:
            snap = jobs.ProjectSnapshot(snap.project_id,
                                        anonymizer.snapshot_texts(snap.texts, blind_inputs, anonymize),
                                        dict(snap.assets))
        elif target == "docx" and inputs and inputs[0].endswith((".md", ".markdown")):
            snap = await self._with_comments(project["id"], inputs[0], snap)
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
        policy = await self.owner_policy(project["id"])
        if target in HEAVY_TARGETS:
            self.check_latex_allowed(policy)
        if key in self._inflight:  # started while the policy was read
            return await asyncio.shield(self._inflight[key])
        seq = None
        if slot is not None:
            self._seq += 1
            seq = self._seq
            self._latest[slot] = seq
        fut = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            async with self.slot(target):
                if slot is not None and self._latest.get(slot) != seq:
                    fut.set_result(None)
                    return None  # a newer request for the same build replaced this one
                if charge:
                    self.charge_rate(actor)
                if target in HEAVY_TARGETS:
                    self.check_latex_quota(actor, charge=True)
                result = await self._timed(target, self._run(key, project["id"], target, layout, inputs, options,
                                                             snap, lang=lang, timeout=policy["timeout"]))
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

    async def _with_comments(self, project_id: str, path: str, snap: jobs.ProjectSnapshot) -> jobs.ProjectSnapshot:
        """Word exports keep the open comment threads as Word comments (§8.11)."""
        doc = await self.app.db.read(lambda c: svc.get_document_by_path(c, project_id, path))
        if doc is None:
            return snap
        rows = await self.app.db.fetchall(
            "SELECT c.id, c.thread_id, c.author_name, c.body, c.suggestion, c.created_at FROM comments c"
            " JOIN comments t ON t.id = c.thread_id WHERE c.doc_id = ? AND t.resolved_at IS NULL"
            " AND t.orphaned = 0 ORDER BY c.thread_id, c.id", (doc["id"],))
        if not rows:
            return snap
        room = await self.app.rooms.get(doc["id"])
        text, anchors = await room.call(
            lambda: _now(room.text(), {k[1]: list(v) for k, v in room.anchors.items() if k[0] == "comment"}))
        if text != snap.texts.get(path):
            return snap  # changed in between: export without comments rather than misplace them
        threads: dict[int, dict] = {}
        for r in rows:
            rng = anchors.get(r["thread_id"])
            if rng is None:
                continue
            t = threads.setdefault(r["thread_id"], {"range": rng, "comments": []})
            body = r["body"]
            if r["id"] == r["thread_id"] and r["suggestion"] is not None:
                body = (f"Suggested: “{r['suggestion']}”" if r["suggestion"] else "Suggested: delete this.") + \
                    (f" {body}" if body else "")
            t["comments"].append({"author": r["author_name"], "date": (r["created_at"] or "")[:19] + "Z",
                                  "body": body})
        flat = [o for t in threads.values() for o in t["range"]]
        idx = docx_import.from_utf16(text, flat)
        items = [{"start": idx[2 * i], "end": idx[2 * i + 1], "comments": t["comments"]}
                 for i, t in enumerate(threads.values())]
        out = jobs.ProjectSnapshot(snap.project_id, dict(snap.texts), dict(snap.assets))
        out.texts[path] = docx_import.comment_markers(text, items)
        return out

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
            async with self.slot(target):
                # Drafts get a copy of their own, removed afterwards, so the cache doesn't fill up with them.
                result = await self._timed(target, self._run(
                    key, sample.SAMPLE_PROJECT, target, layout, inputs, {}, snap,
                    layout_folder=f"layout-drafts/{secrets.token_hex(8)}"))
            fut.set_result(result)
            return result
        except BaseException as exc:
            if not fut.done():
                fut.set_exception(exc)
                fut.exception()
            raise
        finally:
            self._inflight.pop(key, None)

    async def _timed(self, target: str, run) -> RenderResult:
        started = time.monotonic()
        status = "error"
        try:
            result = await run
            status = result.status
            return result
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            self.stats.record(target, status, time.monotonic() - started)

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
                   lang: str | None = None, timeout: int | None = None) -> RenderResult:
        dest = self.renders_dir / key
        output = jobs.OUTPUTS[target]
        if target == "md-source":
            await asyncio.to_thread(_zip_snapshot, snap, dest, output)
            return self._finish(key, project_id, target, "ok", [], dest)
        if target == "fountain":
            def write_fountain():
                dest.mkdir(parents=True, exist_ok=True)
                (dest / output).write_text(fountain.from_markdown(snap.texts.get(inputs[0], "")))

            await asyncio.to_thread(write_fountain)
            return self._finish(key, project_id, target, "ok", [], dest)
        async with self.worker.lease() as worker:
            return await self._run_on(worker, key, project_id, target, layout, inputs, options, snap,
                                      layout_folder, lang, timeout)

    async def _run_on(self, worker, key: str, project_id: str, target: str, layout: Layout, inputs: list[str],
                      options: dict, snap: jobs.ProjectSnapshot, layout_folder: str, lang: str | None,
                      timeout: int | None) -> RenderResult:
        dest = self.renders_dir / key
        output = jobs.OUTPUTS[target]
        job_id = jobs.new_job_id()
        job_dir = worker.run_dir / job_id
        layout_dir = await asyncio.to_thread(self.app.layouts.cached_dir, layout, layout_folder)
        approved = self.app.layouts.filters_approved(layout)
        steps, copies = jobs.pandoc_steps(layout, layout_dir, target, inputs, options, approved)
        meta = jobs.metadata(layout, options, inputs, layout_dir, lang=lang)
        seconds = timeout or self.cfg.render.timeout_seconds
        limits = {"cpu_seconds": seconds, "memory_mb": self.cfg.render.memory_mb, "wall_seconds": seconds}
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
                reply = await worker.run(job_id, limits["wall_seconds"])
            except WorkerUnavailable as exc:
                worker.down_until = time.monotonic() + 15
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
            if target == "pdf-latex":
                await asyncio.to_thread(_index_synctex, job_dir, dest)
            return self._finish(key, project_id, target, status, diagnostics, dest)
        finally:
            await asyncio.to_thread(shutil.rmtree, job_dir, True)
            if layout_folder != "layouts":
                await asyncio.to_thread(shutil.rmtree, layout_dir.parent, True)

    async def convert_docx(self, data: bytes) -> tuple[str, dict[str, bytes]]:
        """Convert an uploaded Word file in the render worker (§8.11): pandoc keeps comments and
        tracked changes, which filters/docx_import.lua marks. Returns the Markdown and the media
        files (paths relative to the media folder)."""
        async with self.worker.lease() as worker:
            return await self._convert_docx_on(worker, data)

    async def _convert_docx_on(self, worker, data: bytes) -> tuple[str, dict[str, bytes]]:
        job_id = jobs.new_job_id()
        job_dir = worker.run_dir / job_id
        layout_dir = await asyncio.to_thread(self.app.layouts.cached_dir, self.app.layouts.get("default"), "layouts")
        L = str(layout_dir)
        limits = {"cpu_seconds": self.cfg.render.timeout_seconds, "memory_mb": self.cfg.render.memory_mb,
                  "wall_seconds": self.cfg.render.timeout_seconds}
        steps = [{"tool": "pandoc", "args": [
            "upload.docx", "--from", "docx", "--track-changes=all", "--to", "markdown-simple_tables-multiline_tables",
            "--wrap=none", "--standalone", "--extract-media=../out", "--lua-filter", f"{L}/_builtin/docx_import.lua",
            "-o", "../out/document.md"]}]
        job = {"id": job_id, "kind": "render", "inputs": ["upload.docx"], "target": "md-import", "layout": "",
               "options": {}, "limits": limits, "steps": steps, "lua_path": f"{L}/_builtin/?.lua"}

        def prepare():
            (job_dir / "src").mkdir(parents=True)
            (job_dir / "out").mkdir()
            (job_dir / "src" / "upload.docx").write_bytes(data)
            (job_dir / "options.json").write_text("{}")
            (job_dir / "job.json").write_text(json.dumps(job))

        def collect():
            out = job_dir / "out"
            text = (out / "document.md").read_text(encoding="utf-8")
            media: dict[str, bytes] = {}
            for p in sorted((out / "media").rglob("*")) if (out / "media").is_dir() else []:
                if p.is_file() and not p.is_symlink():
                    media[p.relative_to(out / "media").as_posix()] = p.read_bytes()
            return text, media

        try:
            await asyncio.to_thread(prepare)
            async with self.slot("md-import"):
                try:
                    reply = await worker.run(job_id, limits["wall_seconds"])
                except WorkerUnavailable as exc:
                    raise HTTPError(503, "worker_unavailable", str(exc)) from None
            if reply.get("status") != "ok" or not (job_dir / "out" / "document.md").is_file():
                messages = "; ".join(d.get("message", "") for d in reply.get("diagnostics", []))[:500]
                raise HTTPError(400, "bad_docx", "This Word file couldn't be read." + (f" {messages}" if messages else ""))
            return await asyncio.to_thread(collect)
        finally:
            await asyncio.to_thread(shutil.rmtree, job_dir, True)

    def _finish(self, key, project_id, target, status, diagnostics, dest: Path) -> RenderResult:
        output = jobs.OUTPUTS[target]
        meta = {"status": status, "output": output, "target": target, "project_id": project_id,
                "diagnostics": diagnostics, "created": time.time()}
        (dest / "meta.json").write_text(json.dumps(meta))
        return RenderResult(key, status, target, output, diagnostics, project_id=project_id)

    # -- previews ------------------------------------------------------------
    # Capability tokens live in the database (migration 0015): they survive a restart and any process
    # serving the preview origin can check them. Connections are numbered per process, so their keys
    # carry this process's boot id.
    def _conn(self, conn_key: int) -> str:
        return f"{self.boot}:{conn_key}"

    async def preview_token(self, conn_key: int, project_id: str) -> str:
        now = time.time()
        conn = self._conn(conn_key)
        cached = self._tokens.get((conn, project_id))
        if cached and cached[1] - now > PREVIEW_TOKEN_TTL / 2:
            return cached[0]
        tok = cached[0] if cached and cached[1] > now else secrets.token_hex(16)  # 128 bits
        expires = now + PREVIEW_TOKEN_TTL

        def save(c):
            c.execute("DELETE FROM preview_tokens WHERE expires_at < ?", (now,))
            c.execute("INSERT INTO preview_tokens(token_hash, project_id, conn_key, expires_at) VALUES (?, ?, ?, ?)"
                      " ON CONFLICT(token_hash) DO UPDATE SET expires_at = excluded.expires_at",
                      (_token_hash(tok), project_id, conn, expires))

        await self.app.db.write(save)
        self._tokens[(conn, project_id)] = (tok, expires)
        return tok

    async def forget(self, conn_key: int) -> None:
        """Preview capabilities expire with the editor session (a minute later, for the last loads)."""
        conn = self._conn(conn_key)
        for k in [k for k in self._tokens if k[0] == conn]:
            del self._tokens[k]
        soon = time.time() + 60
        await self.app.db.write(lambda c: c.execute(
            "UPDATE preview_tokens SET expires_at = MIN(expires_at, ?) WHERE conn_key = ?", (soon, conn)))

    async def token_project(self, token: str) -> str | None:
        row = await self.app.db.fetchone("SELECT project_id, expires_at FROM preview_tokens WHERE token_hash = ?",
                                         (_token_hash(token),))
        if row is None or row["expires_at"] < time.time():
            return None
        return row["project_id"]

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
        actor = f"u{conn.user.id}" if conn.user is not None else f"g{conn.guest.id}" if conn.guest is not None else None
        task_key = (conn.key, room.doc_id, target)
        old = self._preview_tasks.get(task_key)
        if old is not None and not old.done():
            old.cancel()

        async def run():
            try:
                async with self.previews.hold(actor):
                    for attempt in range(2):
                        try:
                            result = await self.render(project, target, doc_path=room.path, options=options,
                                                       layout_id=layout.id, slot=slot, actor=actor)
                            break
                        except asyncio.CancelledError:
                            # This request joined an identical render whose own request was then
                            # cancelled (the one it replaced): render again rather than go silent.
                            if asyncio.current_task().cancelling() or attempt:
                                raise
            except HTTPError as exc:
                conn.send({"t": "rendered", "doc": room.doc_id, "target": target, "url": None,
                           "status": "limited" if exc.status == 429 else "error", "code": exc.code,
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
                    token = await self.preview_token(conn.key, room.project_id)
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
                     options: dict | None, anonymize: bool = False) -> ExportJob:
        now = time.time()
        for jid, job in list(self.exports.items()):
            if now - job.created > 3600:
                del self.exports[jid]
        limit = self.cfg.render.user_exports
        active = sum(1 for j in self.exports.values() if j.user_id == user_id and j.status in ("queued", "running"))
        if limit > 0 and active >= limit:
            raise self._limited("export_limit", EXPORT_LIMIT_MESSAGE)
        if target in HEAVY_TARGETS:
            self.check_latex_quota(user_id, charge=False)
        self.charge_rate(user_id)
        job = ExportJob("x_" + secrets.token_hex(12), project["id"], user_id, target)
        self.exports[job.id] = job

        async def run():
            job.status = "running"
            try:
                job.result = await self.render(project, target, doc_path=doc_path, options=options, actor=user_id,
                                               charge=False, anonymize=anonymize)
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
        entries = []
        for member in self.worker.members:
            try:
                entries += list(member.run_dir.iterdir())
            except OSError:
                continue
        for d in entries:
            try:
                if d.lstat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                continue


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _text_version(room):
    return room.text(), room.version


async def _now(*values):
    return values


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


def _index_synctex(job_dir: Path, dest: Path) -> None:
    """Keep what SyncTeX recorded as synctex.json for clicks in the preview (§6.4). latexmk puts the
    file next to the PDF; older versions leave it in the auxiliary directory."""
    for src in (dest / "document.synctex.gz", job_dir / "tmp" / "latex" / "document.synctex.gz",
                dest / "document.synctex"):
        try:
            if synctex.index(src, dest / "synctex.json"):
                break
        except (OSError, ValueError):
            log.warning("synctex index failed", exc_info=True)
            break
    for leftover in ("document.synctex.gz", "document.synctex"):
        try:
            (dest / leftover).unlink()
        except OSError:
            pass


def _zip_snapshot(snap: jobs.ProjectSnapshot, dest: Path, output: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    tmp = dest / (output + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, text in sorted(snap.texts.items()):
            zf.writestr(path, text)
        for path, (_, blob) in sorted(snap.assets.items()):
            zf.write(blob, path)
    tmp.rename(dest / output)

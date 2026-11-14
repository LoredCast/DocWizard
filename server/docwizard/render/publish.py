"""Publishing documents as static sites (§14.4).

A publication renders the document's ``html`` target with the project's layout and
writes it to ``<publish_dir>/<slug>/`` together with the project's images and,
optionally, a PDF. The proxy serves that folder from a separate origin with a
strict CSP and no scripts. Republishing replaces the folder atomically;
unpublishing deletes it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import shutil
import time
from pathlib import Path

from ..logs import fields
from ..projects import files

log = logging.getLogger("docwizard.publish")

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".avif"}
AUTO_DELAY = 10.0
SITE_CSP = ("default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self';"
            " base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class PublishError(Exception):
    pass


def check_slug(slug: str) -> str:
    slug = slug.strip().lower()
    if not SLUG_RE.match(slug):
        raise PublishError("Addresses use 3 to 64 lowercase letters, digits and dashes, starting with a letter or "
                           "digit.")
    if slug.startswith("u-"):
        raise PublishError("Addresses starting with u- are kept for unlisted publications.")
    return slug


def unlisted_slug() -> str:
    return "u-" + secrets.token_hex(12)


class Publisher:
    def __init__(self, app):
        self.app = app
        self.root = Path(app.cfg.server.publish_dir)
        self._due: dict[str, float] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        app.rooms.change_hooks.append(self._changed)

    def url_for(self, slug: str) -> str:
        return f"{self.app.cfg.server.publish_url.rstrip('/')}/{slug}/"

    # -- auto update -------------------------------------------------------
    def _changed(self, room) -> None:
        self._due[room.doc_id] = time.monotonic()

    async def run_forever(self) -> None:
        last_prune = 0.0
        while True:
            await asyncio.sleep(2.0)
            if time.monotonic() - last_prune > 60:
                last_prune = time.monotonic()
                try:
                    await self.prune()
                except Exception:  # noqa: BLE001
                    log.exception("pruning publications failed")
            ready = [d for d, t in self._due.items() if time.monotonic() - t >= AUTO_DELAY]
            for doc_id in ready:
                self._due.pop(doc_id, None)
                row = await self.app.db.fetchone(
                    "SELECT id FROM publications WHERE doc_id = ? AND auto_update = 1", (doc_id,))
                if row is not None:
                    try:
                        await self.publish(row["id"])
                    except Exception:  # noqa: BLE001
                        log.exception("automatic republish failed", extra=fields(publication=row["id"]))

    # -- publish ------------------------------------------------------------
    async def publish(self, pub_id: int) -> dict:
        lock = self._locks.setdefault(pub_id, asyncio.Lock())
        async with lock:
            pub = await self.app.db.fetchone(
                "SELECT p.*, d.path AS doc_path, d.version AS doc_version FROM publications p"
                " JOIN documents d ON d.id = p.doc_id AND d.deleted_at IS NULL WHERE p.id = ?", (pub_id,))
            if pub is None:
                raise PublishError("The document no longer exists.")
            project = await self.app.db.fetchone("SELECT * FROM projects WHERE id = ?", (pub["project_id"],))
            renderer = self.app.renderer
            html = await renderer.render(dict(project), "html", doc_path=pub["doc_path"])
            if html is None or html.status != "ok":
                message = "; ".join(d.get("message", "") for d in (html.diagnostics if html else [])[:3])
                await self._failed(pub_id, message or "The document couldn't be rendered.")
                raise PublishError(message or "The document couldn't be rendered.")
            pdf = None
            if pub["with_pdf"]:
                pdf = await renderer.render(dict(project), "pdf-typst", doc_path=pub["doc_path"])
                if pdf is None or pdf.status != "ok":
                    await self._failed(pub_id, "The PDF couldn't be rendered.")
                    raise PublishError("The PDF couldn't be rendered.")
            assets = await self.app.db.fetchall(
                "SELECT path, sha256 FROM assets WHERE project_id = ? AND deleted_at IS NULL", (pub["project_id"],))
            await asyncio.to_thread(self._write_site, pub["slug"], renderer.renders_dir / html.key, html.output,
                                    renderer.renders_dir / pdf.key / pdf.output if pdf else None,
                                    [dict(a) for a in assets])
            await self.app.db.execute(
                "UPDATE publications SET status = 'ok', last_error = NULL, version = ?, updated_at = ? WHERE id = ?",
                (pub["doc_version"], time.time(), pub_id))
            log.info("published", extra=fields(publication=pub_id, slug=pub["slug"]))
            return {"url": self.url_for(pub["slug"])}

    async def _failed(self, pub_id: int, message: str) -> None:
        await self.app.db.execute("UPDATE publications SET status = 'error', last_error = ? WHERE id = ?",
                                  (message[:500], pub_id))

    def _write_site(self, slug: str, render_dir: Path, output: str, pdf: Path | None, assets: list[dict]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / f".{slug}.{secrets.token_hex(4)}.tmp"
        shutil.copytree(render_dir, tmp, ignore=shutil.ignore_patterns("meta.json", "*.pdf"))
        if output != "index.html":
            os.replace(tmp / output, tmp / "index.html")
        for a in assets:
            if Path(a["path"]).suffix.lower() not in IMAGE_SUFFIXES:
                continue
            try:
                data = files.get_blob(self.app.cfg.data_dir, a["sha256"])
                files.atomic_write(tmp, a["path"], data)
            except (OSError, files.PathError):
                continue
        if pdf is not None:
            shutil.copyfile(pdf, tmp / "document.pdf")
            index = tmp / "index.html"
            page = index.read_text(encoding="utf-8")
            link = '<p class="docwizard-download"><a href="document.pdf" download>Download PDF</a></p>\n'
            page = page.replace("</body>", link + "</body>", 1) if "</body>" in page else page + link
            index.write_text(page, encoding="utf-8")
        target = self.root / slug
        old = self.root / f".{slug}.{secrets.token_hex(4)}.old"
        if target.exists():
            os.replace(target, old)
        os.replace(tmp, target)
        shutil.rmtree(old, ignore_errors=True)

    async def unpublish(self, pub_id: int) -> None:
        pub = await self.app.db.fetchone("SELECT slug FROM publications WHERE id = ?", (pub_id,))
        await self.app.db.execute("DELETE FROM publications WHERE id = ?", (pub_id,))
        if pub is not None:
            await asyncio.to_thread(self.remove_site, pub["slug"])

    async def prune(self) -> int:
        """Take down publications whose document or project was deleted (also by sync or a scan)."""
        rows = await self.app.db.fetchall(
            "SELECT p.id FROM publications p JOIN documents d ON d.id = p.doc_id JOIN projects pr ON pr.id = p.project_id"
            " WHERE d.deleted_at IS NOT NULL OR pr.deleted_at IS NOT NULL")
        for row in rows:
            await self.unpublish(row["id"])
        return len(rows)

    def remove_site(self, slug: str) -> None:
        if SLUG_RE.match(slug):
            shutil.rmtree(self.root / slug, ignore_errors=True)

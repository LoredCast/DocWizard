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
from . import edition, frontmatter

log = logging.getLogger("docwizard.publish")

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".avif"}
AUTO_DELAY = 10.0
SITE_CSP = ("default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self';"
            " base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


# Addresses that look like part of the server or of a well-known protocol, or that invite
# impersonation of whoever runs it. Published sites live on their own origin, but a reverse proxy
# may serve them under the app's domain, and people read these names as official.
RESERVED_SLUGS = frozenset({
    "abuse", "about", "account", "accounts", "admin", "administrator", "api", "app", "assets", "auth",
    "billing", "blog", "cdn", "contact", "dashboard", "dav", "docs", "docwizard", "download", "editor",
    "favicon", "forgot", "healthz", "help", "home", "imprint", "impressum", "index", "legal", "login",
    "logout", "mail", "metrics", "new", "oauth", "oidc", "official", "postmaster", "preview", "privacy",
    "projects", "public", "readyz", "register", "reset", "robots", "root", "security", "settings",
    "share", "signin", "signup", "sitemap", "static", "status", "support", "system", "terms", "verify",
    "webmaster", "well-known", "www",
})


class PublishError(Exception):
    pass


def check_slug(slug: str, current: str | None = None) -> str:
    """Validate an address. *current*, the publication's present address, stays allowed even if it
    was reserved later."""
    slug = slug.strip().lower()
    if current is not None and slug == current:
        return slug
    if not SLUG_RE.match(slug):
        raise PublishError("Addresses use 3 to 64 lowercase letters, digits and dashes, starting with a letter or "
                           "digit.")
    if slug.startswith("u-"):
        raise PublishError("Addresses starting with u- are kept for unlisted publications.")
    if slug in RESERVED_SLUGS or slug.removeprefix("_") in RESERVED_SLUGS:
        raise PublishError("This address is reserved. Choose another one.")
    return slug


def robots_tag(slug: str, index_public: bool) -> str | None:
    """The X-Robots-Tag for a published site: unlisted ones (u-...) are never indexed."""
    if slug.startswith("u-") or not index_public:
        return "noindex, nofollow"
    return None


ROBOTS_META = '<meta name="robots" content="noindex, nofollow">\n'


def unlisted_slug() -> str:
    return "u-" + secrets.token_hex(12)


class Publisher:
    def __init__(self, app):
        self.app = app
        self.root = Path(app.cfg.server.publish_dir)
        self._due: dict[str, float] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        app.rooms.change_hooks.append(self._changed)

    @property
    def locked_root(self) -> Path:
        """Sites behind a password: outside the folder the proxy serves, served by the app (§14.4)."""
        return Path(self.app.cfg.data_dir) / "publish-locked"

    def url_for(self, slug: str, locked: bool = False) -> str:
        base = self.app.cfg.server.publish_url.rstrip("/")
        return f"{base}/_site/{slug}/" if locked else f"{base}/{slug}/"

    def report_url(self, slug: str, locked: bool = False) -> str:
        """The reader's typo form, with ``{page}`` for the page it is about."""
        base = self.app.cfg.server.publish_url.rstrip("/")
        return (f"{base}/_site/{slug}/_report?page={{page}}" if locked else f"{base}/_report/{slug}/?page={{page}}")

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
            project = dict(await self.app.db.fetchone("SELECT * FROM projects WHERE id = ?", (pub["project_id"],)))
            renderer = self.app.renderer
            anonymize = bool(pub["anonymize"])
            html = await renderer.render(project, "html", doc_path=pub["doc_path"], anonymize=anonymize)
            if html is None or html.status != "ok":
                message = "; ".join(d.get("message", "") for d in (html.diagnostics if html else [])[:3])
                await self._failed(pub_id, message or "The document couldn't be rendered.")
                raise PublishError(message or "The document couldn't be rendered.")
            # Downloads: (rendered file, name in the site, label).
            downloads: list[tuple[Path, str, str]] = []
            for column, target, name, label in (("with_pdf", "pdf-typst", "document.pdf", "PDF"),
                                                ("with_epub", "epub", "document.epub", "EPUB"),
                                                ("with_docx", "docx", "document.docx", "Word (DOCX)")):
                if not pub[column]:
                    continue
                out = await renderer.render(project, target, doc_path=pub["doc_path"], anonymize=anonymize)
                if out is None or out.status != "ok":
                    message = f"The {label} couldn't be rendered."
                    await self._failed(pub_id, message)
                    raise PublishError(message)
                downloads.append((renderer.renders_dir / out.key / out.output, name, label))
            assets = await self.app.db.fetchall(
                "SELECT path, sha256 FROM assets WHERE project_id = ? AND deleted_at IS NULL", (pub["project_id"],))
            locked = bool(pub["password_hash"])
            report_url = self.report_url(pub["slug"], locked) if pub["reports"] else None
            pages = None
            if pub["edition"]:
                meta = await self._metadata(project, pub["doc_path"], anonymize)
                page = (renderer.renders_dir / html.key / html.output).read_text(encoding="utf-8")
                gettext = _site_gettext(meta["lang"])
                try:
                    built = await asyncio.to_thread(
                        edition.build, page, title=meta["title"] or pub["doc_path"], lang=meta["lang"],
                        cite=edition.cite_text(meta, self.url_for(pub["slug"], locked)),
                        downloads=[(label, name) for _src, name, label in downloads], report_url=report_url,
                        gettext=gettext)
                except ValueError as exc:
                    await self._failed(pub_id, str(exc))
                    raise PublishError(str(exc)) from None
                pages = built.pages
            await asyncio.to_thread(self._write_site, pub["slug"], renderer.renders_dir / html.key, html.output,
                                    downloads, [dict(a) for a in assets],
                                    robots_tag(pub["slug"], self.app.cfg.server.index_public_sites) is None,
                                    pages=pages, locked=locked, report_url=report_url)
            await self.app.db.execute(
                "UPDATE publications SET status = 'ok', last_error = NULL, version = ?, updated_at = ? WHERE id = ?",
                (pub["doc_version"], time.time(), pub_id))
            log.info("published", extra=fields(publication=pub_id, slug=pub["slug"]))
            return {"url": self.url_for(pub["slug"], locked)}

    async def _metadata(self, project: dict, doc_path: str, anonymize: bool) -> dict:
        """Title, authors, year and language of the published document, for "Cite this work"."""
        renderer = self.app.renderer
        snap, _ = await renderer.snapshot(project["id"])
        try:
            inputs = renderer.inputs_for(project, doc_path, snap)
        except Exception:  # noqa: BLE001 - the render itself succeeded
            inputs = [doc_path]
        text = snap.texts.get(inputs[0] if inputs else doc_path, "")
        title = frontmatter.value(text, "title")
        date = frontmatter.value(text, "date")
        year = re.match(r"\s*(\d{4})", date).group(1) if isinstance(date, str) and re.match(r"\s*\d{4}", date) \
            else time.strftime("%Y")
        lang = frontmatter.value(text, "lang") or project.get("lang") or "en"
        return {"title": title if isinstance(title, str) else "",
                "authors": [] if anonymize else frontmatter.author_names(text),
                "year": year, "lang": lang if isinstance(lang, str) else "en"}

    async def _failed(self, pub_id: int, message: str) -> None:
        await self.app.db.execute("UPDATE publications SET status = 'error', last_error = ? WHERE id = ?",
                                  (message[:500], pub_id))

    def _write_site(self, slug: str, render_dir: Path, output: str, downloads: list, assets: list[dict],
                    indexable: bool = True, *, pages: dict | None = None, locked: bool = False,
                    report_url: str | None = None) -> None:
        downloads = downloads or []
        root = self.locked_root if locked else self.root
        root.mkdir(parents=True, exist_ok=True)
        tmp = root / f".{slug}.{secrets.token_hex(4)}.tmp"
        shutil.copytree(render_dir, tmp, ignore=shutil.ignore_patterns("meta.json", "*.pdf"))
        if output != "index.html":
            os.replace(tmp / output, tmp / "index.html")
        if pages is not None:
            for name, page in pages.items():
                (tmp / name).write_text(page, encoding="utf-8")
        for a in assets:
            if Path(a["path"]).suffix.lower() not in IMAGE_SUFFIXES:
                continue
            try:
                data = files.get_blob(self.app.cfg.data_dir, a["sha256"])
                files.atomic_write(tmp, a["path"], data)
            except (OSError, files.PathError):
                continue
        for src, name, label in downloads:
            shutil.copyfile(src, tmp / name)
        if pages is None and (downloads or report_url):
            # A single page: the downloads and the typo form at its end.
            index = tmp / "index.html"
            page = index.read_text(encoding="utf-8")
            links = ""
            for _src, name, label in downloads:
                text = "Download PDF" if name == "document.pdf" else f"Download {label}"
                links += f'<p class="docwizard-download"><a href="{name}" download>{text}</a></p>\n'
            if report_url:
                links += (f'<p class="docwizard-report"><a href="{_attr(report_url.format(page="index.html"))}">'
                          "Report a typo</a></p>\n")
            page = page.replace("</body>", links + "</body>", 1) if "</body>" in page else page + links
            index.write_text(page, encoding="utf-8")
        if not indexable or locked:
            # The header covers the proxy; the tag also covers copies of the page served elsewhere.
            for index in tmp.glob("*.html"):
                page = index.read_text(encoding="utf-8")
                page = page.replace("</head>", ROBOTS_META + "</head>", 1) if "</head>" in page else ROBOTS_META + page
                index.write_text(page, encoding="utf-8")
        target = root / slug
        old = root / f".{slug}.{secrets.token_hex(4)}.old"
        if target.exists():
            os.replace(target, old)
        os.replace(tmp, target)
        shutil.rmtree(old, ignore_errors=True)
        # A site moves between the open and the locked folder when its password changes.
        other = (self.root if locked else self.locked_root) / slug
        if SLUG_RE.match(slug):
            shutil.rmtree(other, ignore_errors=True)

    async def unpublish(self, pub_id: int) -> None:
        pub = await self.app.db.fetchone("SELECT slug FROM publications WHERE id = ?", (pub_id,))
        await self.app.db.execute("DELETE FROM publications WHERE id = ?", (pub_id,))
        if pub is not None:
            await asyncio.to_thread(self.remove_site, pub["slug"])

    async def take_down(self, pub_id: int, admin_id: int | None, reason: str = "") -> str | None:
        """Unpublish for good: the address stays blocked, so the site can't come back under it."""
        pub = await self.app.db.fetchone("SELECT slug FROM publications WHERE id = ?", (pub_id,))
        if pub is None:
            return None
        await self.app.db.execute(
            "INSERT OR REPLACE INTO taken_down_slugs(slug, reason, taken_down_at, taken_down_by) VALUES (?, ?, ?, ?)",
            (pub["slug"], reason[:500] or None, time.time(), admin_id))
        await self.unpublish(pub_id)
        return pub["slug"]

    async def unpublish_owned_by(self, user_id: int) -> list[str]:
        """Every publication of the projects someone owns, for suspending an account."""
        rows = await self.app.db.fetchall(
            "SELECT p.id, p.slug FROM publications p JOIN projects pr ON pr.id = p.project_id WHERE pr.owner_id = ?",
            (user_id,))
        for row in rows:
            await self.unpublish(row["id"])
        return [row["slug"] for row in rows]

    async def is_taken_down(self, slug: str) -> bool:
        return await self.app.db.fetchone("SELECT 1 FROM taken_down_slugs WHERE slug = ?", (slug,)) is not None

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
            shutil.rmtree(self.locked_root / slug, ignore_errors=True)


def _attr(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def _site_gettext(lang: str):
    """The edition's own words, in the document's language when the interface has it."""
    from .. import i18n

    code = (lang or "en").split("-")[0].lower()
    code = code if code in i18n.LANGUAGES else "en"
    return lambda message, **values: i18n.gettext(code, message, **values)

"""Browse and install LaTeX templates from latextemplates.com (§9.3).

Uses the site's static, public machine-readable catalogue (``/index.json`` and
``/search.json``) and compile-verified ``/downloads/<slug>.zip`` archives, fetched
through DocWizard's outbound network policy (``netpolicy.Policy``).

Installed templates live as layout folders under ``<data_dir>/layouts/lt-<slug>``
with their source files in ``starter/``. No Lua filters are ever extracted from
external archives.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import shutil
import stat
import time
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from ..netpolicy import FetchError, PolicyError
from ..projects import files

DEFAULT_SITE = "https://latextemplates.com"
CACHE_TTL = 12 * 3600
MAX_CATALOG_BYTES = 5_000_000
MAX_ZIP_BYTES = 25_000_000
MAX_UNPACKED_BYTES = 40_000_000
MAX_FILE_BYTES = 15_000_000
MAX_FILES = 250
MAX_INSTALLED = 200
ENGINES = ("pdflatex", "xelatex", "lualatex")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,120}$")
_DOC_CLASS = re.compile(rb"\\documentclass(?:\[[^\]]*\])?\{")


class CatalogError(ValueError):
    """Raised when the template catalogue or archive cannot be fetched or unpacked."""


def site_url(app) -> str:
    return getattr(app, "latextemplates_url", DEFAULT_SITE).rstrip("/")


def layout_id_for(slug: str) -> str:
    """Deterministic DocWizard layout id (<= 63 chars, matching layouts._ID)."""
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise CatalogError("Invalid template identifier.")
    base = f"lt-{slug}"
    if len(base) <= 63:
        return base
    digest = hashlib.sha256(slug.encode()).hexdigest()[:8]
    return f"{base[:54].rstrip('-')}-{digest}"


def _cache_path(app) -> Path:
    return app.cfg.cache_dir / "latextemplates-catalog.json"


def _read_disk_cache(path: Path, expected_site: str) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("site") != expected_site:
        return None
    if not isinstance(data.get("templates"), list) or not isinstance(data.get("categories"), list):
        return None
    return data


def _write_disk_cache(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _parse_catalog(site: str, index_data: dict, search_data: list | None) -> dict:
    cats_raw = index_data.get("categories") or []
    templates_raw = index_data.get("templates") or []
    if not isinstance(cats_raw, list) or not isinstance(templates_raw, list):
        raise CatalogError("Unexpected catalogue format from latextemplates.com.")

    alt_by_cat_url: dict[str, list[str]] = {}
    desc_by_url: dict[str, str] = {}
    if isinstance(search_data, list):
        for rec in search_data:
            if not isinstance(rec, dict):
                continue
            kind = rec.get("kind")
            url = rec.get("url")
            if not isinstance(url, str):
                continue
            if kind == "category" and isinstance(rec.get("alt"), list):
                alt_by_cat_url[url] = [str(a).strip() for a in rec["alt"] if isinstance(a, str) and a.strip()]
            elif kind == "template" and isinstance(rec.get("text"), str):
                desc_by_url[url] = rec["text"].strip()

    cat_map: dict[str, dict] = {}
    for c in cats_raw:
        if not isinstance(c, dict):
            continue
        cslug = c.get("slug")
        ctitle = c.get("title")
        if not isinstance(cslug, str) or not isinstance(ctitle, str) or not SLUG_RE.match(cslug):
            continue
        curl = c.get("url") if isinstance(c.get("url"), str) else f"/cat/{cslug}/"
        cat_map[cslug] = {
            "slug": cslug,
            "title": ctitle.strip(),
            "blurb": str(c.get("blurb") or "").strip(),
            "alt": alt_by_cat_url.get(curl, []),
            "count": 0,
        }

    templates: list[dict] = []
    seen_slugs: set[str] = set()
    for t in templates_raw:
        if not isinstance(t, dict) or not t.get("rebuilt"):
            continue
        slug = t.get("slug")
        title = t.get("title")
        dl = t.get("downloadUrl")
        if (not isinstance(slug, str) or not SLUG_RE.match(slug) or slug in seen_slugs
                or not isinstance(title, str) or not title.strip()
                or not isinstance(dl, str) or not dl.startswith("/downloads/") or not dl.endswith(".zip")):
            continue
        if ".." in dl or "//" in dl or "\\" in dl:
            continue
        seen_slugs.add(slug)
        cslug = t.get("category") if isinstance(t.get("category"), str) else ""
        cat = cat_map.get(cslug)
        if cat is not None:
            cat["count"] += 1
        turl = t.get("url") if isinstance(t.get("url"), str) else f"/template/{slug}/"
        engine = t.get("engine") if t.get("engine") in ENGINES else "pdflatex"
        templates.append({
            "slug": slug,
            "layoutId": layout_id_for(slug),
            "title": title.strip()[:120],
            "category": cslug,
            "categoryTitle": cat["title"] if cat else cslug.replace("-", " ").title(),
            "description": desc_by_url.get(turl, "")[:500],
            "engine": engine,
            "hits": int(t.get("humanHits") or 0),
            "downloadPath": dl,
            "url": f"{site}/template/{slug}/",
            "pdfUrl": f"{site}/pdfs/{slug}.pdf",
        })

    templates.sort(key=lambda item: (-item["hits"], item["title"].lower()))
    categories = [c for c in cat_map.values() if c["count"] > 0]
    return {
        "version": 1,
        "site": site,
        "fetchedAt": time.time(),
        "categories": categories,
        "templates": templates,
    }


def load_catalog(app, *, refresh: bool = False) -> dict:
    """Fetch or return the cached latextemplates.com catalogue (blocking)."""
    site = site_url(app)
    now = time.time()
    mem = getattr(app, "_lt_catalog", None)
    if not refresh and isinstance(mem, dict) and mem.get("site") == site and now - mem.get("fetchedAt", 0) < CACHE_TTL:
        return mem

    path = _cache_path(app)
    disk = _read_disk_cache(path, site)
    if not refresh and disk is not None and now - disk.get("fetchedAt", 0) < CACHE_TTL:
        app._lt_catalog = disk
        return disk

    net = app.net
    try:
        idx_resp = net.fetch("GET", f"{site}/index.json", max_bytes=MAX_CATALOG_BYTES)
        if idx_resp.status != 200:
            raise CatalogError(f"latextemplates.com returned HTTP {idx_resp.status}.")
        index_data = json.loads(idx_resp.body.decode("utf-8"))
        search_data = None
        try:
            s_resp = net.fetch("GET", f"{site}/search.json", max_bytes=MAX_CATALOG_BYTES)
            if s_resp.status == 200:
                search_data = json.loads(s_resp.body.decode("utf-8"))
        except (PolicyError, FetchError, ValueError):
            search_data = None
    except (PolicyError, FetchError, ValueError, CatalogError) as exc:
        if disk is not None:
            app._lt_catalog = disk
            return disk
        raise CatalogError(f"Couldn't load templates from latextemplates.com: {exc}") from None

    catalog = _parse_catalog(site, index_data, search_data)
    app._lt_catalog = catalog
    _write_disk_cache(path, catalog)
    return catalog


def search_catalog(catalog: dict, *, q: str = "", category: str = "", engine: str = "",
                   offset: int = 0, limit: int = 24, installed_ids: set[str] | None = None) -> dict:
    """Filter, rank and paginate the catalogue."""
    installed = installed_ids or set()
    terms = [tok.lower() for tok in q.strip().split() if tok.strip()]
    cat_alt = {c["slug"]: " ".join(c.get("alt") or []).lower() for c in catalog.get("categories", [])}

    matched: list[tuple[int, int, str, dict]] = []
    for item in catalog.get("templates", []):
        if category and item["category"] != category:
            continue
        if engine and item["engine"] != engine:
            continue
        rank = 0
        if terms:
            title_l = item["title"].lower()
            slug_l = item["slug"].lower()
            cat_l = f"{item['categoryTitle'].lower()} {cat_alt.get(item['category'], '')}"
            desc_l = item["description"].lower()
            hay = f"{title_l} {slug_l} {cat_l} {desc_l}"
            if not all(term in hay for term in terms):
                continue
            if all(term in title_l or term in slug_l for term in terms):
                rank = 0 if title_l.startswith(terms[0]) or slug_l.startswith(terms[0]) else 1
            elif all(term in title_l or term in cat_l for term in terms):
                rank = 2
            else:
                rank = 3
        matched.append((rank, -item["hits"], item["title"].lower(), item))

    matched.sort(key=lambda row: (row[0], row[1], row[2]))
    offset = max(0, offset)
    limit = max(1, min(60, limit))
    page = [
        {
            "slug": item["slug"],
            "layoutId": item["layoutId"],
            "title": item["title"],
            "category": item["category"],
            "categoryTitle": item["categoryTitle"],
            "description": item["description"],
            "engine": item["engine"],
            "hits": item["hits"],
            "url": item["url"],
            "pdfUrl": item["pdfUrl"],
            "installed": item["layoutId"] in installed,
        }
        for _, _, _, item in matched[offset:offset + limit]
    ]
    return {
        "total": len(matched),
        "offset": offset,
        "limit": limit,
        "categories": [
            {"slug": c["slug"], "title": c["title"], "count": c["count"]}
            for c in catalog.get("categories", [])
        ],
        "engines": list(ENGINES),
        "items": page,
    }


def _strip_common_root(paths: list[str]) -> list[str]:
    if len(paths) < 1:
        return paths
    split = [p.split("/") for p in paths]
    if all(len(parts) > 1 and parts[0] == split[0][0] for parts in split):
        return ["/".join(parts[1:]) for parts in split]
    return paths


def _extract_zip(data: bytes) -> tuple[dict[str, bytes], str]:
    """Validate and unpack a latextemplates.com archive into {relative_path: bytes} and main .tex."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise CatalogError("The downloaded template archive is not a valid zip file.") from None

    raw_entries: list[tuple[str, zipfile.ZipInfo]] = []
    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            if info.flag_bits & 0x1:
                raise CatalogError("Encrypted template archives aren't supported.")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                continue
            name = info.filename.replace("\\", "/").lstrip("/")
            parts = [p for p in name.split("/") if p]
            if not parts or parts[0] == "__MACOSX" or any(p.startswith(".") for p in parts):
                continue
            if parts[-1].lower().endswith(".lua"):
                # Never extract Lua code from external archives.
                continue
            raw_entries.append(("/".join(parts), info))

        if not raw_entries:
            raise CatalogError("The template archive contains no usable files.")
        if len(raw_entries) > MAX_FILES:
            raise CatalogError(f"The template archive has too many files (limit {MAX_FILES}).")

        stripped = _strip_common_root([p for p, _ in raw_entries])
        extracted: dict[str, bytes] = {}
        total = 0
        for rel_raw, (_, info) in zip(stripped, raw_entries, strict=True):
            try:
                rel = files.normalize(rel_raw)
            except files.PathError:
                continue
            if any(part.startswith(".") for part in rel.split("/")):
                continue
            with zf.open(info, "r") as fh:
                chunk = fh.read(MAX_FILE_BYTES + 1)
            if len(chunk) > MAX_FILE_BYTES:
                raise CatalogError(f"'{rel}' exceeds the maximum file size.")
            total += len(chunk)
            if total > MAX_UNPACKED_BYTES:
                raise CatalogError("The unpacked template exceeds the maximum size.")
            extracted[rel] = chunk

    main_tex = _detect_main_tex(extracted)
    if main_tex is None:
        raise CatalogError("The template archive doesn't contain a .tex file.")
    return extracted, main_tex


def _detect_main_tex(extracted: dict[str, bytes]) -> str | None:
    tex_files = sorted(
        (p for p in extracted if p.lower().endswith(".tex")),
        key=lambda p: (p.count("/"), p.lower() != "main.tex", p.lower()),
    )
    if not tex_files:
        return None
    for p in tex_files:
        if os.path.basename(p).lower() == "main.tex" and p.count("/") == 0:
            return p
    for p in tex_files:
        if _DOC_CLASS.search(extracted[p][:8192]):
            return p
    return tex_files[0]


def install_template(app, slug: str, *, user_id: int) -> tuple[str, bool]:
    """Download and install *slug* from latextemplates.com into data_dir/layouts/.

    Returns ``(layout_id, already_installed)``. Blocking: call via ``asyncio.to_thread``.
    """
    lid = layout_id_for(slug)
    layouts_root = app.cfg.data_dir / "layouts"
    dest = layouts_root / lid
    if dest.is_dir() and app.layouts.exists(lid):
        return lid, True

    catalog = load_catalog(app)
    entry = next((t for t in catalog.get("templates", []) if t["slug"] == slug), None)
    if entry is None:
        catalog = load_catalog(app, refresh=True)
        entry = next((t for t in catalog.get("templates", []) if t["slug"] == slug), None)
    if entry is None:
        raise CatalogError("This template isn't available for download on latextemplates.com.")

    layouts_root.mkdir(parents=True, exist_ok=True)
    installed_count = sum(1 for p in layouts_root.iterdir() if p.is_dir() and p.name.startswith("lt-"))
    if installed_count >= MAX_INSTALLED:
        raise CatalogError("The server reached the limit of installed LaTeX templates.")

    site = site_url(app)
    download_url = f"{site}{entry['downloadPath']}"
    if urlsplit(download_url).netloc.lower() != urlsplit(site).netloc.lower():
        raise CatalogError("Invalid download location.")

    try:
        resp = app.net.fetch("GET", download_url, max_bytes=MAX_ZIP_BYTES)
    except (PolicyError, FetchError) as exc:
        raise CatalogError(f"Couldn't download the template: {exc}") from None
    if resp.status != 200:
        raise CatalogError(f"Downloading the template failed (HTTP {resp.status}).")

    extracted, main_tex = _extract_zip(resp.body)
    manifest = {
        "id": lid,
        "name": entry["title"],
        "version": "1.0.0",
        "extends": "default",
        "description": entry["description"] or f"LaTeX template ({entry['categoryTitle']}) from latextemplates.com.",
        "defaultPdf": "pdf-latex",
        "targets": {
            "pdf-latex": {"engine": entry["engine"]},
        },
        "starter": "latex-bundle",
        "starterMain": main_tex,
        "source": {
            "provider": "latextemplates",
            "slug": slug,
            "category": entry["categoryTitle"],
            "url": entry["url"],
            "pdfUrl": entry["pdfUrl"],
            "installedBy": user_id,
        },
    }

    tmp = layouts_root / f".{lid}.tmp-{secrets.token_hex(4)}"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        starter_root = tmp / "starter"
        starter_root.mkdir(parents=True, exist_ok=True)
        for rel, content in extracted.items():
            files.atomic_write(starter_root, rel, content)
        (tmp / "layout.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            tmp.rename(dest)
        except OSError:
            if dest.is_dir():
                shutil.rmtree(tmp, ignore_errors=True)
                app.layouts.reload()
                if app.layouts.exists(lid):
                    return lid, True
            raise
        app.layouts.reload()
        if not app.layouts.exists(lid):
            shutil.rmtree(dest, ignore_errors=True)
            app.layouts.reload()
            raise CatalogError("The downloaded template couldn't be loaded as a layout.")
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return lid, False


def uninstall_template(app, lid: str, *, user_id: int, is_admin: bool) -> None:
    """Remove an installed latextemplates.com layout folder and reload layouts."""
    if not app.layouts.exists(lid):
        raise LookupError("No such layout.")
    lay = app.layouts.get(lid)
    src = lay.external_source
    if not src or src.get("provider") != "latextemplates":
        raise CatalogError("Only templates installed from latextemplates.com can be uninstalled here.")
    if not is_admin and src.get("installedBy") != user_id:
        raise PermissionError("Only the person who installed this template or an admin can uninstall it.")
    children = [x.name for x in app.layouts.list() if x.parent == lid]
    if children:
        raise CatalogError(f"Other layouts are based on this one ({', '.join(children[:3])}). Delete or change "
                           "them first.")
    layouts_root = (app.cfg.data_dir / "layouts").resolve()
    dest = (layouts_root / lid).resolve()
    if dest.parent != layouts_root or not dest.is_dir():
        raise LookupError("No such installed template.")
    shutil.rmtree(dest)
    app.layouts.reload()

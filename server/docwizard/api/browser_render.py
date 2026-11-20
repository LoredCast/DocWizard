"""Previews rendered in the browser (ARCHITECTURE.md §6.4).

The worker's job for a Typst PDF, handed to the browser: the same pandoc options, layout files,
filters, citation style and project files, so pandoc and Typst compiled to WebAssembly
(``web/vendor/browser-render/``, installed by ``deploy/fetch_tools.py --browser-render``) produce
the PDF the worker would, without the network. The browser keeps the kit, so the preview also works
offline, and puts the text being typed in place of the document's saved text.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
from pathlib import Path

from ..http.request import HTTPError, Request, bad_request
from ..http.response import Response
from ..render import jobs
from .projects import document_access, project_access

KIT_VERSION = 1
# Pictures larger than this in all go without: the preview then shows the PDF without them.
MAX_ASSET_BYTES = 12_000_000
MAX_LAYOUT_FILE = 2_000_000
LAYOUT_ROOT = "layout"  # where the layout folder sits in the browser's file system


def installed() -> bool:
    from ..asgi import WEB_DIR

    root = WEB_DIR / "vendor" / "browser-render"
    return (root / "pandoc.wasm").is_file() and (root / "typst_ts_web_compiler_bg.wasm").is_file()


def fonts(app) -> list[str]:
    from ..asgi import WEB_DIR

    folder = WEB_DIR / "vendor" / "browser-render" / "fonts"
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir() if p.suffix.lower() in (".ttf", ".otf"))


def pandoc_options(args: list[str], layout_dir: str) -> dict:
    """Turn the worker's pandoc arguments into the defaults-file options pandoc.wasm takes. Paths in
    the layout folder move to layout/, and the job's ../options.json to options.json."""
    def path(p: str) -> str:
        if p.startswith(layout_dir + "/"):
            return f"{LAYOUT_ROOT}/{p[len(layout_dir) + 1:]}"
        return "options.json" if p == "../options.json" else p

    opts: dict = {"input-files": [], "filters": []}
    i = 0
    while i < len(args):
        a = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else None
        if not a.startswith("--") and a != "-o":
            opts["input-files"].append(a)
        elif a == "--from":
            opts["from"], i = nxt, i + 1
        elif a == "--to":
            opts["to"], i = nxt, i + 1
        elif a == "--standalone":
            opts["standalone"] = True
        elif a == "--metadata-file":
            opts.setdefault("metadata-files", []).append(path(nxt))
            i += 1
        elif a == "--resource-path":
            opts["resource-path"] = [path(p) for p in nxt.split(":")]
            i += 1
        elif a == "--lua-filter":
            opts["filters"].append({"type": "lua", "path": path(nxt)})
            i += 1
        elif a == "--citeproc":
            opts["filters"].append({"type": "citeproc"})
        elif a == "--toc":
            opts["table-of-contents"] = True
        elif a == "--template":
            opts["template"], i = path(nxt), i + 1
        elif a == "-o":
            opts["output-file"], i = nxt, i + 1
        else:
            raise ValueError(f"unexpected pandoc argument {a}")
        i += 1
    return opts


def _file_entry(data: bytes) -> dict:
    try:
        return {"text": data.decode("utf-8")}
    except UnicodeDecodeError:
        return {"b64": base64.b64encode(data).decode("ascii")}


def build_kit(app, project: dict, doc_path: str | None, snap: jobs.ProjectSnapshot, layout, options: dict,
              inputs: list[str], layout_dir: Path, approved: bool) -> dict:
    steps, _ = jobs.pandoc_steps(layout, layout_dir, "pdf-typst", inputs, options, approved)
    if len(steps) != 2 or steps[0]["tool"] != "pandoc" or steps[1]["tool"] != "typst":
        raise bad_request("This document can't be previewed in the browser.")
    L = str(layout_dir)
    pandoc = pandoc_options(steps[0]["args"], L)
    meta = jobs.metadata(layout, options, inputs, layout_dir, lang=project.get("lang") or None)
    files: dict[str, dict] = {"options.json": {"text": json.dumps(meta)}}
    # The layout: templates, generated design files, filters; of the bundled styles only the one in use.
    csl = meta.get("csl") if isinstance(meta.get("csl"), str) else None
    for p in sorted(layout_dir.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(layout_dir).as_posix()
        if rel.startswith("_csl/") and (csl is None or rel != f"_csl/{os.path.basename(csl)}"):
            continue
        if p.stat().st_size > MAX_LAYOUT_FILE:
            continue
        files[f"{LAYOUT_ROOT}/{rel}"] = _file_entry(p.read_bytes())
    if csl and os.path.isabs(csl):  # the layout's own style, by its path in the layout folder
        try:
            meta["csl"] = f"{LAYOUT_ROOT}/{Path(csl).relative_to(layout_dir).as_posix()}"
            files["options.json"] = {"text": json.dumps(meta)}
        except ValueError:
            pass
    # Filters find the shared module (require "dwlocale") and its data next to where pandoc runs.
    for name in ("dwlocale.lua", "locales.json"):
        entry = files.get(f"{LAYOUT_ROOT}/_builtin/{name}")
        if entry:
            files[name] = entry
    # The project's text files, as a render job sees them (src/ is the folder pandoc runs in).
    for path, text in snap.texts.items():
        files[f"src/{path}"] = {"text": jobs.with_short_titles(text) if path.endswith(".json") else text}
    assets, skipped, total = {}, [], 0
    for path, (sha, blob) in sorted(snap.assets.items()):
        try:
            size = blob.stat().st_size
        except OSError:
            continue
        if total + size > MAX_ASSET_BYTES:
            skipped.append(path)
            continue
        total += size
        assets[f"src/{path}"] = {"b64": base64.b64encode(blob.read_bytes()).decode("ascii")}
    files.update(assets)
    # Fonts the layout names, from the browser renderer's set; the first serif family as a fallback.
    text = " ".join(e["text"] for k, e in files.items() if k.startswith(f"{LAYOUT_ROOT}/") and "text" in e)
    available = fonts(app)
    families = sorted({re.split(r"[-.]", f)[0] for f in available})
    used = [fam for fam in families if re.search(rf"\b{re.escape(fam)}\b", text, re.I)
            or re.search(rf"\b{re.escape(re.sub(r'(?<!^)([A-Z])', r' \1', fam))}\b", text)]
    for fallback in ("Tinos", "NotoSerif"):
        if fallback in families and fallback not in used:
            used.append(fallback)
    layout_fonts = [k for k in files if k.startswith(f"{LAYOUT_ROOT}/fonts/") and k.lower().endswith((".ttf", ".otf"))]
    digest = hashlib.sha256(json.dumps({"layout": layout.content_sha256, "options": options, "inputs": inputs,
                                        "lang": project.get("lang"), "kit": KIT_VERSION}, sort_keys=True).encode())
    return {
        "version": KIT_VERSION,
        "key": digest.hexdigest()[:32],
        "project": project["id"],
        "doc": doc_path,
        "inputs": inputs,
        "pandoc": {**pandoc, "output-file": "src/.docwizard.typ",
                   "input-files": [f"src/{p}" for p in pandoc["input-files"]],
                   "resource-path": [("src" if p == "." else p) for p in pandoc.get("resource-path", ["."])]},
        "typst": {"main": "src/.docwizard.typ",
                  "fonts": [f"{app.static_prefix}/vendor/browser-render/fonts/{f}" for f in available
                            if re.split(r"[-.]", f)[0] in used],
                  "layoutFonts": layout_fonts},
        "files": files,
        "skipped": skipped,
        "tools": app.renderer.worker.versions,
    }


def register(app) -> None:
    renderer = app.renderer

    async def kit(req: Request) -> Response:
        if not installed():
            raise HTTPError(404, "not_installed", "Previews in the browser aren't installed on this server.")
        project, _ = await project_access(app, req, req.param("id"), "read")
        project = dict(project)
        doc_path = None
        if req.query.get("doc"):
            doc, _, _ = await document_access(app, req, req.query["doc"], "read")
            if doc["project_id"] != project["id"]:
                raise bad_request("That document is in another project.")
            doc_path = doc["path"]
        layout = renderer.resolve_layout(project, None)
        try:
            options = layout.validate_options(json.loads(project.get("layout_options") or "{}"))
        except ValueError:
            options = {}
        snap, _ = await renderer.snapshot(project["id"])
        inputs = renderer.inputs_for(project, doc_path, snap)
        if not inputs or inputs[0].endswith(".tex"):
            raise bad_request("LaTeX documents are built on the server.")
        layout_dir = await asyncio.to_thread(app.layouts.cached_dir, layout, "layouts")
        approved = app.layouts.filters_approved(layout)
        data = await asyncio.to_thread(build_kit, app, project, doc_path, snap, layout, options, inputs,
                                       layout_dir, approved)
        body = json.dumps(data, separators=(",", ":"), sort_keys=True).encode()
        etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
        if req.headers.get("if-none-match") == etag:
            resp = Response(b"", status=304)
        else:
            resp = Response(body, content_type="application/json")
        resp.set_header("etag", etag)
        resp.set_header("cache-control", "private, no-cache")
        return resp

    app.router.add("GET", "/api/projects/{id}/render-kit", kit, guest=True)

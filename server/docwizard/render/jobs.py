"""Render jobs: project snapshots, job directories and tool steps (§9.1, §9.2, §9.5).

The app decides everything about a job: which files it sees, which layout
files and approved filters it uses, and the exact tool arguments. The worker
only runs the listed steps in its sandbox.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..projects import files
from . import design as designs
from .layouts import Layout

OUTPUTS = {
    "html": "index.html",
    "pdf-typst": "document.pdf",
    "pdf-latex": "document.pdf",
    "docx": "document.docx",
    "odt": "document.odt",
    "md-resolved": "document.md",
    "md-gfm": "document.md",
    "md-commonmark": "document.md",
    "typ-source": "document.typ",
    "tex-source": "document.tex",
    "md-source": "project.zip",
}
TARGETS = set(OUTPUTS)
PREVIEW_TARGETS = {"html", "pdf-typst", "pdf-latex"}
# §9.6: no shell escape, file-line errors for diagnostics, SyncTeX for later source mapping. The worker
# replaces {job} with the job directory: kpathsea's paranoid mode refuses ../ paths but allows
# absolute ones below TEXMFOUTPUT, which the worker sets to the job directory.
LATEXMK = ["-interaction=nonstopmode", "-halt-on-error", "-file-line-error", "-no-shell-escape", "-synctex=1",
           "-jobname=document", "-outdir={job}/out", "-auxdir={job}/tmp/latex"]
LATEX_ENGINES = {"lualatex", "xelatex", "pdflatex"}
BUILTIN_FILTERS = {"crossref", "pagebreak", "wordcount"}


@dataclass
class ProjectSnapshot:
    project_id: str
    texts: dict[str, str] = field(default_factory=dict)  # path -> text
    assets: dict[str, tuple[str, Path]] = field(default_factory=dict)  # path -> (sha256, blob path)

    def digest(self) -> str:
        h = hashlib.sha256()
        for path in sorted(self.texts):
            h.update(b"t\0" + path.encode() + b"\0" + hashlib.sha256(self.texts[path].encode()).digest())
        for path in sorted(self.assets):
            h.update(b"a\0" + path.encode() + b"\0" + self.assets[path][0].encode())
        return h.hexdigest()

    def write(self, dest: Path) -> None:
        for path, text in self.texts.items():
            files.atomic_write(dest, path, text.encode("utf-8"))
        for path, (_, blob) in self.assets.items():
            target = dest / path
            target.parent.mkdir(parents=True, exist_ok=True)
            # Always a copy: a hard link would hand the blob itself to the job's user.
            shutil.copyfile(blob, target)


def cache_key(project_id: str, snapshot_digest: str, layout: Layout, target: str, inputs: list[str], options: dict,
              versions: dict) -> str:
    """Renders are cached per project: outputs are served only to the project's members."""
    h = hashlib.sha256()
    h.update(json.dumps({"project": project_id, "files": snapshot_digest, "layout": layout.content_sha256, "target": target,
                         "inputs": inputs, "options": options, "tools": versions}, sort_keys=True).encode())
    return h.hexdigest()


def new_job_id() -> str:
    return "j_" + secrets.token_hex(16)


def fonts_dir() -> str:
    """Fonts every layout can use, installed in the worker image by deploy/fetch_tools.py."""
    return os.environ.get("DOCWIZARD_FONTS_DIR") or designs.SHARED_FONTS_DIR


CSL_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")


def metadata(layout: Layout, project_options: dict, inputs: list[str], layout_dir: Path | None = None) -> dict:
    """Option precedence: front matter, then project settings, then layout defaults (§9.3).

    The values go in a --metadata-file, which pandoc lets the document override.
    """
    meta = {k: v for k, v in layout.option_defaults().items() if v is not None and v != ""}
    meta.update({k: v for k, v in project_options.items() if v is not None and v != ""})
    defaults = layout.safe_defaults()
    for key in ("metadata", "variables"):
        if isinstance(defaults.get(key), dict):
            for k, v in defaults[key].items():
                meta.setdefault(k, v)
    if "link-citations" in defaults:
        meta.setdefault("link-citations", defaults["link-citations"])
    meta.setdefault("pagetitle", os.path.splitext(os.path.basename(inputs[0]))[0] if inputs else "Document")
    # Design settings that pandoc itself handles. The document's front matter still wins.
    design = layout.design
    if design["text.lang"]:
        meta.setdefault("lang", design["text.lang"])
    if design["bib.title"]:
        meta.setdefault("reference-section-title", design["bib.title"])
    meta.setdefault("link-citations", design["cite.link"])
    # The citation style: a layout option naming a bundled style, else the layout's own CSL
    # file, else the design's style. Front matter `csl:` still wins, since the document
    # overrides the metadata file.
    style = meta.pop("citation-style", None)
    if layout_dir is not None:
        own = layout.manifest.get("csl")
        for choice in (style, own, design["cite.style"]):
            if not isinstance(choice, str) or not choice:
                continue
            bundled = layout_dir / "_csl" / f"{choice.removesuffix('.csl')}.csl"
            if CSL_ID.match(choice.removesuffix(".csl")) and bundled.is_file():
                meta["csl"] = bundled.name  # found through --resource-path, so no server path shows in outputs
                break
            if choice is own and choice is not None and ".." not in choice and (layout_dir / choice).is_file():
                meta["csl"] = str(layout_dir / choice)
                break
    return meta


def latex_steps(layout: Layout, target: str, root: str) -> tuple[list[dict], list]:
    """LaTeX projects build their root document with latexmk; pandoc isn't involved (§9.5)."""
    if target != "pdf-latex":
        raise ValueError("LaTeX documents export as PDF (LaTeX) or as the project source.")
    engine = layout.targets.get("pdf-latex", {}).get("engine")
    engine = engine if engine in LATEX_ENGINES else "lualatex"
    return [{"tool": "latexmk", "args": [f"-{engine}", *LATEXMK, root], "log": "latex"}], []


def pandoc_steps(layout: Layout, layout_dir: Path, target: str, inputs: list[str], options: dict,
                 approved_layout_filters: bool) -> tuple[list[dict], list[tuple[Path, str]]]:
    """Return (steps, files to copy into out/) for *target*. Paths are relative to the job's src/."""
    L = str(layout_dir)
    if inputs and inputs[0].endswith(".tex"):
        return latex_steps(layout, target, inputs[0])
    spec = layout.targets.get(target if target != "typ-source" else "pdf-typst", {})
    common = [*inputs, "--from", "markdown", "--standalone", "--metadata-file", "../options.json",
              "--resource-path", f".:{L}/_csl"]
    for name in layout.manifest.get("filters", []):
        if name in BUILTIN_FILTERS:
            common += ["--lua-filter", f"{L}/_builtin/{name}.lua"]
        elif approved_layout_filters and isinstance(name, str) and name.startswith("filters/") \
                and name.endswith(".lua") and ".." not in name:
            common += ["--lua-filter", f"{L}/{name}"]
    # zotero: bibliography entries point at the job's copy of the library (§9.4).
    common += ["--lua-filter", f"{L}/_builtin/zotero.lua", "--citeproc"]
    if options.get("toc", layout.design["toc.enabled"]) is True:
        common.append("--toc")
    copies: list[tuple[Path, str]] = []
    out = f"../out/{OUTPUTS[target]}"
    if target == "html":
        args = [*common, "--to", "html5", "--mathml", "--section-divs"]
        if spec.get("template"):
            args += ["--template", f"{L}/{spec['template']}"]
        for i, css in enumerate(spec.get("css", [])):
            name = f"_layout/{i}-{os.path.basename(css)}"
            copies.append((layout_dir / css, name))
            args += ["--css", name]
        return [{"tool": "pandoc", "args": [*args, "-o", out]}], copies
    if target in ("pdf-typst", "typ-source"):
        args = [*common, "--to", "typst"]
        if spec.get("template"):
            args += ["--template", f"{L}/{spec['template']}"]
        if target == "typ-source":
            return [{"tool": "pandoc", "args": [*args, "-o", out]}], copies
        typ = ".docwizard.typ"
        typst = ["compile", "--root", "..", "--ignore-system-fonts", "--font-path", f"{L}/fonts",
                 "--font-path", fonts_dir(), typ, out]
        return [{"tool": "pandoc", "args": [*args, "-o", typ]}, {"tool": "typst", "args": typst}], copies
    if target == "pdf-latex":
        args = [*common, "--to", "latex"]
        if spec.get("template"):
            args += ["--template", f"{L}/{spec['template']}"]
        tex = "_docwizard.tex"  # TeX refuses dot files in paranoid mode
        engine = spec.get("engine") if spec.get("engine") in LATEX_ENGINES else "lualatex"
        return [{"tool": "pandoc", "args": [*args, "-o", tex]},
                {"tool": "latexmk", "args": [f"-{engine}", *LATEXMK, tex], "log": "latex"}], copies
    if target in ("docx", "odt"):
        args = [*common, "--to", target]
        ref = spec.get("reference")
        if ref and (layout_dir / ref).is_file():
            args += ["--reference-doc", f"{L}/{ref}"]
        return [{"tool": "pandoc", "args": [*args, "-o", out]}], copies
    writer = {"md-resolved": "markdown-citations", "md-gfm": "gfm", "md-commonmark": "commonmark_x",
              "tex-source": "latex"}[target]
    return [{"tool": "pandoc", "args": [*common, "--to", writer, "-o", out]}], copies

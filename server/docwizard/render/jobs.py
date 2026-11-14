"""Render jobs: project snapshots, job directories and tool steps (§9.1, §9.2, §9.5).

The app decides everything about a job: which files it sees, which layout
files and approved filters it uses, and the exact tool arguments. The worker
only runs the listed steps in its sandbox.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..projects import files
from .layouts import Layout

OUTPUTS = {
    "html": "index.html",
    "pdf-typst": "document.pdf",
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
PREVIEW_TARGETS = {"html", "pdf-typst"}
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


def metadata(layout: Layout, project_options: dict, inputs: list[str]) -> dict:
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
    return meta


def pandoc_steps(layout: Layout, layout_dir: Path, target: str, inputs: list[str], options: dict,
                 approved_layout_filters: bool) -> tuple[list[dict], list[tuple[Path, str]]]:
    """Return (steps, files to copy into out/) for *target*. Paths are relative to the job's src/."""
    L = str(layout_dir)
    spec = layout.targets.get(target if target != "typ-source" else "pdf-typst", {})
    common = [*inputs, "--from", "markdown", "--standalone", "--metadata-file", "../options.json",
              "--resource-path", "."]
    for name in layout.manifest.get("filters", []):
        if name in BUILTIN_FILTERS:
            common += ["--lua-filter", f"{L}/_builtin/{name}.lua"]
        elif approved_layout_filters and isinstance(name, str) and name.startswith("filters/") \
                and name.endswith(".lua") and ".." not in name:
            common += ["--lua-filter", f"{L}/{name}"]
    # zotero: bibliography entries point at the job's copy of the library (§9.4).
    common += ["--lua-filter", f"{L}/_builtin/zotero.lua", "--citeproc"]
    if options.get("toc") is True:
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
        typst = ["compile", "--root", "..", "--ignore-system-fonts", "--font-path", f"{L}/fonts", typ, out]
        return [{"tool": "pandoc", "args": [*args, "-o", typ]}, {"tool": "typst", "args": typst}], copies
    if target in ("docx", "odt"):
        args = [*common, "--to", target]
        ref = spec.get("reference")
        if ref and (layout_dir / ref).is_file():
            args += ["--reference-doc", f"{L}/{ref}"]
        return [{"tool": "pandoc", "args": [*args, "-o", out]}], copies
    writer = {"md-resolved": "markdown-citations", "md-gfm": "gfm", "md-commonmark": "commonmark_x",
              "tex-source": "latex"}[target]
    return [{"tool": "pandoc", "args": [*common, "--to", writer, "-o", out]}], copies

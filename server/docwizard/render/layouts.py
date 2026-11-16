"""Layouts: manifests, inheritance, option schemas and the defaults allowlist (§9.3).

A layout is a folder with ``layout.json``. With ``extends`` it inherits every
file it doesn't provide from its parent, and manifests are merged with the
child winning. Resolved layouts are copied once per content hash into the
shared cache, which the render worker mounts read-only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import design as designs

REPO_LAYOUTS = Path(__file__).resolve().parents[3] / "layouts"
REPO_FILTERS = Path(__file__).resolve().parents[3] / "filters"
REPO_CSL = Path(__file__).resolve().parents[3] / "csl"
_BUILTIN_HASH: list[str] = []


def builtin_filters_hash() -> str:
    """Changes when DocWizard's own filters, styles or design code change, so cached layout copies get them."""
    if not _BUILTIN_HASH:
        h = hashlib.sha256(designs.GENERATOR_HASH.encode())
        for f in sorted([*REPO_FILTERS.glob("*.lua"), *REPO_FILTERS.glob("*.json")]) if REPO_FILTERS.is_dir() else []:
            h.update(f.name.encode() + b"\0" + f.read_bytes())
        for f in sorted(REPO_CSL.glob("*.csl")) if REPO_CSL.is_dir() else []:
            h.update(f.name.encode() + b"\0" + f.read_bytes())
        _BUILTIN_HASH.append(h.hexdigest())
    return _BUILTIN_HASH[0]

# Only these pandoc defaults keys are passed on (§9.3).
DEFAULTS_ALLOWLIST = {
    "variables", "metadata", "table-of-contents", "toc", "toc-depth", "number-sections", "number-offset",
    "top-level-division", "highlight-style", "syntax-highlighting", "section-divs", "html-q-tags", "ascii",
    "reference-links", "reference-location", "dpi", "wrap", "columns", "tab-stop", "indented-code-classes",
    "list-tables", "link-citations", "link-bibliography", "notes-after-punctuation", "cite-method",
}
TARGET_KEYS = {"template", "css", "reference", "engine"}
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def bundled_styles() -> dict[str, str]:
    """Citation styles shipped with DocWizard: id -> title."""
    out = {}
    for f in sorted(REPO_CSL.glob("*.csl")) if REPO_CSL.is_dir() else []:
        m = re.search(r"<title>([^<]+)</title>", f.read_text(encoding="utf-8", errors="replace")[:4000])
        out[f.stem] = m.group(1) if m else f.stem
    return out


class LayoutError(ValueError):
    pass


@dataclass
class Layout:
    id: str
    dirs: list[Path]  # own folder first, then ancestors
    manifest: dict
    content_sha256: str = ""
    files: dict[str, Path] = field(default_factory=dict)  # relative path -> source file
    owner: int | None = None  # custom layouts made in the layout editor
    shared: bool = True
    parent: str | None = None

    @property
    def name(self) -> str:
        return self.manifest.get("name", self.id)

    @property
    def description(self) -> str:
        return self.manifest.get("description", "")

    @property
    def targets(self) -> dict:
        return self.manifest.get("targets", {})

    @property
    def variables(self) -> dict:
        return self.manifest.get("variables", {})

    @property
    def default_pdf(self) -> str:
        return self.manifest.get("defaultPdf", "pdf-typst")

    @property
    def design(self) -> dict:
        """The complete design: defaults, then every ancestor's settings, then this layout's."""
        return designs.complete(self.manifest.get("design"))

    @property
    def custom(self) -> bool:
        return self.owner is not None

    @property
    def designable(self) -> bool:
        """Whether the layout's Typst template reads the design settings."""
        src = self.files.get(self.targets.get("pdf-typst", {}).get("template", ""))
        try:
            return src is not None and "$design()$" in src.read_text(errors="replace")
        except OSError:
            return False

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "version": self.manifest.get("version", ""),
                "description": self.description, "targets": sorted(self.targets), "defaultPdf": self.default_pdf,
                "variables": self.variables, "hash": self.content_sha256[:12], "custom": self.custom,
                "parent": self.parent}

    def option_defaults(self) -> dict:
        return {k: v.get("default") for k, v in self.variables.items() if "default" in v}

    def validate_options(self, options) -> dict:
        """Check options against the variable schema; returns the cleaned dict."""
        if options is None:
            return {}
        if not isinstance(options, dict):
            raise ValueError("Layout options must be an object.")
        clean = {}
        for key, value in options.items():
            spec = self.variables.get(key)
            if spec is None:
                raise ValueError(f"The layout has no option '{key}'.")
            kind = spec.get("type")
            if kind == "enum":
                if value not in spec.get("values", []):
                    raise ValueError(f"'{key}' must be one of {', '.join(map(str, spec.get('values', [])))}.")
            elif kind == "number":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"'{key}' must be a number.")
                if "min" in spec and value < spec["min"] or "max" in spec and value > spec["max"]:
                    raise ValueError(f"'{key}' is out of range.")
            elif kind == "boolean":
                if not isinstance(value, bool):
                    raise ValueError(f"'{key}' must be true or false.")
            elif kind == "string":
                if not isinstance(value, str) or len(value) > 500:
                    raise ValueError(f"'{key}' must be text up to 500 characters.")
            else:
                raise ValueError(f"'{key}' has an unknown type.")
            clean[key] = value
        return clean

    def safe_defaults(self) -> dict:
        """The layout's defaults.json reduced to the allowlist."""
        src = self.files.get("defaults.json")
        if src is None:
            return {}
        try:
            data = json.loads(src.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if k in DEFAULTS_ALLOWLIST}

    def filter_files(self) -> list[str]:
        return [rel for rel in self.files if rel.startswith("filters/") and rel.endswith(".lua")]

    def filters_hash(self) -> str:
        h = hashlib.sha256()
        for rel in sorted(self.filter_files()):
            h.update(rel.encode() + b"\0" + self.files[rel].read_bytes() + b"\0")
        return h.hexdigest()


def _collect(folder: Path) -> dict[str, Path]:
    out = {}
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            full = Path(dirpath) / name
            if full.is_symlink() or not full.is_file():
                continue
            out[full.relative_to(folder).as_posix()] = full
    return out


def _merge(parent: dict, child: dict) -> dict:
    out = dict(parent)
    for key, value in child.items():
        if key == "design" and isinstance(value, dict):
            out[key] = {**parent.get(key, {}), **value}
        elif key in ("targets", "variables") and isinstance(value, dict):
            merged = dict(parent.get(key, {}))
            for k, v in value.items():
                merged[k] = {**merged.get(k, {}), **v} if isinstance(v, dict) else v
            out[key] = merged
        else:
            out[key] = value
    out.pop("extends", None)
    return out


def _design_options(manifest: dict) -> None:
    """Classic options that override a design setting take their default from the design."""
    variables = manifest.get("variables")
    if not isinstance(variables, dict):
        return
    variables = manifest["variables"] = dict(variables)
    full = designs.complete(manifest.get("design"))
    for name, spec in variables.items():
        if name not in designs.OPTION_KEYS or not isinstance(spec, dict):
            continue
        value = designs.option_value(name, full)
        spec = dict(spec)
        spec["default"] = value
        if spec.get("type") == "enum" and value not in spec.get("values", []):
            spec["values"] = [*spec.get("values", []), value]
        variables[name] = spec


# Keys a custom layout's manifest may have; files, templates and filters come from its parent.
CUSTOM_KEYS = {"id", "name", "version", "extends", "description", "design", "options"}

_STYLE_LABELS = {
    "chicago-author-date": "Chicago author-date (18th ed.)",
    "chicago-notes-bibliography": "Chicago notes and bibliography (18th ed.)",
    "chicago-shortened-notes-bibliography": "Chicago shortened notes (18th ed.)",
    "chicago-notes-bibliography-subsequent-author-title-17th-edition": "Chicago notes and bibliography (17th ed., Turabian 9th ed.)",
    "chicago-shortened-notes-bibliography-17th-edition": "Chicago shortened notes (17th ed., Turabian 9th ed.)",
    "chicago-author-date-17th-edition": "Chicago author-date (17th ed., Turabian 9th ed.)",
    "apa": "APA 7th edition",
    "modern-language-association": "MLA 9th edition",
    "harvard-cite-them-right": "Harvard (Cite Them Right)",
    "ieee": "IEEE",
    "american-physics-society": "American Physical Society",
    "oxford-guide-to-style-notes": "Oxford Guide to Style (notes)",
    "din-1505-2": "DIN 1505-2 (German)",
    "uni-wien-philosophie": "Uni Wien Philosophie (footnotes)",
    "uni-wien-philosophie-kurzbelege": "Uni Wien Philosophie (short citations)",
}


def style_labels() -> dict[str, str]:
    """Bundled citation styles with short labels, for option lists and the layout editor."""
    return {k: _STYLE_LABELS.get(k, v) for k, v in bundled_styles().items()}


def option_specs() -> dict[str, dict]:
    """Variable specs for the options a custom layout can offer authors (§9.3)."""
    styles = style_labels()
    return {
        "papersize": {"type": "enum", "values": list(designs.PAPERS), "labels": designs.PAPERS, "label": "Paper size"},
        "fontsize": {"type": "enum", "values": ["10pt", "11pt", "12pt"], "label": "Font size"},
        "linestretch": {"type": "number", "min": 1, "max": 2.5, "step": 0.25, "label": "Line spacing"},
        "section-numbering": {"type": "enum", "values": ["", "1.1", "1.a", "I.1"], "label": "Section numbering"},
        "toc": {"type": "boolean", "label": "Table of contents"},
        "columns": {"type": "enum", "values": ["1", "2"], "labels": {"1": "One column", "2": "Two columns"},
                    "label": "Columns"},
        "titlepage": {"type": "boolean", "label": "Title page"},
        "citation-style": {"type": "enum", "values": list(styles), "labels": styles, "label": "Citation style"},
    }


def _apply_options(merged: dict, options) -> None:
    """A custom layout lists the options it offers; the rest of its parent's options go."""
    if not isinstance(options, list):
        return
    current = merged.get("variables", {})
    specs = option_specs()
    out = {}
    for name in options:
        if not isinstance(name, str):
            continue
        if name in current:
            out[name] = current[name]
        elif name in specs:
            out[name] = specs[name]
    merged["variables"] = out


class Layouts:
    def __init__(self, roots: list[Path], cache_dir: Path, approved_filters: set[str] | None = None):
        self.roots = roots
        self.cache_dir = cache_dir
        self.approved_filters = approved_filters or set()
        self.layouts: dict[str, Layout] = {}
        # Layouts made in the layout editor: id -> (manifest, owner id, shared). Stored in the database.
        self.custom: dict[str, tuple[dict, int, bool]] = {}
        self.reload()

    def set_custom(self, rows: dict[str, tuple[dict, int, bool]]) -> None:
        self.custom = rows
        self.reload()

    def reload(self) -> None:
        raw: dict[str, tuple[Path | None, dict]] = {}
        for root in self.roots:
            if not root.is_dir():
                continue
            for folder in sorted(root.iterdir()):
                manifest_path = folder / "layout.json"
                if not manifest_path.is_file():
                    continue
                try:
                    manifest = json.loads(manifest_path.read_text())
                except ValueError as exc:
                    raise LayoutError(f"{manifest_path}: {exc}") from None
                lid = manifest.get("id", folder.name)
                if not _ID.match(lid):
                    raise LayoutError(f"{manifest_path}: invalid layout id")
                raw[lid] = (folder, manifest)
        builtin = set(raw)
        for lid, (manifest, _owner, _shared) in self.custom.items():
            if lid not in builtin and _ID.match(lid):
                raw[lid] = (None, {k: v for k, v in manifest.items() if k in CUSTOM_KEYS})
        resolved: dict[str, Layout] = {}

        def resolve(lid: str, seen: tuple = ()) -> Layout:
            if lid in resolved:
                return resolved[lid]
            if lid in seen or lid not in raw:
                raise LayoutError(f"layout '{lid}' is missing or extends itself")
            folder, manifest = raw[lid]
            parent_id = manifest.get("extends")
            if parent_id:
                parent = resolve(parent_id, (*seen, lid))
                merged = _merge(parent.manifest, manifest)
                files = dict(parent.files)
                dirs = ([folder] if folder else []) + parent.dirs
            else:
                merged = {k: v for k, v in manifest.items() if k != "extends"}
                files, dirs = {}, [folder] if folder else []
            if folder is not None:
                files.update(_collect(folder))
            merged["id"] = lid
            _apply_options(merged, merged.pop("options", None))
            _design_options(merged)
            layout = Layout(lid, dirs, merged, files=files, parent=parent_id or None)
            if lid in self.custom and folder is None:
                layout.owner, layout.shared = self.custom[lid][1], self.custom[lid][2]
            h = hashlib.sha256(json.dumps(merged, sort_keys=True).encode())
            for rel in sorted(files):
                h.update(rel.encode() + b"\0" + files[rel].read_bytes() + b"\0")
            layout.content_sha256 = h.hexdigest()
            resolved[lid] = layout
            return layout

        for lid in raw:
            try:
                resolve(lid)
            except LayoutError:
                if lid in builtin:
                    raise
                # A custom layout whose parent was removed: leave it out rather than fail.
        self.layouts = {k: v for k, v in resolved.items() if k in raw}

    def build(self, manifest: dict, owner: int | None = None) -> Layout:
        """Resolve a draft custom layout without registering it (layout editor previews)."""
        manifest = {k: v for k, v in manifest.items() if k in CUSTOM_KEYS}
        parent = self.get(manifest.get("extends") or "default")
        merged = _merge(parent.manifest, manifest)
        merged["id"] = manifest.get("id") or "draft"
        _apply_options(merged, merged.pop("options", None))
        _design_options(merged)
        layout = Layout(merged["id"], list(parent.dirs), merged, files=dict(parent.files), owner=owner,
                        parent=parent.id)
        h = hashlib.sha256(json.dumps(merged, sort_keys=True).encode())
        for rel in sorted(layout.files):
            h.update(rel.encode() + b"\0" + layout.files[rel].read_bytes() + b"\0")
        layout.content_sha256 = h.hexdigest()
        return layout

    def exists(self, lid: str) -> bool:
        return lid in self.layouts

    def get(self, lid: str) -> Layout:
        try:
            return self.layouts[lid]
        except KeyError:
            raise LayoutError(f"unknown layout '{lid}'") from None

    def list(self) -> list[Layout]:
        return sorted(self.layouts.values(), key=lambda lay: lay.name.lower())

    def visible(self, user_id: int | None) -> list[Layout]:
        """Layouts someone can choose: built-in and shared ones, and their own."""
        return [lay for lay in self.list() if lay.owner is None or lay.shared or lay.owner == user_id]

    def filters_approved(self, layout: Layout) -> bool:
        return not layout.filter_files() or layout.filters_hash() in self.approved_filters

    def cached_dir(self, layout: Layout, folder: str = "layouts") -> Path:
        """Copy the resolved layout into the shared cache once per content hash."""
        target = self.cache_dir / folder / f"{layout.content_sha256[:48]}-{builtin_filters_hash()[:15]}"
        if target.is_dir():
            return target
        tmp = target.with_name(target.name + ".tmp-" + os.urandom(4).hex())
        shutil.rmtree(tmp, ignore_errors=True)
        for rel, src in layout.files.items():
            dest = tmp / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
        (tmp / "layout.json").write_text(json.dumps(layout.manifest, indent=2))
        # Files generated from the design settings, next to the templates that read them.
        for rel, text in designs.generated_files(layout.design, layout.variables).items():
            dest = tmp / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
        # Built-in, pre-approved filters (§9.7).
        if REPO_FILTERS.is_dir():
            (tmp / "_builtin").mkdir(parents=True, exist_ok=True)
            for f in [*REPO_FILTERS.glob("*.lua"), *REPO_FILTERS.glob("*.json")]:
                shutil.copyfile(f, tmp / "_builtin" / f.name)
        # Bundled citation styles (§10.1).
        if REPO_CSL.is_dir():
            (tmp / "_csl").mkdir(parents=True, exist_ok=True)
            for f in REPO_CSL.glob("*.csl"):
                shutil.copyfile(f, tmp / "_csl" / f.name)
        try:
            tmp.rename(target)
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
        return target

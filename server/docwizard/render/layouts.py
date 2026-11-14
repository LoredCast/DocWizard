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

REPO_LAYOUTS = Path(__file__).resolve().parents[3] / "layouts"
REPO_FILTERS = Path(__file__).resolve().parents[3] / "filters"
_BUILTIN_HASH: list[str] = []


def builtin_filters_hash() -> str:
    """Changes when DocWizard's own filters change, so cached layout copies get them."""
    if not _BUILTIN_HASH:
        h = hashlib.sha256()
        for f in sorted(REPO_FILTERS.glob("*.lua")) if REPO_FILTERS.is_dir() else []:
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


class LayoutError(ValueError):
    pass


@dataclass
class Layout:
    id: str
    dirs: list[Path]  # own folder first, then ancestors
    manifest: dict
    content_sha256: str = ""
    files: dict[str, Path] = field(default_factory=dict)  # relative path -> source file

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

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "version": self.manifest.get("version", ""),
                "description": self.description, "targets": sorted(self.targets), "defaultPdf": self.default_pdf,
                "variables": self.variables, "hash": self.content_sha256[:12]}

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
        if key in ("targets", "variables") and isinstance(value, dict):
            merged = dict(parent.get(key, {}))
            for k, v in value.items():
                merged[k] = {**merged.get(k, {}), **v} if isinstance(v, dict) else v
            out[key] = merged
        else:
            out[key] = value
    out.pop("extends", None)
    return out


class Layouts:
    def __init__(self, roots: list[Path], cache_dir: Path, approved_filters: set[str] | None = None):
        self.roots = roots
        self.cache_dir = cache_dir
        self.approved_filters = approved_filters or set()
        self.layouts: dict[str, Layout] = {}
        self.reload()

    def reload(self) -> None:
        raw: dict[str, tuple[Path, dict]] = {}
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
                dirs = [folder, *parent.dirs]
            else:
                merged = {k: v for k, v in manifest.items() if k != "extends"}
                files, dirs = {}, [folder]
            files.update(_collect(folder))
            merged["id"] = lid
            layout = Layout(lid, dirs, merged, files=files)
            h = hashlib.sha256(json.dumps(merged, sort_keys=True).encode())
            for rel in sorted(files):
                h.update(rel.encode() + b"\0" + files[rel].read_bytes() + b"\0")
            layout.content_sha256 = h.hexdigest()
            resolved[lid] = layout
            return layout

        for lid in raw:
            resolve(lid)
        self.layouts = resolved

    def exists(self, lid: str) -> bool:
        return lid in self.layouts

    def get(self, lid: str) -> Layout:
        try:
            return self.layouts[lid]
        except KeyError:
            raise LayoutError(f"unknown layout '{lid}'") from None

    def list(self) -> list[Layout]:
        return sorted(self.layouts.values(), key=lambda lay: lay.name.lower())

    def filters_approved(self, layout: Layout) -> bool:
        return not layout.filter_files() or layout.filters_hash() in self.approved_filters

    def cached_dir(self, layout: Layout) -> Path:
        """Copy the resolved layout into the shared cache once per content hash."""
        target = self.cache_dir / "layouts" / f"{layout.content_sha256[:48]}-{builtin_filters_hash()[:15]}"
        if target.is_dir():
            return target
        tmp = target.with_name(target.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        for rel, src in layout.files.items():
            dest = tmp / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
        (tmp / "layout.json").write_text(json.dumps(layout.manifest, indent=2))
        # Built-in, pre-approved filters (§9.7).
        if REPO_FILTERS.is_dir():
            (tmp / "_builtin").mkdir(parents=True, exist_ok=True)
            for f in REPO_FILTERS.glob("*.lua"):
                shutil.copyfile(f, tmp / "_builtin" / f.name)
        try:
            tmp.rename(target)
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
        return target

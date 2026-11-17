"""Layouts available to the user, and the layout editor's API (§9.3).

Built-in and admin-installed layouts are folders. Layouts made in the layout editor are
manifests in the database: they extend another layout and change its design settings
and the options authors get. Anyone with an account can make one; its owner and admins
can change or delete it. Shared layouts are listed for everyone on the instance.
"""

from __future__ import annotations

import json
import re
import secrets

from ..http.request import HTTPError, Request, bad_request, not_found
from ..http.response import Response, json_response, user_content
from ..render import design as designs
from ..render import fields
from ..render.layouts import Layout, LayoutError, option_specs, style_labels

_KEY = re.compile(r"^[0-9a-f]{64}$")
_SLUG = re.compile(r"[^a-z0-9]+")
MAX_CUSTOM_PER_USER = 200


def slug_id(name: str) -> str:
    base = _SLUG.sub("-", name.lower()).strip("-")[:40].strip("-") or "layout"
    return f"{base}-{secrets.token_hex(3)}"


def can_edit(req: Request, layout: Layout) -> bool:
    return layout.custom and req.user is not None and (layout.owner == req.user.id or req.user.is_admin)


def describe(req: Request, layout: Layout) -> dict:
    out = layout.public()
    out["editable"] = can_edit(req, layout)
    out["designable"] = layout.designable
    out["shared"] = layout.shared
    out["mine"] = req.user is not None and layout.owner == req.user.id
    return out


def register(app) -> None:
    r = app.router

    def styles() -> set[str]:
        return set(style_labels())

    def clean_manifest(body: dict, *, current: dict | None = None) -> dict:
        """Validate the editable parts of a custom layout's manifest."""
        manifest = dict(current or {})
        if "name" in body or current is None:
            name = body.get("name")
            if not isinstance(name, str) or not name.strip() or len(name) > 80:
                raise bad_request("Give the layout a name of up to 80 characters.")
            manifest["name"] = name.strip()
        if "description" in body:
            desc = body["description"]
            if not isinstance(desc, str) or len(desc) > 500:
                raise bad_request("The description can be up to 500 characters.")
            manifest["description"] = desc.strip()
        if "extends" in body or current is None:
            parent = body.get("extends") or "default"
            if not isinstance(parent, str) or not app.layouts.exists(parent):
                raise bad_request("Choose a layout to start from.")
            if current is not None and _descends_from(app, parent, current.get("id")):
                raise bad_request("A layout can't be based on itself or on a layout based on it.")
            manifest["extends"] = parent
        if "design" in body:
            try:
                manifest["design"] = designs.validate(body["design"], styles())
            except designs.DesignError as exc:
                raise bad_request(str(exc)) from None
        if "options" in body:
            options = body["options"]
            if not isinstance(options, list) or not all(isinstance(o, str) for o in options) or len(options) > 30:
                raise bad_request("Options must be a list of option names.")
            parent = app.layouts.get(manifest["extends"])
            known = set(option_specs()) | set(parent.variables)
            unknown = [o for o in options if o not in known]
            if unknown:
                raise bad_request(f"Unknown option '{unknown[0]}'.")
            manifest["options"] = list(dict.fromkeys(options))
        manifest.setdefault("version", "1.0.0")
        return manifest

    async def list_layouts(req: Request) -> Response:
        uid = req.user.id if req.user else None
        return json_response({"layouts": [describe(req, lay) for lay in app.layouts.visible(uid)],
                              "default": app.policy("default_layout")})

    async def schema(req: Request) -> Response:
        out = designs.schema(style_labels())
        out["optionSpecs"] = option_specs()
        return json_response(out)

    async def get_layout(req: Request) -> Response:
        lay = _get(app, req, req.param("id"))
        out = describe(req, lay)
        out["design"] = lay.design
        out["fields"] = fields.for_layout(lay)
        if lay.custom:
            manifest = app.layouts.custom[lay.id][0]
            out["own"] = {"design": manifest.get("design", {}), "options": manifest.get("options"),
                          "extends": manifest.get("extends", "default")}
        parent = app.layouts.get(lay.parent) if lay.parent and app.layouts.exists(lay.parent) else None
        out["parentDesign"] = parent.design if parent else designs.complete({})
        return json_response(out)

    async def create(req: Request) -> Response:
        body = await req.json_object()
        count = await app.db.fetchone("SELECT COUNT(*) AS n FROM custom_layouts WHERE owner_id = ?", (req.user.id,))
        if count["n"] >= MAX_CUSTOM_PER_USER:
            raise bad_request("You have reached the limit of layouts per account.")
        manifest = clean_manifest(body)
        # A copy starts from its parent's options, so authors keep what they had.
        if "options" not in manifest:
            manifest["options"] = list(app.layouts.get(manifest["extends"]).variables)
        lid = slug_id(manifest["name"])
        manifest["id"] = lid
        shared = bool(body.get("shared", False))
        await app.db.execute("INSERT INTO custom_layouts(id, owner_id, manifest_json, shared) VALUES (?, ?, ?, ?)",
                             (lid, req.user.id, json.dumps(manifest), int(shared)))
        await app.load_custom_layouts()
        if not app.layouts.exists(lid):
            await app.db.execute("DELETE FROM custom_layouts WHERE id = ?", (lid,))
            await app.load_custom_layouts()
            raise bad_request("This layout can't be built from its settings.")
        return json_response(describe(req, app.layouts.get(lid)), 201)

    async def update(req: Request) -> Response:
        lay = _get(app, req, req.param("id"))
        if not can_edit(req, lay):
            raise HTTPError(403, "forbidden", "Only the layout's owner can change it. Make a copy instead.")
        body = await req.json_object()
        current = dict(app.layouts.custom[lay.id][0])
        current["id"] = lay.id
        manifest = clean_manifest(body, current=current)
        shared = lay.shared if "shared" not in body else bool(body["shared"])
        old = await app.db.fetchone("SELECT manifest_json, shared FROM custom_layouts WHERE id = ?", (lay.id,))
        await app.db.execute("UPDATE custom_layouts SET manifest_json = ?, shared = ?,"
                             " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                             (json.dumps(manifest), int(shared), lay.id))
        await app.load_custom_layouts()
        if not app.layouts.exists(lay.id):
            await app.db.execute("UPDATE custom_layouts SET manifest_json = ?, shared = ? WHERE id = ?",
                                 (old["manifest_json"], old["shared"], lay.id))
            await app.load_custom_layouts()
            raise bad_request("This layout can't be built from these settings.")
        return json_response(describe(req, app.layouts.get(lay.id)))

    async def delete(req: Request) -> Response:
        lay = _get(app, req, req.param("id"))
        if not can_edit(req, lay):
            raise HTTPError(403, "forbidden", "Only the layout's owner can delete it.")
        children = [x.name for x in app.layouts.list() if x.parent == lay.id]
        if children:
            raise bad_request(f"Other layouts are based on this one ({', '.join(children[:3])}). Delete or change "
                              "them first.")
        await app.db.execute("DELETE FROM custom_layouts WHERE id = ?", (lay.id,))
        await app.load_custom_layouts()
        return json_response({"deleted": lay.id})

    async def preview(req: Request) -> Response:
        """Render the sample document with unsaved settings."""
        body = await req.json_object()
        target = "pdf-typst"
        draft = {"name": "Preview", "extends": body.get("extends") or "default"}
        if not app.layouts.exists(draft["extends"]):
            raise bad_request("Choose a layout to start from.")
        try:
            draft["design"] = designs.validate(body.get("design") or {}, styles())
        except designs.DesignError as exc:
            raise bad_request(str(exc)) from None
        if isinstance(body.get("options"), list):
            draft["options"] = [o for o in body["options"] if isinstance(o, str)]
        try:
            layout = app.layouts.build(draft, owner=req.user.id)
        except LayoutError as exc:
            raise bad_request(str(exc)) from None
        result = await app.renderer.render_sample(layout, target)
        url = None
        if result.status == "ok":
            url = f"/api/layouts/previews/{result.key}/{result.output}"
        return json_response({"status": result.status, "url": url, "diagnostics": result.diagnostics})

    async def preview_file(req: Request) -> Response:
        key, name = req.param("key"), req.param("file")
        if not _KEY.match(key):
            raise not_found()
        base = app.renderer.renders_dir / key
        try:
            meta = json.loads((base / "meta.json").read_text())
        except (OSError, ValueError):
            raise not_found("This preview is no longer available.") from None
        if meta.get("project_id") != app.renderer.SAMPLE_PROJECT or name != meta.get("output") \
                or not name.endswith(".pdf"):
            raise not_found()
        return user_content(base / name, name)

    r.add("GET", "/api/layouts", list_layouts, guest=True)
    r.add("GET", "/api/layouts/schema", schema)
    r.add("POST", "/api/layouts/preview", preview)
    r.add("GET", "/api/layouts/previews/{key}/{file}", preview_file)
    r.add("POST", "/api/layouts", create)
    r.add("GET", "/api/layouts/{id}", get_layout)
    r.add("PATCH", "/api/layouts/{id}", update)
    r.add("DELETE", "/api/layouts/{id}", delete)


def _get(app, req: Request, lid: str) -> Layout:
    if not app.layouts.exists(lid):
        raise not_found("No such layout.")
    lay = app.layouts.get(lid)
    if lay.custom and not lay.shared and (req.user is None or (lay.owner != req.user.id and not req.user.is_admin)):
        raise not_found("No such layout.")
    return lay


def _descends_from(app, parent: str, lid: str | None) -> bool:
    """Whether *parent* is *lid* or one of its descendants."""
    seen = set()
    while parent and parent not in seen:
        if parent == lid:
            return True
        seen.add(parent)
        lay = app.layouts.layouts.get(parent)
        parent = lay.parent if lay else None
    return False

"""Service worker and offline shells (§6.5).

The service worker is generated per release: the app injects the list of static
assets, so every release precaches exactly its own files and drops the old ones.
The shells are pages without any server data, which the service worker serves
when the network is gone; the editor shell opens documents from IndexedDB.
"""

from __future__ import annotations

import json

from ..http.request import Request
from ..http.response import Response, html

SHELLS = ("/_shell/home", "/_shell/editor")


def static_assets(app) -> list[str]:
    from ..asgi import WEB_DIR

    root = WEB_DIR.resolve()
    out = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if not path.is_file() or rel.as_posix() == "sw.js" or any(p.startswith(".") for p in rel.parts):
            continue
        if path.suffix in ("", ".md") or path.name in ("MANIFEST", "LICENSE"):
            continue
        # Harper's 20 MB of WebAssembly is cached when the grammar checker first loads it, not
        # with every release (§6.8).
        if rel.parts[:2] == ("vendor", "harper"):
            continue
        out.append(f"{app.static_prefix}/{rel.as_posix()}")
    return out


def register(app) -> None:
    from ..asgi import WEB_DIR

    cache: dict[str, bytes] = {}

    def build() -> bytes:
        source = (WEB_DIR / "sw.js").read_text(encoding="utf-8")
        manifest = {"release": app.cfg.release, "version": app.static_prefix.rsplit("/", 1)[-1],
                    "static": app.static_prefix, "assets": static_assets(app),
                    "shells": list(SHELLS)}
        return source.replace("self.__MANIFEST__", json.dumps(manifest, separators=(",", ":"))).encode()

    async def service_worker(req: Request) -> Response:
        if "body" not in cache:
            cache["body"] = build()
        resp = Response(cache["body"], 200, "text/javascript; charset=utf-8")
        resp.set_header("cache-control", "no-cache")
        return resp

    def shell(name: str, **ctx):
        async def handler(req: Request) -> Response:
            resp = html(app.render(req, name, user=None, guest=None, csrf="", **ctx))
            resp.set_header("cache-control", "no-cache")
            return resp
        return handler

    # The editor shell is the editor page with placeholders; the script fills them in
    # from the IndexedDB copy of the document.
    editor_ctx = {"project": {"id": "", "name": ""}, "doc": {"path": "", "kind": "markdown"}, "role": "viewer",
                  "layout": {"name": ""}, "snapshot": "null", "can": lambda *a: False}

    app.router.add("GET", "/sw.js", service_worker, auth=False, no_session=True)
    app.router.add("GET", "/_shell/home", shell("shell_home.html"), auth=False, no_session=True)
    app.router.add("GET", "/_shell/editor", shell("editor.html", **editor_ctx), auth=False, no_session=True)

"""Published sites served by the app (§14.4): sites behind a password, and the reader's
typo form, which turns a report into a suggestion in the document.

Both live on the publish host. Sites with a password are kept outside the folder the proxy
serves and answer at ``/_site/<slug>/``: without the site's cookie, a form asks for the
password. The cookie is bound to the site and its password, so changing the password locks
everyone out again. The typo form is at ``/_report/<slug>/`` (``/_site/<slug>/_report`` for
sites with a password) and needs no account; reports are rate limited.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html as htmllib
import time
from pathlib import Path

from .. import activity
from ..auth import passwords
from ..auth.throttle import TokenBucket
from ..collab.authority import PushError
from ..collab.changeset import to_u16_offset
from ..config import origin_of
from ..http.request import HTTPError, Request
from ..http.response import Response, guess_type, html, redirect
from ..render import synctex
from ..render.publish import SITE_CSP, SLUG_RE

COOKIE = "dw_site"
COOKIE_DAYS = 30
MAX_FIELD = 500
FORM_CSP = ("default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none';"
            " frame-ancestors 'none'")

STYLE = """
body { font: 16px/1.5 system-ui, sans-serif; max-width: 32rem; margin: 3rem auto; padding: 0 1rem; color: #141414;
  background: #fff; }
@media (prefers-color-scheme: dark) { body { color: #e6e6e6; background: #000; } input, textarea { color: inherit; } }
h1 { font-size: 1.4rem; font-weight: 600; }
label { display: block; margin: 1rem 0 .25rem; }
input, textarea { width: 100%; box-sizing: border-box; font: inherit; padding: .4rem .5rem; border: 1px solid #8a8a8a;
  border-radius: 2px; background: transparent; }
button { margin-top: 1rem; font: inherit; padding: .4rem 1rem; border: 0; border-radius: 2px; background: #0b57d0;
  color: #fff; cursor: pointer; }
.muted { color: #616161; }
.error { color: #c5221f; }
.trap { position: absolute; left: -9999px; }
"""


def sign(slug: str, password_hash: str, expires: int) -> str:
    mac = hmac.new(password_hash.encode(), f"{slug}|{expires}".encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{mac}"


def valid(cookie: str | None, slug: str, password_hash: str, now: float | None = None) -> bool:
    if not cookie or "." not in cookie:
        return False
    expires, _, _mac = cookie.partition(".")
    if not expires.isdigit() or int(expires) < (time.time() if now is None else now):
        return False
    return hmac.compare_digest(cookie, sign(slug, password_hash, int(expires)))


def page(title: str, body: str, lang: str, status: int = 200) -> Response:
    resp = html(f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                '<meta name="robots" content="noindex, nofollow">'
                f"<title>{htmllib.escape(title)}</title><style>{STYLE}</style></head><body>{body}</body></html>",
                status)
    resp.set_header("content-security-policy", FORM_CSP)
    resp.set_header("referrer-policy", "no-referrer")
    resp.set_header("cache-control", "no-store")
    resp.set_header("x-robots-tag", "noindex, nofollow")
    return resp


def change_for(source: str, at: int, words: list, passage: str, correction: str) -> tuple[int, int, str] | None:
    """The smallest change in *source* that turns the passage (its *words*, found at *at* in
    the source's word list) into *correction*: (from, to, insert) as Python offsets."""
    src_words = synctex.words_of(source)[at:at + len(words)]
    fix = synctex.words_of(correction)
    norm_p, norm_c = [w for w, _ in words], [w for w, _ in fix]
    p = 0
    while p < min(len(norm_p), len(norm_c)) and norm_p[p] == norm_c[p]:
        p += 1
    s = 0
    while s < min(len(norm_p), len(norm_c)) - p and norm_p[-1 - s] == norm_c[-1 - s]:
        s += 1
    if p == len(norm_p) == len(norm_c):
        # Same words, other punctuation or spacing: the whole passage.
        first, last = src_words[0], src_words[-1]
        return first[1], _word_end(source, last[1]), correction
    src_from = src_words[p][1] if p < len(src_words) - s else _word_end(source, src_words[p - 1][1])
    src_to = _word_end(source, src_words[len(src_words) - s - 1][1]) if len(src_words) - s > p else src_from
    if p < len(fix) - s:
        fix_from = fix[p][1] if src_to > src_from else (_word_end(correction, fix[p - 1][1]) if p else 0)
        fix_to = _word_end(correction, fix[len(fix) - s - 1][1])
    else:
        fix_from = fix_to = 0
        if p:  # a deletion: take the space before it too
            src_from = _word_end(source, src_words[p - 1][1])
    return src_from, src_to, correction[fix_from:fix_to]


def _word_end(text: str, start: int) -> int:
    m = synctex._WORD.match(text, start)
    return m.end() if m else start


def find_passage(texts: list[tuple[str, str]], passage: str) -> list[tuple[str, int, list]]:
    """Where the passage's words occur in a row: [(path, word index, passage words)]."""
    words = synctex.words_of(passage)
    if len(words) < 2:
        return []
    want = [w for w, _ in words]
    hits = []
    for path, text in texts:
        have = [w for w, _ in synctex.words_of(text)]
        first = want[0]
        for i, w in enumerate(have):
            if w == first and have[i:i + len(want)] == want:
                hits.append((path, i, words))
    return hits


def register(app) -> None:
    r = app.router
    unlock_bucket = TokenBucket(10, 0.1)    # per address and site: ten tries, then one every ten seconds
    report_bucket = TokenBucket(5, 1 / 120)  # per address: five reports, then one every two minutes
    site_bucket = TokenBucket(30, 1 / 60)    # per site: thirty, then one a minute

    def lang_of(req: Request) -> str:
        return app.lang_of(req)

    def _(req: Request, message: str, **values) -> str:
        return app.t(req, message, **values)

    publish_origin = origin_of(app.cfg.server.publish_url)

    def on_publish_host(req: Request) -> None:
        """These pages answer only on the publish host (or anywhere, when it is the app's own)."""
        if publish_origin != app.cfg.app_origin and req.origin != publish_origin:
            raise HTTPError(404, "not_found")

    async def publication(slug: str, req: Request):
        on_publish_host(req)
        if not SLUG_RE.match(slug):
            raise HTTPError(404, "not_found")
        pub = await app.db.fetchone(
            "SELECT p.*, d.path AS doc_path FROM publications p JOIN documents d ON d.id = p.doc_id"
            " AND d.deleted_at IS NULL WHERE p.slug = ?", (slug,))
        if pub is None:
            raise HTTPError(404, "not_found")
        return pub

    def unlocked(req: Request, pub) -> bool:
        return valid(req.cookies.get(COOKIE), pub["slug"], pub["password_hash"])

    def unlock_form(req: Request, pub, error: str = "", status: int = 200) -> Response:
        action = f"/_site/{pub['slug']}/_unlock"
        body = (f"<h1>{htmllib.escape(_(req, 'This site is protected'))}</h1>"
                f"<p class=\"muted\">{htmllib.escape(_(req, 'Enter the password you were given to read it.'))}</p>"
                + (f'<p class="error" role="alert">{htmllib.escape(error)}</p>' if error else "")
                + f'<form method="post" action="{action}"><label for="pw">{htmllib.escape(_(req, "Password"))}</label>'
                '<input id="pw" name="password" type="password" autocomplete="current-password" required autofocus>'
                f'<button type="submit">{htmllib.escape(_(req, "Open"))}</button></form>')
        return page(_(req, "This site is protected"), body, lang_of(req), status)

    async def locked_site(req: Request) -> Response:
        slug = req.param("slug")
        rel = req.path_params.get("path") or ("_report" if req.path.endswith("/_report") else "")
        pub = await publication(slug, req)
        if not pub["password_hash"]:
            # The site is open again: its address moved back.
            return redirect(app.publisher.url_for(slug), 308)
        if rel == "_report":
            return await report(req, pub)
        if not unlocked(req, pub):
            return unlock_form(req, pub, status=401)
        root = (app.publisher.locked_root / slug).resolve()
        parts = Path(rel).parts
        if any(p.startswith(".") for p in parts):
            raise HTTPError(404, "not_found")
        target = (root / rel).resolve() if rel else root
        if target != root and root not in target.parents:
            raise HTTPError(404, "not_found")
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            raise HTTPError(404, "not_found")
        ctype = guess_type(target.name)
        if ctype.startswith("text/") or ctype == "image/svg+xml":
            ctype += "; charset=utf-8"
        resp = Response(b"", 200, ctype)
        resp.file_path = target
        resp.set_header("content-security-policy", SITE_CSP)
        resp.set_header("referrer-policy", "no-referrer")
        resp.set_header("cache-control", "private, no-cache")
        resp.set_header("x-robots-tag", "noindex, nofollow")
        return resp

    async def site_root(req: Request) -> Response:
        on_publish_host(req)
        return redirect(f"/_site/{req.param('slug')}/", 308)

    async def unlock(req: Request) -> Response:
        pub = await publication(req.param("slug"), req)
        if not pub["password_hash"]:
            raise HTTPError(404, "not_found")
        form = await req.form()
        if not unlock_bucket.allow(f"{req.client_ip}|{pub['slug']}"):
            return unlock_form(req, pub, _(req, "Too many tries. Wait a minute, then try again."), 429)
        ok = await asyncio.to_thread(passwords.verify_password, form.get("password", ""), pub["password_hash"])
        if not ok:
            return unlock_form(req, pub, _(req, "That password is wrong."), 403)
        expires = int(time.time()) + COOKIE_DAYS * 86400
        resp = redirect(f"/_site/{pub['slug']}/")
        resp.set_cookie(COOKIE, sign(pub["slug"], pub["password_hash"], expires), max_age=COOKIE_DAYS * 86400,
                        secure=app.cfg.secure_cookies, path=f"/_site/{pub['slug']}/")
        return resp

    # -- typo reports --------------------------------------------------------
    def report_form(req: Request, pub, values: dict | None = None, error: str = "", status: int = 200) -> Response:
        v = values or {}
        esc = htmllib.escape
        locked = bool(pub["password_hash"])
        action = f"/_site/{pub['slug']}/_report" if locked else f"/_report/{pub['slug']}/"
        back = app.publisher.url_for(pub["slug"], locked) + (v.get("page") or "")
        field = lambda name, label, rows=0, hint="": (  # noqa: E731
            f'<label for="{name}">{esc(label)}</label>' + (f'<p class="muted">{esc(hint)}</p>' if hint else "")
            + (f'<textarea id="{name}" name="{name}" rows="{rows}" maxlength="{MAX_FIELD}">{esc(v.get(name, ""))}</textarea>'
               if rows else f'<input id="{name}" name="{name}" maxlength="{MAX_FIELD}" value="{esc(v.get(name, ""))}">'))
        body = (f"<h1>{esc(_(req, 'Report a typo'))}</h1>"
                f"<p class=\"muted\">{esc(_(req, 'Copy the words with the mistake from the page, then write them as they should be. The authors see your report as a suggested change.'))}</p>"
                + (f'<p class="error" role="alert">{esc(error)}</p>' if error else "")
                + f'<form method="post" action="{action}"><input type="hidden" name="page" value="{esc(v.get("page", ""))}">'
                + field("passage", _(req, "The passage, as it is on the page"), 3)
                + field("correction", _(req, "How it should read"), 3)
                + field("note", _(req, "A note for the authors (optional)"), 2)
                + field("name", _(req, "Your name (optional)"))
                + '<div class="trap" aria-hidden="true"><label for="website">Website</label>'
                  '<input id="website" name="website" tabindex="-1" autocomplete="off"></div>'
                + f'<button type="submit">{esc(_(req, "Send"))}</button></form>'
                + f'<p><a href="{esc(back)}">{esc(_(req, "Back to the text"))}</a></p>')
        return page(_(req, "Report a typo"), body, lang_of(req), status)

    async def report(req: Request, pub) -> Response:
        if not pub["reports"]:
            raise HTTPError(404, "not_found")
        if pub["password_hash"] and not unlocked(req, pub):
            return unlock_form(req, pub, status=401)
        if req.method == "GET":
            return report_form(req, pub, {"page": req.query.get("page", "")[:200]})
        form = await req.form()
        values = {k: (form.get(k) or "").strip()[:MAX_FIELD] for k in ("passage", "correction", "note", "name", "page")}
        if form.get("website"):
            return page(_(req, "Thank you"), f"<h1>{htmllib.escape(_(req, 'Thank you'))}</h1>", lang_of(req))
        if not values["passage"] or not values["correction"]:
            return report_form(req, pub, values, _(req, "Write the passage and how it should read."), 400)
        if not report_bucket.allow(req.client_ip) or not site_bucket.allow(pub["slug"]):
            return report_form(req, pub, values, _(req, "Too many reports right now. Try again in a few minutes."), 429)
        try:
            await suggest(pub, values)
        except LookupError as exc:
            return report_form(req, pub, values, _(req, str(exc)), 400)
        back = app.publisher.url_for(pub["slug"], bool(pub["password_hash"])) + values["page"]
        return page(_(req, "Thank you"),
                    f"<h1>{htmllib.escape(_(req, 'Thank you'))}</h1><p>{htmllib.escape(_(req, 'The authors will see your suggestion.'))}</p>"
                    f'<p><a href="{htmllib.escape(back)}">{htmllib.escape(_(req, "Back to the text"))}</a></p>',
                    lang_of(req))

    async def suggest(pub, values: dict) -> None:
        """Find the passage in the published documents and add the correction as a suggestion."""
        project = dict(await app.db.fetchone("SELECT * FROM projects WHERE id = ?", (pub["project_id"],)))
        snap, _v = await app.renderer.snapshot(project["id"])
        inputs = app.renderer.inputs_for(project, pub["doc_path"], snap)
        docs = await app.db.fetchall(
            f"SELECT id, path FROM documents WHERE project_id = ? AND deleted_at IS NULL AND path IN"
            f" ({','.join('?' * len(inputs))})", (project["id"], *inputs)) if inputs else []
        by_path = {d["path"]: d["id"] for d in docs}
        texts = []
        for path in inputs:
            if path in by_path:
                room = await app.rooms.get(by_path[path])
                text, version = await room.call(lambda room=room: _now(room))
                texts.append((path, text, version))
        hits = find_passage([(p, t) for p, t, _ in texts], values["passage"])
        if not hits:
            raise LookupError("The passage wasn't found. Copy a few more words exactly as they are on the page.")
        if len(hits) > 1:
            raise LookupError("The passage is in the text more than once. Copy a few more words around it.")
        path, at, words = hits[0]
        text, version = next((t, v) for p, t, v in texts if p == path)
        change = change_for(text, at, words, values["passage"], values["correction"])
        if change is None:
            raise LookupError("The correction is the same as the passage.")
        frm, to, insert = change
        if text[frm:to] == insert:
            raise LookupError("The correction is the same as the passage.")
        name = values["name"] or "A reader"
        author = f"{name} (reader)" if values["name"] else name
        body = values["note"] or ""
        room = await app.rooms.get(by_path[path])
        u_from, u_to = to_u16_offset(text, frm), to_u16_offset(text, to)

        def insert_row(c, a, b, v, quote):
            cid = c.execute(
                "INSERT INTO comments(doc_id, author_name, body, quote, anchor_from, anchor_to, anchor_version,"
                " suggestion) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (by_path[path], author[:80], body, quote, a, b, v, insert)).lastrowid
            c.execute("UPDATE comments SET thread_id = id WHERE id = ?", (cid,))
            return cid

        try:
            await room.call(lambda: room.add_thread(version, u_from, u_to, insert_row))
        except PushError:
            raise LookupError("The text changed just now. Try again.") from None
        await activity.log(app, project["id"], "report", actor=None, actor_name=author, doc_id=by_path[path],
                           path=path, detail={"excerpt": values["correction"]})

    async def public_report(req: Request) -> Response:
        pub = await publication(req.param("slug"), req)
        if pub["password_hash"]:
            raise HTTPError(404, "not_found")
        return await report(req, pub)

    opts = {"auth": False, "no_session": True, "host_any": True, "csrf_exempt": True}
    r.add("GET", "/_site/{slug}", site_root, **opts)
    r.add("GET", "/_site/{slug}/", locked_site, **opts)
    r.add("POST", "/_site/{slug}/_unlock", unlock, **opts)
    r.add("POST", "/_site/{slug}/_report", locked_site, **opts)
    r.add("GET", "/_site/{slug}/{path:path}", locked_site, **opts)
    r.add("GET,POST", "/_report/{slug}/", public_report, **opts)


async def _now(room):
    return room.text(), room.version

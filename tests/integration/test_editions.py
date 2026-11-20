"""Living editions, password-protected sites and readers' typo reports (§14.4)."""

import re

from docwizard.pages import sites
from docwizard.render import edition
from tests.asgi_client import Client
from tests.integration.test_collab import new_project
from tests.integration.test_render import pytestmark, rapp, rclient  # noqa: F401

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Book</title></head><body>
<header id="title-block-header"><h1 class="title">A Book</h1></header>
<section id="introduction" class="level1"><h1>Introduction</h1>
<p>See <a href="#method">the method</a> and a note.<a href="#fn1" class="footnote-ref" id="fnref1" role="doc-noteref"><sup>1</sup></a>
<span class="citation" data-cites="doe2020"><a href="#ref-doe2020" role="doc-biblioref">Doe 2020</a></span></p>
</section>
<section id="method" class="level1"><h1>Method</h1>
<section id="sampling" class="level2"><h2>Sampling</h2><p>Back to <a href="#introduction">the start</a>.</p></section>
<div id="refs" class="references csl-bib-body" role="list">
<div id="ref-doe2020" class="csl-entry" role="listitem">Doe, J. 2020. A Study.</div></div>
</section>
<section id="footnotes" class="footnotes footnotes-end-of-document" role="doc-endnotes"><hr><ol>
<li id="fn1"><p>The note text.<a href="#fnref1" class="footnote-back" role="doc-backlink">↩︎</a></p></li></ol></section>
</body></html>"""


def test_edition_splits_chapters_and_links_across_pages():
    built = edition.build(PAGE, title="A Book", cite={"text": "Doe (2020). A Book. https://x/", "bibtex": "@book{x}"},
                          downloads=[("PDF", "document.pdf")], report_url="/_report/x/?page={page}")
    names = sorted(built.pages)
    assert names[0] == "01-introduction.html" and "02-method.html" in names and "index.html" in names
    intro, method, index = built.pages["01-introduction.html"], built.pages["02-method.html"], built.pages["index.html"]
    # Links that cross chapters point at the other page; the note stays with its chapter.
    assert 'href="02-method.html#method"' in intro or 'href="02-method.html"' in intro
    assert 'href="01-introduction.html#introduction"' in method or 'href="01-introduction.html"' in method
    assert "The note text." in intro and "The note text." not in method
    # Hover pops for the citation and the note: no script anywhere.
    assert "Doe, J. 2020. A Study." in intro
    assert all("<script" not in page for page in built.pages.values())
    # The front page: contents, downloads, the citation box; every page offers the typo form.
    assert "Introduction" in index and "Method" in index and "document.pdf" in index and "@book{x}" in index
    assert "/_report/x/?page=02-method.html" in method


def test_cite_text():
    out = edition.cite_text({"title": "On Things", "authors": ["Ada Lovelace", "Alan Turing"], "year": "2024"},
                            "https://pub.example.org/things/")
    assert out["text"].startswith("Ada Lovelace and Alan Turing. (2024). On Things.")
    assert "title = {On Things}" in out["bibtex"] and "2024" in out["bibtex"]


def test_site_cookie_is_bound_to_site_and_password():
    cookie = sites.sign("my-site", "hash-a", 2_000_000_000)
    assert sites.valid(cookie, "my-site", "hash-a", now=1_000)
    assert not sites.valid(cookie, "other-site", "hash-a", now=1_000)
    assert not sites.valid(cookie, "my-site", "hash-b", now=1_000)
    assert not sites.valid(cookie, "my-site", "hash-a", now=2_000_000_001)
    assert not sites.valid("garbage", "my-site", "hash-a")


def test_typo_change_is_minimal():
    source = "We *recieve* the data, then sort it.\n"
    hits = sites.find_passage([("a.md", source)], "We recieve the data")
    assert len(hits) == 1
    path, at, words = hits[0]
    frm, to, insert = sites.change_for(source, at, words, "We recieve the data", "We receive the data")
    assert (source[frm:to], insert) == ("recieve", "receive")
    # A deleted word takes its space with it.
    source = "the the cat"
    path, at, words = sites.find_passage([("a.md", source)], "the the cat")[0]
    frm, to, insert = sites.change_for(source, at, words, "the the cat", "the cat")
    assert source[:frm] + insert + source[to:] == "the cat"


async def _set_text(app, doc_id, text):
    room = await app.rooms.get(doc_id)

    async def edit():
        return await room.apply_text(text, "test", 1)

    await room.call(edit)


BOOK = """---
title: A Small Book
author: Jane Doe
---

# Introduction

We recieve the data and sort it.[^n]

[^n]: A footnote.

# Method

The method is simple.
"""


async def test_password_edition_and_typo_report(rapp, rclient):
    app = rapp
    pid = await new_project(rclient)
    assert (await rclient.get(f"/p/{pid}/paper.md")).status == 200
    doc = await app.db.fetchone("SELECT id FROM documents WHERE project_id = ? AND path = 'paper.md'", (pid,))
    await _set_text(app, doc["id"], BOOK)
    base = f"/api/documents/{doc['id']}/publication"

    short = await rclient.request("PUT", base, json={"slug": "small-book", "password": "abc"})
    assert short.status == 400
    r = await rclient.request("PUT", base, json={"slug": "small-book", "edition": True, "reports": True,
                                                 "password": "open sesame"})
    assert r.status == 200, r.text
    pub = r.json()["publication"]
    assert pub["status"] == "ok", pub
    assert pub["password"] and pub["edition"] and pub["reports"]
    assert pub["url"].endswith("/_site/small-book/")

    site = Client(app, origin=app.cfg.preview_origin)
    # Not in the folder the proxy serves, and not on the app origin.
    assert (await site.get("/small-book/")).status == 404
    assert (await rclient.get("/_site/small-book/")).status == 404
    locked = await site.get("/_site/small-book/")
    assert locked.status == 401 and 'name="password"' in locked.text and "A Small Book" not in locked.text
    assert (await site.get("/_site/small-book/01-introduction.html")).status == 401
    wrong = await site.post("/_site/small-book/_unlock", form={"password": "nope"})
    assert wrong.status == 403 and not site.cookies
    ok = await site.post("/_site/small-book/_unlock", form={"password": "open sesame"})
    assert ok.status == 303 and sites.COOKIE in site.cookies
    index = await site.get("/_site/small-book/")
    assert index.status == 200 and "A Small Book" in index.text and "Jane Doe" in index.text
    assert "script" not in index.header("content-security-policy")
    assert "noindex" in index.header("x-robots-tag")
    chapter = await site.get("/_site/small-book/01-introduction.html")
    assert chapter.status == 200 and "recieve" in chapter.text and "A footnote." in chapter.text
    assert "/_site/small-book/_report?page=01-introduction.html" in chapter.text
    assert (await site.get("/_site/small-book/../small-book/index.html")).status == 404

    # A reader reports a typo: it arrives as a suggestion in the document.
    form = await site.get("/_site/small-book/_report?page=01-introduction.html")
    assert form.status == 200 and 'name="passage"' in form.text
    missing = await site.post("/_site/small-book/_report",
                              form={"passage": "words that are not there", "correction": "x y"})
    assert missing.status == 400
    sent = await site.post("/_site/small-book/_report",
                           form={"passage": "We recieve the data", "correction": "We receive the data",
                                 "name": "Rita", "note": "Spelling", "page": "01-introduction.html"})
    assert sent.status == 200, sent.text
    row = await app.db.fetchone("SELECT * FROM comments WHERE doc_id = ?", (doc["id"],))
    assert row["suggestion"] == "receive" and row["quote"] == "recieve" and row["author_name"] == "Rita (reader)"
    assert row["body"] == "Spelling" and row["author_user_id"] is None and row["author_guest_id"] is None
    event = await app.db.fetchone("SELECT * FROM activity WHERE project_id = ? AND kind = 'report'", (pid,))
    assert event is not None

    # A new password locks readers out again; removing it moves the site back to the open folder.
    r = await rclient.request("PUT", base, json={"slug": "small-book", "edition": True, "password": "another one"})
    assert (await site.get("/_site/small-book/")).status == 401
    r = await rclient.request("PUT", base, json={"slug": "small-book", "edition": True, "password": False})
    pub = r.json()["publication"]
    assert not pub["password"] and pub["url"].endswith("/small-book/") and "/_site/" not in pub["url"]
    page = await site.get("/small-book/")
    assert page.status == 200 and "A Small Book" in page.text
    assert (await site.get("/_site/small-book/")).status == 308
    # Reports were turned off with this save.
    assert (await site.get("/_report/small-book/")).status == 404
    assert re.search(r'href="01-introduction\.html"', page.text)


async def test_open_site_typo_form(rapp, rclient):
    app = rapp
    pid = await new_project(rclient)
    assert (await rclient.get(f"/p/{pid}/paper.md")).status == 200
    doc = await app.db.fetchone("SELECT id FROM documents WHERE project_id = ? AND path = 'paper.md'", (pid,))
    await _set_text(app, doc["id"], BOOK)
    r = await rclient.request("PUT", f"/api/documents/{doc['id']}/publication", json={"slug": "open-book", "reports": True})
    assert r.json()["publication"]["status"] == "ok", r.text
    site = Client(app, origin=app.cfg.preview_origin)
    page = await site.get("/open-book/")
    assert "/_report/open-book/?page=" in page.text
    form = await site.get("/_report/open-book/?page=")
    assert form.status == 200 and "form-action 'self'" in form.header("content-security-policy")
    # The honeypot swallows bots without a suggestion.
    trap = await site.post("/_report/open-book/", form={"passage": "We recieve", "correction": "We receive",
                                                         "website": "http://spam"})
    assert trap.status == 200
    assert await app.db.fetchone("SELECT 1 FROM comments WHERE doc_id = ?", (doc["id"],)) is None
    sent = await site.post("/_report/open-book/", form={"passage": "We recieve the", "correction": "We receive the"})
    assert sent.status == 200, sent.text
    row = await app.db.fetchone("SELECT * FROM comments WHERE doc_id = ?", (doc["id"],))
    assert row["author_name"] == "A reader" and row["suggestion"] == "receive"

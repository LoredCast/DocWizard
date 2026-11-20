"""Finding text in the PDF preview, following the cursor paragraph by paragraph, and zoom steps
(web/app/preview/pdf.js)."""

import shutil
import subprocess
from pathlib import Path

import pytest

PDF = Path(__file__).resolve().parents[2] / "web" / "app" / "preview" / "pdf.js"

NODE_SCRIPT = r"""
import assert from "node:assert/strict";
const pdf = await import(process.argv[1]);
// Text items as PDF.js gives them: the string, its transform (size, x, y), width and line ends.
const item = (str, x, y, hasEOL = false, size = 10) => ({ str, transform: [size, 0, 0, size, x, y], width: str.length * 5, hasEOL });
const view = [0, 0, 612, 792];
const page1 = pdf.pageModel({ items: [
  item("1 Introduction", 72, 720, true, 16),
  item("Cities are made of networks that decide who reaches infra-", 72, 700, true),
  item("structure, and how quickly (Doe and Roe 2020, 42). Über", 72, 688, true),
  item("alles: the networks decide.", 72, 676, true),
] }, view);
const page2 = pdf.pageModel({ items: [
  item("2 Method", 72, 720, true, 16),
  item("We counted riders on every line of the network.", 72, 700, true),
] }, view);
const pages = [page1, page2];

// The page text joins hyphenated words and lines.
assert.ok(page1.text.includes("infrastructure, and"));
assert.equal(pdf.foldChars("Über ﬁx").length, "Über ﬁx".length);
assert.equal(pdf.foldChars("Über"), "uber");

// Find: case, accents and spaces don't matter; matches across a line end are found.
let hits = pdf.findText(pages, "NETWORKS");
assert.deepEqual(hits.map((h) => h.page), [1, 1]);
assert.deepEqual(pdf.findText(pages, "network").map((h) => h.page), [1, 1, 2]);
assert.equal(pdf.findText(pages, "uber   alles").length, 1);
assert.equal(pdf.findText(pages, "reaches infrastructure").length, 1);
assert.equal(pdf.findText(pages, "").length, 0);
assert.equal(pdf.findText(pages, "nowhere to be found").length, 0);
// A hit's rectangle sits on its line, in points from the page's top left.
const r = pdf.findText(pages, "riders")[0].rects[0];
assert.equal(r.page, 2);
assert.ok(r.y > 80 && r.y < 95, `y ${r.y}`);
assert.ok(r.x > 72 && r.w > 20 && r.w < 40, JSON.stringify(r));
// A match over two lines gives a rectangle per line.
assert.equal(pdf.findText(pages, "quickly Doe and Roe 2020 42 Über alles").length, 0); // punctuation counts
assert.equal(pdf.findText(pages, "über alles: the").at(0).rects.length, 2);

// The paragraph at the cursor in Markdown: citations, attributes and math left out.
const md = `---
title: Cities
---

# Introduction

Cities are made of networks that decide who reaches
infrastructure, and how quickly [@doe2020, p. 42]. Über alles: the networks decide.

## Method {#sec:method}

We counted riders on every line of the network.
`;
assert.equal(pdf.paragraphAt(md, 5), null); // in the front matter
let para = pdf.paragraphAt(md, md.indexOf("quickly"));
assert.equal(para.words[para.at], "quickly");
assert.ok(!para.words.includes("doe2020"));
assert.equal(para.words[0], "cities");
assert.equal(pdf.paragraphAt(md, md.indexOf("\n\n# Intro") + 1), null); // a blank line
const heading = pdf.paragraphAt(md, md.indexOf("Method"));
assert.deepEqual(heading.words, ["method"]);

// It is found in the PDF, across the line ends and the rendered citation.
const tokens = pdf.tokensOf(pages);
let found = pdf.matchPassage(tokens, para.words, para.at, 0.3);
assert.ok(found, "paragraph found");
assert.equal(tokens[found.from].w, "cities");
assert.equal(tokens[found.to].w, "decide");
const rects = pdf.rangeRects(pages, tokens, found.from, found.to);
assert.equal(rects.length, 3); // three lines on page 1
assert.ok(rects.every((q) => q.page === 1));
// The heading's word is found on the page it's on; nothing is found for words not in the PDF.
found = pdf.matchPassage(tokens, heading.words, 0, 0.9);
assert.equal(tokens[found.from].page, 1);
assert.equal(pdf.matchPassage(tokens, ["entirely", "different", "words", "here"], 0), null);

// LaTeX: commands and comments left out, their text kept.
const tex = "\\section{Method}\n\nWe \\emph{counted} riders % not printed\non every line~\\cite{doe}.\n";
para = pdf.paragraphAt(tex, tex.indexOf("riders"), { latex: true });
assert.deepEqual(para.words, ["we", "counted", "riders", "on", "every", "line"]);
assert.equal(para.words[para.at], "riders");

// Zoom steps.
assert.equal(pdf.nextZoom(1, 1), 1.1);
assert.equal(pdf.nextZoom(1, -1), 0.9);
assert.equal(pdf.nextZoom(1.03, 1), 1.1);
assert.equal(pdf.nextZoom(4, 1), 4);
assert.equal(pdf.nextZoom(0.5, -1), 0.5);
assert.equal(pdf.clampZoom(9), 4);
console.log("ok");
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_find_follow_and_zoom():
    result = subprocess.run(["node", "--input-type=module", "-e", NODE_SCRIPT, PDF.as_uri()],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

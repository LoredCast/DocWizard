"""Unit checks for web/app/editor/source-check.js: page locators, PDF page numbers,
quotations with their citations, and checking them against a PDF's text."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "web" / "app" / "editor" / "source-check.js"

NODE_SCRIPT = r"""
import assert from "node:assert/strict";
const s = await import(process.argv[1]);
const { normalizeText } = await import(new URL("./cite-tools.js", process.argv[1]).href);

// Locators name a page, or something else.
assert.equal(s.locatorPage("p. 42"), "42");
assert.equal(s.locatorPage("pp. 42–45"), "42");
assert.equal(s.locatorPage("S. 7f."), "7");
assert.equal(s.locatorPage("xii"), "xii");
assert.equal(s.locatorPage("chap. 3"), null);
assert.equal(s.locatorPage("§ 4"), null);
assert.equal(s.locatorPage(""), null);

// Printed pages to PDF pages: by offset, by the PDF's labels, or as they are.
assert.equal(s.pageIndexFor("125", null, 122, 30), 3);
assert.equal(s.pageIndexFor("500", null, 122, 30), null);
assert.equal(s.pageIndexFor("iv", ["i", "ii", "iii", "iv", "1"], null, 5), 4);
assert.equal(s.pageIndexFor("2", null, null, 5), 2);

// Quotations followed by a citation, in several quotation styles.
const text = "She wrote “the limits of my language mean the limits of my world” [@witt1922, p. 74]. "
  + "Er schrieb „eine ganz andere Sache, die lang genug ist“ (so) [vgl. @doe2020, S. 12]. "
  + "A “quotation without any citation at all”. See @fig:map [@fig:map].";
const qs = s.quotations(text);
assert.deepEqual(qs.map((q) => [q.key, q.locator]), [["witt1922", "p. 74"], ["doe2020", "S. 12"]]);
assert.equal(text.slice(qs[0].citeFrom, qs[0].citeTo), "[@witt1922, p. 74]");
assert.equal(qs[0].quote, "the limits of my language mean the limits of my world");

// Checking against the pages' text.
const pages = ["cover page", "The limits of my lan- guage mean the limits of my world. All I know", "more text here"]
  .map(normalizeText);
assert.equal(s.verifyQuote(pages, qs[0].quote, 2).status, "verified");
assert.equal(s.verifyQuote(pages, qs[0].quote, 3).status, "other-page");
assert.equal(s.verifyQuote(pages, qs[0].quote, 3, null, 72).printed, "74");
assert.equal(s.verifyQuote(pages, "nothing like this appears anywhere", 2).status, "not-found");

// Excerpts mark the quotation; quotations go into the text with their citation.
const clip = s.excerpt("Before. The limits of my language mean the limits of my world. After.", "the limits of my language");
assert.equal(clip.match.toLowerCase().startsWith("the limits of my language"), true);
assert.equal(s.quoteMarkup("the lim-\nits of x", "k", "p. 3", "en-GB"), "“the limits of x” [@k, p. 3]");
assert.equal(s.quoteMarkup("Wort", "k", "", "de"), "„Wort“ [@k]");
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_source_check():
    result = subprocess.run(["node", "--input-type=module", "-e", NODE_SCRIPT, MODULE.as_uri()],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

"""Unit checks for web/app/editor/cite-tools.js: bibliography front matter, citations at
the cursor, locators and finding quotations in a PDF's text."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "web" / "app" / "editor" / "cite-tools.js"

NODE_SCRIPT = r"""
import assert from "node:assert/strict";
const t = await import(process.argv[1]);

function apply(text, change) {
  const list = Array.isArray(change) ? change : [change];
  for (const c of [...list].sort((a, b) => b.from - a.from)) text = text.slice(0, c.from) + c.insert + text.slice(c.to);
  return text;
}

// Bibliography entries in every YAML shape, and adding one.
{
  assert.deepEqual(t.bibliographyEntries("no front matter"), []);
  assert.equal(apply("Text", t.addBibliography("Text", "zotero:thesis")), "---\nbibliography: zotero:thesis\n---\n\nText");

  const scalar = "---\ntitle: X\nbibliography: refs.bib\n---\n\nText";
  assert.deepEqual(t.bibliographyEntries(scalar), ["refs.bib"]);
  assert.equal(t.addBibliography(scalar, "./refs.bib"), null);
  assert.equal(apply(scalar, t.addBibliography(scalar, "zotero:thesis")),
               "---\ntitle: X\nbibliography:\n  - refs.bib\n  - zotero:thesis\n---\n\nText");

  const block = "---\nbibliography:\n    - a.json\n    - \"b.bib\"\nlang: de-DE\n---\nText";
  assert.deepEqual(t.bibliographyEntries(block), ["a.json", "b.bib"]);
  assert.equal(apply(block, t.addBibliography(block, "c.ris")),
               "---\nbibliography:\n    - a.json\n    - \"b.bib\"\n    - c.ris\nlang: de-DE\n---\nText");
  assert.equal(t.documentLang(block), "de-DE");

  const flow = "---\nbibliography: [a.json, 'zotero:x']\n---\n";
  assert.deepEqual(t.bibliographyEntries(flow), ["a.json", "zotero:x"]);
  assert.equal(apply(flow, t.addBibliography(flow, "b.bib")), "---\nbibliography: [a.json, 'zotero:x', b.bib]\n---\n");

  const none = "---\ntitle: Y\n---\n\nBody";
  assert.equal(apply(none, t.addBibliography(none, "refs.bib")), "---\ntitle: Y\nbibliography: refs.bib\n---\n\nBody");
}

// The citation at the cursor and its parts.
{
  const text = "As argued [see @doe2020, p. 4; -@roe2019] and [a link](x) and [^note @x].";
  const c = t.citationAt(text, text.indexOf("doe"));
  assert.equal(text.slice(c.from, c.to), "[see @doe2020, p. 4; -@roe2019]");
  assert.deepEqual(c.cites.map((x) => [x.key, x.prefix, x.suffix, x.suppress]),
                   [["doe2020", "see", ", p. 4", false], ["roe2019", "", "", true]]);
  assert.equal(t.locatorOf(c.cites[0]), "p. 4");
  assert.equal(t.citationAt(text, text.indexOf("link")), null);
  assert.equal(t.citationAt(text, text.indexOf("note")), null);
  assert.deepEqual(t.citedKeys(text + " @smith says"), ["doe2020", "roe2019", "x", "smith"]);
  assert.equal(t.citationAt("[@{odd key}]", 3).cites[0].key, "odd key");
}

// Locators: set, replace, keep the rest of the suffix, remove.
{
  let text = "X [@doe2020, p. 4, emphasis added].";
  let c = t.citationAt(text, 4).cites[0];
  text = apply(text, t.setLocator(c, "pp. 7-9"));
  assert.equal(text, "X [@doe2020, pp. 7-9, emphasis added].");
  c = t.citationAt(text, 4).cites[0];
  assert.equal(apply(text, t.setLocator(c, "")), "X [@doe2020, emphasis added].");
  assert.equal(t.formatLocator("en-US", 23), "p. 23");
  assert.equal(t.formatLocator("", 23, 25), "pp. 23-25");
  assert.equal(t.formatLocator("de-AT", 23, 25), "S. 23-25");
  assert.equal(t.formatLocator("fr", "xii"), "xii");
}

// Citing at the selection.
{
  let text = "A claim.";
  let r = t.citeChanges(text, { from: 7, to: 7 }, "doe2020", "p. 3");
  assert.equal(apply(text, r.changes), "A claim [@doe2020, p. 3].");
  text = "A claim [@doe2020].";
  r = t.citeChanges(text, { from: 12, to: 12 }, "roe2019");
  assert.equal(apply(text, r.changes), "A claim [@doe2020; @roe2019].");
  r = t.citeChanges(text, { from: 12, to: 12 }, "doe2020", "p. 9");
  assert.equal(apply(text, r.changes), "A claim [@doe2020, p. 9].");
  assert.equal(apply("(x)", t.citeChanges("(x)", { from: 1, to: 2 }, "a b").changes), "(x [@{a b}])");
}

// Quotations and searching a PDF's pages.
{
  const text = "He wrote „Die Grenzen meiner Sprache bedeuten die Grenzen meiner Welt“ [@witt, ";
  assert.equal(t.quoteBefore(text, text.length - 2), "Die Grenzen meiner Sprache bedeuten die Grenzen meiner Welt");
  assert.equal(t.quoteBefore("\"An old quotation here.\" Then more. And [@x]", 40), "");
  const pages = ["intro text", "the limits of my lan- guage mean the limits", "of my world. Fin"].map(t.normalizeText);
  const hits = t.findInPages(pages, "The limits of my language mean the limits of my world");
  assert.deepEqual(hits.map((h) => h.page), [2]);
  assert.equal(t.findInPages(pages, "nothing like it").length, 0);
  const partial = t.findInPages(pages, "the limits of my language mean something else entirely here");
  assert.equal(partial[0].page, 2);
  assert.ok(partial[0].partial);
}

// Printed page numbers.
{
  assert.equal(t.pageOffset(null, 23, "123-145"), 122);
  assert.equal(t.pageOffset(null, 24, "123-145"), 121);  // a cover page
  assert.equal(t.pageOffset(["i", "ii", "1"], 3, "123-145"), null);
  assert.equal(t.pageOffset(null, 10, ""), null);
  assert.equal(t.pageLabel(3, null, 122), "125");
  assert.equal(t.pageLabel(2, ["i", "ii"], null), "ii");
  assert.equal(t.pageLabel(5, null, null), "5");
}
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_cite_tools():
    result = subprocess.run(["node", "--input-type=module", "-e", NODE_SCRIPT, TOOLS.as_uri()],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

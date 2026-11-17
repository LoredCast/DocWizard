"""Unit checks for web/app/workers/grammar-text.js (§6.8): what the grammar checker doesn't read,
which checker a document gets, and Harper's dialect."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "web" / "app" / "workers" / "grammar-text.js"

NODE_SCRIPT = r"""
import assert from "node:assert/strict";
const g = await import(process.argv[1]);

const text = [
  "---", "title: A", "lang: en-GB", "---", "",
  "See [@doe2020, p. 4] and @smith21 for $x^2$ costing $5 and $10.",
  "<!-- a note -->", "::: {.warning}", "Text \\emph{here} and {#sec:a}.", ":::", "",
  "$$", "E = mc^2", "$$", "Mail me at jo@example.org. 😀 Teh end.",
].join("\n");
const { text: m, ranges } = g.mask(text);
assert.equal(m.length, text.length);
assert.equal(m.split("\n").length, text.split("\n").length);
const line = (n) => m.split("\n")[n];
assert.match(line(0), /^ *$/);
assert.equal(line(5).replace(/ +/g, " "), "See and for costing $5 and $10.");
assert.match(line(6), /^ *$/);
assert.match(line(7), /^ *$/);
assert.equal(line(8).replace(/ +/g, " "), "Text and .");
assert.match(line(12), /^ *$/);
assert.ok(line(14).includes("jo@example.org") && line(14).includes("😀 Teh end."));
// Offsets are kept, so a finding's offsets are the document's.
const teh = text.indexOf("Teh");
assert.equal(m.slice(teh, teh + 3), "Teh");
assert.equal(g.touchesMask(ranges, teh, teh + 3), false);
const cite = text.indexOf("[@doe2020");
assert.equal(g.touchesMask(ranges, cite - 1, cite), true); // borders the citation
assert.equal(g.touchesMask(ranges, 0, 2), true);           // front matter

assert.equal(g.documentLanguage(text, "de-DE"), "en-GB");
assert.equal(g.documentLanguage("---\ntitle: x\n---\nlang: fr\n", "de-AT"), "de-AT");
assert.equal(g.documentLanguage("No front matter", null), "");
assert.equal(g.documentLanguage('---\nlang: "pt-BR"\n---\n', ""), "pt-BR");

assert.deepEqual(["en", "en-US", "en-GB", "en-AU", "en-CA", "en-IN", "en-NZ"].map(g.dialectOf), [0, 0, 1, 2, 3, 4, 1]);

const both = { harper: true, languagetool: true, choice: "auto" };
const harperOnly = { harper: true, languagetool: false, choice: "auto" };
assert.equal(g.checkerFor(both, "en-GB"), "harper");
assert.equal(g.checkerFor(both, ""), "harper");
assert.equal(g.checkerFor(both, "de-DE"), "languagetool");
assert.equal(g.checkerFor(harperOnly, "de-DE"), "");
assert.equal(g.checkerFor({ ...both, choice: "languagetool" }, "en-US"), "languagetool");
assert.equal(g.checkerFor({ ...both, choice: "harper" }, "fr"), "");
assert.equal(g.checkerFor({ ...both, choice: "off" }, "en"), "");
assert.equal(g.checkerFor({ harper: false, languagetool: true, choice: "auto" }, "en"), "languagetool");
assert.equal(g.checkerFor(undefined, "en"), "");
console.log("ok");
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_grammar_text():
    result = subprocess.run(["node", "--input-type=module", "-e", NODE_SCRIPT, MODULE.as_uri()],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"

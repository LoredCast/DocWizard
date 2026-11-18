"""The words around a double-click in the PDF preview (web/app/preview/pdf.js)."""

import shutil
import subprocess
from pathlib import Path

import pytest

PDF = Path(__file__).resolve().parents[2] / "web" / "app" / "preview" / "pdf.js"

NODE_SCRIPT = r"""
import assert from "node:assert/strict";
const { wordsAround } = await import(process.argv[1]);

// Two lines of 10 pt text at y = 700 and y = 688; the first ends in a hyphen.
const item = (str, x, y, width, hasEOL = false) => ({ str, transform: [10, 0, 0, 10, x, y], width, hasEOL });
const content = { items: [
  item("Cities are made of networks that decide who reaches infra-", 72, 700, 300, true),
  item("structure, and how quickly.", 72, 688, 130, true),
] };
// A click on "networks" (about a third of the way along the first line).
let got = wordsAround(content, 72 + 300 * 0.36, 703);
assert.equal(got.words[got.at], "networks");
assert.deepEqual(got.words.slice(0, 3), ["Cities", "are", "made"]);
// The hyphenated word is joined.
assert.ok(got.words.includes("infrastructure"));
// A click on the second line.
got = wordsAround(content, 72 + 130 * 0.85, 690);
assert.equal(got.words[got.at], "quickly");
// Between lines, the nearer one wins; an empty page gives nothing.
got = wordsAround(content, 80, 697);
assert.equal(got.words[got.at], "Cities");
assert.deepEqual(wordsAround({ items: [] }, 0, 0), { words: [], at: 0 });
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_words_around_a_click():
    result = subprocess.run(["node", "--input-type=module", "-e", NODE_SCRIPT, PDF.as_uri()],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

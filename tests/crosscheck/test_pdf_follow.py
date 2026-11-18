"""Finding a heading in the PDF preview's text, so the PDF follows the cursor (web/app/preview/pdf.js)."""

import shutil
import subprocess
from pathlib import Path

import pytest

PDF = Path(__file__).resolve().parents[2] / "web" / "app" / "preview" / "pdf.js"

NODE_SCRIPT = r"""
import assert from "node:assert/strict";
const pdf = await import(process.argv[1]);
// A text item as PDF.js gives it: the string and its transform (size on the diagonal, x, y).
const item = (str, size, x, y) => ({ str, transform: [size, 0, 0, size, x, y], width: str.length * size / 2 });

assert.equal(pdf.plainText("Über die  Stadt: eine Studie!"), "uber die stadt eine studie");

const page = { items: [
  item("Running head", 8, 72, 800),
  item("2.1", 16, 72, 700), item("Method and", 16, 100, 700),
  item("materials", 16, 72, 680),
  item("We counted riders on every line of the network.", 11, 72, 650),
  item("More body text here, at the body size.", 11, 72, 636),
  item("12", 8, 300, 40),
] };
const toc = { items: [item("Contents", 16, 72, 760), item("2.1 Method and materials ........ 4", 11, 72, 700)] };
const body = pdf.bodySize([page, toc]);
assert.equal(body, 11);
// The heading wraps over two lines and is found as one; running heads, page numbers and the
// table of contents' entries are body size or smaller and aren't headings.
const headings = pdf.headingsOf(page, body);
assert.deepEqual(headings.map((h) => h.text), ["2 1 method and materials"]);
assert.equal(headings[0].top, 716);
assert.deepEqual(pdf.headingsOf(toc, body).map((h) => h.text), ["contents"]);
// A numbered heading is the heading without its number.
assert.ok(pdf.sameHeading("2 1 method and materials", pdf.plainText("Method and materials")));
assert.ok(pdf.sameHeading("iv results", "results"));
assert.ok(!pdf.sameHeading("methods of the past", "method"));
console.log("ok");
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_headings_in_pdf_text():
    result = subprocess.run(["node", "--input-type=module", "-e", NODE_SCRIPT, PDF.as_uri()],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.strip() == "ok"

"""Matching in the command palette (web/app/editor/palette.js)."""

import shutil
import subprocess
from pathlib import Path

import pytest

PALETTE = Path(__file__).resolve().parents[2] / "web" / "app" / "editor" / "palette.js"

NODE_SCRIPT = r"""
import assert from "node:assert/strict";
const { fold, rank, score } = await import(process.argv[1]);

assert.equal(fold("Über Straße"), "uber straße");
assert.equal(fold("Über").length, 4);
assert.equal(score("", "anything").score, 0);
assert.equal(score("xyz", "Page setup"), null);
// Substrings, word starts and subsequences all match; substrings and word starts rank higher.
assert.ok(score("setup", "Page setup").score > score("pst", "Page setup").score);
assert.deepEqual(score("ps", "Page setup").at, [0, 5]);
assert.ok(score("ueber", "Über") === null); // accents fold to the plain letter only
assert.ok(score("uber", "Über die Stadt"));
// Several words: each in order.
assert.ok(score("ch meth", "chapters/02-methods.md"));
assert.equal(score("meth ch", "chapters/02-methods.md"), null);

const items = [
  { id: "a", label: "Page setup", group: "Layout" },
  { id: "b", label: "Paragraph", group: "Format" },
  { id: "c", label: "Download or print", group: "Share and export", keywords: "pdf export word" },
  { id: "d", label: "chapters/02-methods.md", group: "Document" },
  { id: "e", label: "Methods", group: "Heading" },
];
let r = rank(items, "pa");
assert.deepEqual(r.slice(0, 2).map((x) => x.item.id).sort(), ["a", "b"]);
// The label beats the keywords; keywords still find an item.
r = rank(items, "export");
assert.equal(r[0].item.id, "c");
r = rank(items, "meth");
assert.equal(r[0].item.id, "e"); // a word start at the beginning beats one later in a path
assert.ok(r.some((x) => x.item.id === "d"));
assert.deepEqual(rank(items, "zzz"), []);
console.log("ok");
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_palette_matching():
    result = subprocess.run(["node", "--input-type=module", "-e", NODE_SCRIPT, PALETTE.as_uri()],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

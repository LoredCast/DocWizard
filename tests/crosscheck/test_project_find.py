"""Unit checks for web/app/editor/project-find.js: patterns, replacements with groups,
line numbers and context, and UTF-16 offsets that the server can apply."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "web" / "app" / "editor" / "project-find.js"

NODE_SCRIPT = r"""
import assert from "node:assert/strict";
const f = await import(process.argv[1]);

// Plain text is taken literally; case and whole words are options.
{
  const text = "Colour, colour (colours) and c.olour.";
  assert.equal(f.findAll(text, f.compile("colour")).length, 3);
  assert.equal(f.findAll(text, f.compile("colour", { caseSensitive: true })).length, 2);
  assert.equal(f.findAll(text, f.compile("colour", { wholeWord: true })).length, 2);
  assert.equal(f.findAll(text, f.compile("c.olour")).length, 1);
  assert.equal(f.findAll("Ärger ärger", f.compile("ärger", { wholeWord: true })).length, 2);
  assert.throws(() => f.compile("(", { regexp: true }), SyntaxError);
}

// Replacements: $&, $1, $<name>, $$; empty matches are skipped.
{
  const re = f.compile("(\\w+)@(?<host>\\w+)", { regexp: true });
  const [m] = f.findAll("mail ann@example now", re);
  assert.equal(f.expandReplacement("$2 at $1 ($<host>) $$ [$&]", m, true), "example at ann (example) $ [ann@example]");
  assert.equal(f.expandReplacement("$1", m, false), "$1");
  assert.equal(f.findAll("abc", f.compile("x*", { regexp: true })).length, 0);
}

// Offsets are UTF-16 units, as the server counts them.
{
  const text = "😀 colour";
  const [m] = f.findAll(text, f.compile("colour"));
  assert.equal(m.from, 3);
  assert.equal(text.slice(m.from, m.to), "colour");
}

// Matches across documents, with lines, columns and context.
{
  const docs = [
    { id: "a", path: "a.md", text: "First line\nsecond colour line\nthird colour and colour\n" },
    { id: "b", path: "b.md", text: "nothing here" },
    { id: "c", path: "c.md", text: "colour" },
  ];
  const r = f.searchDocs(docs, "colour", { replacement: "color" });
  assert.equal(r.total, 4);
  assert.deepEqual(r.groups.map((g) => g.doc.id), ["a", "c"]);
  assert.deepEqual(r.groups[0].matches.map((m) => [m.line, m.col]), [[2, 8], [3, 7], [3, 18]]);
  assert.deepEqual(r.groups[0].matches[0].context, { before: "second ", text: "colour", after: " line" });
  assert.equal(r.groups[0].matches[0].insert, "color");
  const capped = f.searchDocs(docs, "colour", {}, 2);
  assert.equal(capped.total, 2);
  assert.ok(capped.truncated);
  assert.deepEqual(f.lineAt("a\nbc\nd", 3), { line: 2, col: 2, lineFrom: 2, lineTo: 4 });
}
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_project_find():
    result = subprocess.run(["node", "--input-type=module", "-e", NODE_SCRIPT, MODULE.as_uri()],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

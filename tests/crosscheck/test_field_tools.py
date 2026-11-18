"""Unit checks for web/app/editor/field-tools.js: the front matter's keys, whether a field is
filled in, and adding one where the layout prints it (§9.4)."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "web" / "app" / "editor" / "field-tools.js"

NODE_SCRIPT = r"""
import assert from "node:assert/strict";
const t = await import(process.argv[1]);

function apply(text, change) {
  const c = change.changes;
  return text.slice(0, c.from) + c.insert + text.slice(c.to);
}

const course = { key: "course", snippet: 'course: "${}"' };
const date = { key: "date", snippet: "date: {today}${}" };
const address = { key: "address", snippet: "address: |\n  | ${}" };
const affiliation = { key: "affiliation", in: "author", topLevel: false,
                      snippet: 'author:\n  - name: "${}"\n    affiliation: ""' };
const email = { key: "email", in: "author", topLevel: true, snippet: 'email: "${}"' };

// Keys and their states.
{
  const text = [
    "---",
    'title: "A Paper"',
    'course: ""',
    "keywords: []",
    "author:",
    "  - name: Jane Doe",
    "    affiliation: Example University",
    "    email: \"\"",
    "  - name: John Roe",
    "    email: john@example.org",
    "abstract: |",
    "  It argues.",
    "# a comment",
    "instructor:  # to do",
    "---",
    "",
    "Body: not a key",
  ].join("\n");
  const keys = t.frontMatterKeys(text);
  assert.deepEqual([...keys.top.keys()], ["title", "course", "keywords", "author", "abstract", "instructor"]);
  assert.equal(t.fieldState(keys, { key: "title" }).state, "filled");
  assert.equal(t.fieldState(keys, course).state, "empty");
  assert.equal(t.fieldState(keys, { key: "keywords" }).state, "empty");
  assert.equal(t.fieldState(keys, { key: "abstract" }).state, "filled");
  assert.equal(t.fieldState(keys, { key: "instructor" }).state, "empty");
  assert.equal(t.fieldState(keys, { key: "author" }).state, "filled");
  assert.equal(t.fieldState(keys, affiliation).state, "filled");
  assert.equal(t.fieldState(keys, email).state, "filled");   // John's, though Jane's is empty
  const missing = t.fieldState(keys, { key: "supervisor" });
  assert.equal(missing.state, "missing");
  assert.equal(missing.at, null);
  // The "Go to" position is the end of the key's line.
  const at = t.fieldState(keys, course).at;
  assert.equal(text.slice(text.lastIndexOf("\n", at - 1) + 1, at), 'course: ""');

  // An author without an affiliation: the panel offers to go to author:.
  const plain = t.frontMatterKeys("---\nauthor: Jane Doe\n---\n");
  const state = t.fieldState(plain, affiliation);
  assert.equal(state.state, "missing");
  assert.ok(state.parent);
  assert.equal(t.insertField("---\nauthor: Jane Doe\n---\n", affiliation), null);

  // No front matter, or text that only looks like it further down.
  assert.equal(t.frontMatterKeys("Body\n---\ntitle: x\n---\n").fm, null);
}

// Adding a field.
{
  // Without front matter, a new block starts the document.
  let text = "# Introduction\n";
  let change = t.insertField(text, course);
  assert.equal(apply(text, change), '---\ncourse: ""\n---\n\n# Introduction\n');
  assert.equal(change.cursor, '---\ncourse: "'.length);

  // In the layout's order: after the closest field before it that the document has.
  text = '---\ntitle: X\nauthor: Jane\ndate: 2026-11-17\nlang: en-US\n---\n\nText';
  const order = ["title", "author", "course", "instructor", "date"];
  change = t.insertField(text, course, { order });
  assert.equal(apply(text, change), '---\ntitle: X\nauthor: Jane\ncourse: ""\ndate: 2026-11-17\nlang: en-US\n---\n\nText');
  assert.equal(apply(text, change).slice(change.cursor - 'course: "'.length, change.cursor), 'course: "');

  // After a block value, not inside it.
  text = "---\ntitle: X\nauthor:\n  - name: Jane\n    affiliation: Uni\nlang: de\n---\n";
  change = t.insertField(text, course, { order });
  assert.equal(apply(text, change), '---\ntitle: X\nauthor:\n  - name: Jane\n    affiliation: Uni\ncourse: ""\nlang: de\n---\n');

  // Otherwise at the end, with today's date filled in.
  text = "---\ntitle: X\n---\nText";
  change = t.insertField(text, date, { today: "2026-11-17" });
  assert.equal(apply(text, change), "---\ntitle: X\ndate: 2026-11-17\n---\nText");
  assert.equal(change.cursor, "---\ntitle: X\ndate: 2026-11-17".length);

  // Block snippets put the cursor on the indented line.
  change = t.insertField(text, address);
  assert.equal(apply(text, change), "---\ntitle: X\naddress: |\n  | \n---\nText");
  assert.equal(apply(text, change).slice(0, change.cursor).endsWith("address: |\n  | "), true);

  // An empty front matter block is filled, not duplicated.
  text = "---\n\n---\nText";
  assert.equal(apply(text, t.insertField(text, course)), '---\ncourse: ""\n---\nText');

  // Affiliation without an author yet: a structured author entry.
  text = "---\ntitle: X\n---\n";
  assert.equal(apply(text, t.insertField(text, affiliation)),
               '---\ntitle: X\nauthor:\n  - name: ""\n    affiliation: ""\n---\n');
}

// Completing and explaining keys.
{
  const text = "---\ntitle: X\ncou\n  nested: 1\n---\nBody";
  assert.deepEqual(t.keyBeingTyped(text, text.indexOf("cou") + 3), { from: text.indexOf("cou"), typed: "cou" });
  assert.equal(t.keyBeingTyped(text, text.indexOf("X") + 1), null);          // in a value
  assert.equal(t.keyBeingTyped(text, text.indexOf("nested") + 2), null);     // indented
  assert.equal(t.keyBeingTyped(text, text.length), null);                    // in the body
  assert.deepEqual(t.keyAt(text, 6), { key: "title", from: 4, to: 9, nested: false });
  assert.equal(t.keyAt(text, text.indexOf("X")), null);
  assert.equal(t.keyAt(text, text.indexOf("nested") + 1).key, "nested");
  assert.equal(t.keyAt("---\nauthor:\n  - email: a@b\n---\n", 18).key, "email");
}
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_field_tools():
    result = subprocess.run(["node", "--input-type=module", "-e", NODE_SCRIPT, TOOLS.as_uri()],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

"""Reading and setting front matter fields for the title card (web/app/editor/front-matter-edit.js)."""

import shutil
import subprocess
from pathlib import Path

import pytest

EDIT = Path(__file__).resolve().parents[2] / "web" / "app" / "editor" / "front-matter-edit.js"

NODE_SCRIPT = r"""
import assert from "node:assert/strict";
const fm = await import(process.argv[1]);
const apply = (text, c) => (c ? text.slice(0, c.from) + c.insert + text.slice(c.to) : text);

// 1. Values without YAML's quotes, lists and block markers.
assert.deepEqual(fm.readValue(' "A \\"quoted\\" title"'), { kind: "text", value: 'A "quoted" title' });
assert.deepEqual(fm.readValue(" 'It''s'"), { kind: "text", value: "It's" });
assert.deepEqual(fm.readValue(" plain # a comment"), { kind: "text", value: "plain" });
assert.deepEqual(fm.readValue(" [Jane Doe, \"Roe, John\"]"), { kind: "list", value: ["Jane Doe", "Roe, John"] });
assert.deepEqual(fm.readValue("\n  - Jane\n  - \"John\""), { kind: "list", value: ["Jane", "John"] });
assert.deepEqual(fm.readValue("\n  - name: Jane\n    affiliation: Uni"), { kind: "complex", value: "" });
assert.deepEqual(fm.readValue(" |\n  One.\n\n  Two."), { kind: "block", value: "One.\n\nTwo." });
assert.deepEqual(fm.readValue(" >\n  one\n  two"), { kind: "block", value: "one two" });
assert.deepEqual(fm.readValue(""), { kind: "text", value: "" });

const doc = '---\ntitle: "Untitled"\nauthor: "jane"\ndate: 2026-11-18\nlang: en-US\nabstract: |\n  Short.\n---\n\n# Intro\n';
const { has, fields } = fm.readFields(doc);
assert.ok(has);
assert.deepEqual(fields.map((f) => f.key), ["title", "author", "date", "lang", "abstract"]);
assert.equal(fm.readFields("# no front matter").has, false);

// 2. Setting fields: quoted, in place, the rest of the text untouched.
let text = apply(doc, fm.setField(doc, "title", 'My "great": thesis'));
assert.ok(text.startsWith('---\ntitle: "My \\"great\\": thesis"\nauthor: "jane"\n'));
assert.ok(text.endsWith("---\n\n# Intro\n"));
// A subtitle goes after the title; several authors become a list; an ISO date stays bare.
text = apply(text, fm.setField(text, "subtitle", "A study"));
text = apply(text, fm.setField(text, "author", fm.authorValue("Jane Doe; John Roe")));
text = apply(text, fm.setField(text, "date", "2027-01-02"));
assert.ok(text.includes('title: "My \\"great\\": thesis"\nsubtitle: "A study"\nauthor:\n  - "Jane Doe"\n  - "John Roe"\ndate: 2027-01-02\nlang'));
assert.equal(fm.authorsOf(fm.readFields(text).fields.find((f) => f.key === "author")), "Jane Doe; John Roe");
// Back to one author: one line again. A block value keeps its lines.
text = apply(text, fm.setField(text, "author", fm.authorValue("Jane Doe")));
assert.ok(text.includes('author: "Jane Doe"\ndate'));
text = apply(text, fm.setField(text, "abstract", "First.\n\nSecond."));
assert.ok(text.includes("abstract: |\n  First.\n\n  Second.\n---"));
// Other fields go at the end; lists stay lists; empty values remove a field, but not the title.
text = apply(text, fm.setField(text, "keywords", ["cities", "trams, buses"]));
assert.ok(text.includes('keywords: [cities, "trams, buses"]\n---'));
text = apply(text, fm.setField(text, "subtitle", ""));
assert.ok(!text.includes("subtitle"));
text = apply(text, fm.setField(text, "title", ""));
assert.ok(text.startsWith('---\ntitle: ""\nauthor'));
// Unchanged values give no change; a document without front matter gets one.
assert.equal(fm.setField(text, "author", "Jane Doe"), null);
assert.equal(apply("Text\n", fm.setField("Text\n", "title", "T")), '---\ntitle: "T"\n---\n\nText\n');
assert.equal(fm.setField("Text\n", "subtitle", ""), null);
// A title added to front matter that lacks one goes first.
assert.equal(apply("---\nlang: de\n---\n", fm.setField("---\nlang: de\n---\n", "title", "T")), '---\ntitle: "T"\nlang: de\n---\n');
console.log("ok");
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_front_matter_fields_round_trip():
    result = subprocess.run(["node", "--input-type=module", "-e", NODE_SCRIPT, EDIT.as_uri()],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.strip() == "ok"

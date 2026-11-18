"""Suggest mode (§8.10) in web/app/editor/suggest-mode.js: typing collects into a pending
suggestion and never changes the text; insertions widen to a word; the editor shows only
the words a suggestion changes."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "web" / "vendor" / "codemirror.js"
FILES = {
    "editor/suggest-mode.js": ROOT / "web" / "app" / "editor" / "suggest-mode.js",
    "editor/anchors.js": ROOT / "web" / "app" / "editor" / "anchors.js",
    "ui/worddiff.js": ROOT / "web" / "app" / "ui" / "worddiff.js",
}

NODE_SCRIPT = r"""
import assert from "node:assert/strict";

const [vendorUrl, modeUrl, diffUrl] = process.argv.slice(1);
const cm = await import(vendorUrl);
const sm = await import(modeUrl);
const { sharedEnds } = await import(diffUrl);

const mode = sm.suggestMode({ submit: async () => true });
let state = cm.EditorState.create({ doc: "The cat sat.\nNext line", extensions: [mode.extension] });
state = state.update({ effects: sm.setSuggestMode.of(true) }).state;
const at = (pos) => { state = state.update({ selection: { anchor: pos } }).state; };
const type = (text) => {
  const { from, to } = state.selection.main;
  state = state.update({ changes: { from, to, insert: text }, selection: { anchor: from + text.length },
                         userEvent: "input.type" }).state;
};
const backspace = () => {
  const head = state.selection.main.head;
  state = state.update({ changes: { from: head - 1, to: head }, userEvent: "delete.backward" }).state;
};
const pending = () => {
  const p = state.field(sm.suggestField).pending;
  return p && { from: p.from, to: p.to, text: p.text };
};

// 1. Typing never changes the text; it collects at the cursor.
at(7);
type(" black");
type("!");
assert.equal(state.doc.toString(), "The cat sat.\nNext line");
assert.deepEqual(pending(), { from: 7, to: 7, text: " black!" });
assert.equal(state.selection.main.head, 7);

// 2. Backspace takes back typed text first, then strikes the text before.
backspace();
assert.equal(pending().text, " black");
for (let i = 0; i < 6; i++) backspace();
assert.equal(pending(), null);
backspace();
backspace();
backspace();
assert.deepEqual(pending(), { from: 4, to: 7, text: "" });
type("dog");
assert.deepEqual(pending(), { from: 4, to: 7, text: "dog" });
assert.equal(state.doc.toString(), "The cat sat.\nNext line");

// 3. Typing over a selection elsewhere finishes the pending one and starts another.
state = state.update({ selection: { anchor: 8, head: 11 } }).state;
type("stood");
assert.deepEqual(pending(), { from: 8, to: 11, text: "stood" });
const sent = state.field(sm.suggestField).sent;
assert.equal(sent.length, 1);
assert.equal(sent[0].text, "dog");

// 4. Remote changes move pending suggestions along.
state = state.update({ changes: { from: 0, insert: "Yes. " }, filter: false }).state;
assert.deepEqual(pending(), { from: 13, to: 16, text: "stood" });

// 5. Off, typing changes the text again.
state = state.update({ effects: sm.setSuggestMode.of(false) }).state;
at(0);
type("A");
assert.equal(state.doc.toString(), "AYes. The cat sat.\nNext line");

// 6. Insertions widen to the word before, or after at the start of a line.
const doc = cm.Text.of(["The cat sat.", "", "x"]);
assert.deepEqual(sm.widen(doc, { from: 7, to: 7, text: " black" }), { from: 4, to: 7, text: "cat black" });
assert.deepEqual(sm.widen(doc, { from: 4, to: 4, text: "big " }), { from: 0, to: 4, text: "The big " });
assert.deepEqual(sm.widen(doc, { from: 0, to: 0, text: "So " }), { from: 0, to: 3, text: "So The" });
assert.deepEqual(sm.widen(doc, { from: 13, to: 13, text: "New" }), { from: 12, to: 13, text: "\nNew" });
assert.deepEqual(sm.widen(doc, { from: 4, to: 7, text: "" }), { from: 4, to: 7, text: "" });
assert.deepEqual(sm.widen(doc, { from: 5, to: 5, text: "h" }), { from: 4, to: 7, text: "chat" });

// 7. Backspace and Delete in the typing box strike text around the typed text.
const doc2 = cm.Text.of(["one two three"]);
assert.deepEqual(sm.strikeBefore(doc2, { from: 4, to: 4, text: "x" }), { from: 3, to: 4, text: "x" });
assert.deepEqual(sm.strikeBefore(doc2, { from: 7, to: 7, text: "" }, true), { from: 4, to: 7, text: "" });
assert.deepEqual(sm.strikeAfter(doc2, { from: 3, to: 3, text: "" }, true), { from: 3, to: 7, text: "" });

// 8. In place, only the words a suggestion changes are struck or added.
assert.deepEqual(sharedEnds("cat", "cat black"), { head: 3, tail: 0 });
assert.deepEqual(sharedEnds("colour me", "color me"), { head: 0, tail: 3 });
assert.deepEqual(sharedEnds("The ", "The big "), { head: 4, tail: 0 });
"""


@pytest.mark.skipif(shutil.which("node") is None or not VENDOR.exists(), reason="needs node and vendored codemirror")
def test_suggest_mode(tmp_path):
    vendor_uri = VENDOR.as_uri()
    (tmp_path / "package.json").write_text('{"type": "module"}')
    for name, source in FILES.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text().replace('from "codemirror"', f'from "{vendor_uri}"'))
    result = subprocess.run(
        ["node", "--input-type=module", "-e", NODE_SCRIPT, vendor_uri,
         (tmp_path / "editor" / "suggest-mode.js").as_uri(), (tmp_path / "ui" / "worddiff.js").as_uri()],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

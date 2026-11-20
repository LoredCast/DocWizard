"""Pipe table commands in web/app/editor/tables.js."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "web" / "vendor" / "codemirror.js"
TABLES = ROOT / "web" / "app" / "editor" / "tables.js"

NODE_SCRIPT = r"""
import assert from "node:assert/strict";
const [vendorUrl, tablesUrl] = process.argv.slice(1);
const cm = await import(vendorUrl);
const tb = await import(tablesUrl);

function makeView(doc, anchor, head = anchor) {
  let state = cm.EditorState.create({ doc, selection: cm.EditorSelection.range(anchor, head) });
  return {
    get state() { return state; },
    dispatch(spec) { state = state.update(spec).state; },
    text() { return state.doc.toString(); },
    selected() { return state.sliceDoc(state.selection.main.from, state.selection.main.to); },
  };
}

// 1. Splitting rows: escaped pipes and pipes in code stay in their cell.
assert.deepEqual(tb.splitRow("| a | b \\| c | `x|y` |"), ["a", "b \\| c", "`x|y`"]);
assert.deepEqual(tb.splitRow("a | b"), ["a", "b"]);

// 2. Inserting: a padded table on its own paragraph, the first header selected.
let v = makeView("Intro text\n\nMore", 3);
tb.insertTable(v, 3, 2);
assert.equal(v.text(), "Intro text\n\n| Column 1 | Column 2 |\n| -------- | -------- |\n|          |          |\n|          |          |\n\nMore");
assert.equal(v.selected(), "Column 1");
v = makeView("", 0);
tb.insertTable(v, 2, 2, (i) => `H${i}`);
assert.equal(v.text(), "| H0  | H1  |\n| --- | --- |\n|     |     |\n");

// 3. Tab goes to the next cell and adds a row at the end; Shift+Tab goes back.
const doc = "Text\n\n| Name | Age |\n|---|---|\n| Ann | 3 |\n";
v = makeView(doc, doc.indexOf("Name"));
assert.equal(tb.tableAt(v.state, v.state.selection.main.head).col, 0);
tb.nextCell(v);
assert.equal(v.selected(), "Age");
assert.equal(v.text(), "Text\n\n| Name | Age |\n| ---- | --- |\n| Ann  | 3   |\n");
tb.nextCell(v);
assert.equal(v.selected(), "Ann");
tb.nextCell(v); tb.nextCell(v);
assert.equal(v.text(), "Text\n\n| Name | Age |\n| ---- | --- |\n| Ann  | 3   |\n|      |     |\n");
tb.previousCell(v);
assert.equal(v.selected(), "3");
// Outside a table, Tab does nothing here.
assert.equal(tb.nextCell(makeView("plain", 0)), false);

// 4. Rows and columns: add below/right, delete, align.
v = makeView("| a | b |\n|---|---|\n| 1 | 2 |", 2);
tb.addColumn(v);
assert.equal(v.text(), "| a   |     | b   |\n| --- | --- | --- |\n| 1   |     | 2   |");
tb.addRow(v);
assert.equal(v.text(), "| a   |     | b   |\n| --- | --- | --- |\n|     |     |     |\n| 1   |     | 2   |");
tb.deleteColumn(v);
assert.equal(v.text(), "| a   | b   |\n| --- | --- |\n|     |     |\n| 1   | 2   |");
tb.deleteRow(v);
assert.equal(v.text(), "| a   | b   |\n| --- | --- |\n| 1   | 2   |");
v = makeView("| a | b |\n|---|---|\n| 1 | 2 |", "| a | b |\n|---|---|\n| 1 | ".length);
tb.cycleAlign(v);
assert.equal(v.text(), "| a   |  b  |\n| --- | :-: |\n| 1   |  2  |");
tb.cycleAlign(v);
assert.equal(v.text(), "| a   |   b |\n| --- | --: |\n| 1   |   2 |");
// Deleting the last row removes the table, with the blank line after it.
v = makeView("Before\n\n| a |\n|---|\n| 1 |\n\nAfter", 10);
tb.deleteRow(v);
tb.deleteRow(v);
assert.equal(v.text(), "Before\n\nAfter");

// 5. Cells as the table view types into them: where each one's text sits, pipes escaped.
assert.deepEqual(tb.cellSpans("| ab | `x|y` |     | c \\| d |"), [{ from: 2, to: 4 }, { from: 7, to: 12 }, { from: 15, to: 15 }, { from: 21, to: 27 }]);
assert.deepEqual(tb.cellSpans("| a | b"), [{ from: 2, to: 3 }, { from: 6, to: 7 }]);
assert.equal(tb.cellStored("a | b\nc"), "a \\| b c");
assert.equal(tb.cellShown("a \\| b"), "a | b");
assert.equal(tb.shownToStored("a | b", 4), 5);
assert.equal(tb.storedToShown("a \\| b", 5), 4);

// 6. Rewriting a table changes only what differs: a cell's padding, not the text around it, so
// someone typing elsewhere in the table isn't disturbed.
const apply = (doc, changes) => cm.EditorState.create({ doc }).update({ changes }).state.doc.toString();
const messy = "Text\n\n| Name | Age |\n|---|---|\n| Annabel | 3 |\n";
let state = cm.EditorState.create({ doc: messy });
let table = tb.tableAt(state, messy.indexOf("Name"));
let changes = tb.tidyChanges(state, table);
const tidy = "Text\n\n| Name    | Age |\n| ------- | --- |\n| Annabel | 3   |\n";
assert.equal(apply(messy, changes), tidy);
const annabel = messy.indexOf("Annabel");
assert.ok(changes.every((c) => c.to <= annabel || c.from >= annabel + "Annabel".length), "the cell's text is left alone");
assert.deepEqual(tb.tidyChanges(cm.EditorState.create({ doc: tidy }), tb.tableAt(cm.EditorState.create({ doc: tidy }), 8)), []);
// Adding a row inserts one line; the others keep theirs.
state = cm.EditorState.create({ doc: tidy });
table = tb.tableAt(state, 8);
changes = tb.tableChanges(state, table, tb.formatTable([...table.rows, ["Bo", "4"]], table.aligns).text);
assert.equal(changes.length, 1);
assert.equal(apply(tidy, changes), "Text\n\n| Name    | Age |\n| ------- | --- |\n| Annabel | 3   |\n| Bo      | 4   |\n");
assert.deepEqual(tb.charDiff("abc", "abXc", 10), [{ from: 12, to: 12, insert: "X" }]);
console.log("ok");
"""


@pytest.mark.skipif(shutil.which("node") is None or not VENDOR.exists(), reason="needs node and vendored codemirror")
def test_table_commands():
    result = subprocess.run(["node", "--input-type=module", "-e", NODE_SCRIPT, VENDOR.as_uri(), TABLES.as_uri()],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.strip() == "ok"

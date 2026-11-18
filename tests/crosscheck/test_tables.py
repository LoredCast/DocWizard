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
console.log("ok");
"""


@pytest.mark.skipif(shutil.which("node") is None or not VENDOR.exists(), reason="needs node and vendored codemirror")
def test_table_commands():
    result = subprocess.run(["node", "--input-type=module", "-e", NODE_SCRIPT, VENDOR.as_uri(), TABLES.as_uri()],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.strip() == "ok"

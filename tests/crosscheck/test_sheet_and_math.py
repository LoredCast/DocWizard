"""Data sheets (web/app/editor/sheet-model.js, filters/dwsheet.lua) and the math palette
(web/app/editor/math-palette.js)."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "web" / "vendor" / "codemirror.js"
EDITOR = ROOT / "web" / "app" / "editor"

SHEET_SCRIPT = r"""
import assert from "node:assert/strict";
const m = await import(process.argv[1]);
const text = 'Year,Revenue,Cost\n2020,"1,000",400\n2021,1500,=B3*0.5\nTotal,=SUM(B2:B3),"=SUM(C2:C3)"\n';
const rows = m.parseSheet(text, ",");
assert.equal(rows.length, 4);
assert.equal(rows[1][1].value, "1,000");
assert.equal(rows[1][1].src, '"1,000"');
// Written back unchanged, cell for cell.
assert.equal(m.serializeSheet(rows, ","), text);
const v = new m.SheetValues(rows);
assert.equal(v.value(2, 2), 750);
assert.equal(v.value(3, 1), 2500);
assert.equal(v.value(3, 2), 1150);
assert.equal(v.display(3, 2), "1150");
// Editing one cell changes only that cell's text.
m.setCell(rows, 2, 1, "2000", ",");
const after = m.serializeSheet(rows, ",");
const ch = m.minimalChange(text, after);
assert.equal(text.slice(0, ch.from) + ch.insert + text.slice(ch.to), after);
assert.ok(ch.to - ch.from <= 4 && ch.from >= text.indexOf("1500"));
// Cells beyond the end extend the sheet; values that need it are quoted.
m.setCell(rows, 5, 4, 'a "b", c', ",");
assert.ok(m.serializeSheet(rows, ",").endsWith('\n\n,,,,"a ""b"", c"\n'));
assert.equal(m.toNumber("1,234.5"), 1234.5);
assert.equal(m.toNumber("1.234,5"), 1234.5);
assert.equal(m.toNumber("1,5"), 1.5);
assert.equal(m.toNumber("12%"), 0.12);
assert.equal(m.toNumber("abc"), null);
assert.equal(new m.SheetValues(m.parseSheet("=B1,=A1\n", ",")).value(0, 0), "#CYCLE");
const f = new m.SheetValues(m.parseSheet('"=ROUND(2/3,2)",=FOO(1),=1/0,"=MAX(1,2;5)+2^3"\n', ","));
assert.equal(f.value(0, 0), 0.67);
assert.equal(f.value(0, 1), "#NAME?");
assert.equal(f.value(0, 2), "#DIV/0");
assert.equal(f.value(0, 3), 13);
// Pasting from spreadsheet programs, copying back.
assert.deepEqual(m.parseClipboard("a\tb\r\n1\t2\r\n"), [["a", "b"], ["1", "2"]]);
assert.equal(m.toClipboard([["a", "b\tc"]]), 'a\t"b\tc"');
assert.equal(m.separatorFor("a;b;c\n"), ";");
assert.equal(m.separatorFor("a,b", "x.tsv"), "\t");
assert.equal(m.colName(28), "AB");
assert.equal(m.colIndex("AB"), 28);
assert.deepEqual(m.parseSheet("a,b\r\nc,d\r\n", ",").map((r) => r.map((c) => c.value)), [["a", "b"], ["c", "d"]]);
"""

LUA_SCRIPT = r"""
local S = require "dwsheet"
local function eq(a, b) if a ~= b then error(("expected %s got %s"):format(tostring(b), tostring(a)), 2) end end
eq(S.number("1,234"), 1234); eq(S.number("1,234.5"), 1234.5); eq(S.number("1,5"), 1.5)
eq(S.number("1.234,5"), 1234.5); eq(S.number("12%"), 0.12); eq(S.number("abc"), nil); eq(S.number("-3e2"), -300)
local sh = S.sheet(S.parse('Year,Revenue,Cost\n2020,"1,000",400\n2021,1500,=B3*0.5\nTotal,=SUM(B2:B3),"=SUM(C2:C3)"\n'))
eq(sh.value(3, 3), 750); eq(sh.value(4, 2), 2500); eq(sh.value(4, 3), 1150); eq(sh.display(4, 3), "1150")
eq(S.sheet(S.parse("=B1,=A1\n")).value(1, 1), "#CYCLE")
local f = S.sheet(S.parse('"=ROUND(2/3,2)",=FOO(1),=1/0,"=MAX(1,2;5)+2^3"\n'))
eq(f.value(1, 1), 0.67); eq(f.value(1, 2), "#NAME?"); eq(f.value(1, 3), "#DIV/0"); eq(f.value(1, 4), 13)
eq(S.separator("a;b;c\n1;2;3"), ";"); eq(S.separator("a,b", "x.tsv"), "\t")
eq(#S.parse("a,b\r\nc,d\r\n"), 2)
"""

MATH_SCRIPT = r"""
import assert from "node:assert/strict";
const [vendorUrl, paletteUrl, pandocUrl] = process.argv.slice(1);
const cm = await import(vendorUrl);
const { pandocExtensions } = await import(pandocUrl);
const p = await import(paletteUrl);
const lang = new cm.Language(cm.defineLanguageFacet({}), cm.markdownParser.configure([cm.GFM, ...pandocExtensions]), [],
                             "markdown");
const state = (doc, from, to = from) => cm.EditorState.create({ doc, selection: cm.EditorSelection.range(from, to),
                                                                extensions: [lang] });
const apply = (s, spec) => s.update(spec).state;
const frac = p.MATH_GROUPS[0].items.find((i) => i.label === "Fraction");
const matrix = p.MATH_GROUPS[1].items.find((i) => i.label === "Matrix 2 × 2");
assert.deepEqual(p.parseSnippet("\\frac{‹a›}{b}"), { text: "\\frac{a}{b}", select: [6, 7] });
// Outside math: inline $…$, the first part selected.
let s = apply(state("Ratio  here", 6), p.mathInsertion(state("Ratio  here", 6), frac));
assert.equal(s.doc.toString(), "Ratio $\\frac{a}{b}$ here");
assert.equal(s.sliceDoc(s.selection.main.from, s.selection.main.to), "a");
// Inside math: only the LaTeX.
const inside = state("Say $x + y$ ok", 9);
s = apply(inside, p.mathInsertion(inside, frac));
assert.equal(s.doc.toString(), "Say $x + \\frac{a}{b}y$ ok");
// Equations get lines of their own.
const para = state("Before\nAfter", 6);
s = apply(para, p.mathInsertion(para, matrix));
assert.equal(s.doc.toString(), "Before\n\n$$\n\\begin{pmatrix}\na & b \\\\\nc & d\n\\end{pmatrix}\n$$\n\nAfter");
assert.equal(s.sliceDoc(s.selection.main.from, s.selection.main.to), "a");
// An inline piece as an equation, on request.
const empty = state("", 0);
s = apply(empty, p.mathInsertion(empty, frac, { display: true }));
assert.equal(s.doc.toString(), "$$\n\\frac{a}{b}\n$$");
// Every snippet has its selection inside it.
for (const g of p.MATH_GROUPS) for (const item of g.items) {
  const { text, select } = p.parseSnippet(item.tex);
  assert.ok(select[0] <= select[1] && select[1] <= text.length && !text.includes("‹"), item.label);
}
"""

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


@needs_node
def test_sheet_model():
    subprocess.run(["node", "--input-type=module", "-e", SHEET_SCRIPT, (EDITOR / "sheet-model.js").as_uri()],
                   check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="needs pandoc")
def test_lua_sheet_matches(tmp_path):
    script = tmp_path / "sheet.lua"
    script.write_text(f'package.path = "{ROOT / "filters"}/?.lua;" .. package.path\n' + LUA_SCRIPT)
    r = subprocess.run(["pandoc", "lua", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(shutil.which("node") is None or not VENDOR.exists(), reason="needs node and vendored codemirror")
def test_math_palette(tmp_path):
    vendor = VENDOR.as_uri()
    palette = tmp_path / "math-palette.mjs"
    palette.write_text((EDITOR / "math-palette.js").read_text()
                       .replace('from "codemirror"', f'from "{vendor}"')
                       .replace('from "../ui/dom.js"', f'from "{(EDITOR.parent / "ui" / "dom.js").as_uri()}"')
                       .replace('from "./math.js"', f'from "{(EDITOR / "math.js").as_uri()}"'))
    syntax = tmp_path / "pandoc-syntax.mjs"
    syntax.write_text((EDITOR / "pandoc-syntax.js").read_text().replace('from "codemirror"', f'from "{vendor}"'))
    r = subprocess.run(["node", "--input-type=module", "-e", MATH_SCRIPT, vendor, palette.as_uri(), syntax.as_uri()],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

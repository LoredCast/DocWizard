// Tables in the editor (ARCHITECTURE.md §6.3): a pipe table shows as a table, with its header,
// alignments, inline formatting and caption, and it is edited as a table. Each cell is a small
// text field: typing changes that cell's text in the source and nothing else, Tab and Enter move
// between cells (and add a row at the end), the arrow keys move out of a cell at its edges, and
// the table bar adds and deletes rows and columns. A cell shows its Markdown (`**bold**`) only
// while it is typed into, as the line with the cursor does. When a cell is left, the columns
// are padded again, touching only the padding. Pasting cells from a spreadsheet fills the table.
//
// The editor's selection follows the cell being typed into, so the table bar, undo, comments,
// presence and find work as they do everywhere else; a selection put into the table from
// outside (Tab, the table bar, undo, a search match) puts the caret in that cell. The source
// shows only when asked for (the table bar's Markdown) or when a selection spans several cells,
// until the selection leaves the table. Wide tables scroll sideways inside the text, so they
// never make the page wider than a phone.
import {
  Annotation, Decoration, EditorView, Facet, Prec, StateEffect, StateField, Transaction, ViewPlugin, WidgetType, isolateHistory,
  keymap, redo, undo,
} from "codemirror";
import { el } from "../ui/dom.js";
import { toggleBold, toggleItalic } from "./commands.js";
import {
  addRow, cellShown, cellSpans, cellStored, charDiff, formatTable, nextCell, previousCell, shownToStored, splitRow,
  storedToShown, tableAt, tableChanges, tidyChanges,
} from "./tables.js";

const ROW = /^\s*\|/;
const DELIM = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;
const FENCE = /^\s{0,3}(```+|~~~+)/;
const CAPTION = /^(?::|Table:)[ \t]+/;
const captionLine = Decoration.line({ class: "cm-table-caption" });
const hidden = Decoration.replace({});

const fromCell = Annotation.define(); // the transaction came from a cell of the table view
export const showTableSource = StateEffect.define(); // a table's start: its Markdown, until the selection leaves it
const config = Facet.define({ combine: (v) => v[0] ?? { reading: false, locked: () => false } });

// Whether cells can be "plaintext-only" fields (everything but old Firefox), so pasting and
// dropping can't bring formatted HTML into them.
const PLAIN = (() => {
  try {
    const d = document.createElement("div");
    d.contentEditable = "plaintext-only";
    return d.contentEditable === "plaintext-only" ? "plaintext-only" : "true";
  } catch { return "true"; }
})();

function alignOf(cell) {
  const c = cell.trim();
  const left = c.startsWith(":");
  const right = c.endsWith(":");
  return left && right ? "center" : right ? "right" : left ? "left" : "";
}

// The pipe tables of a document, outside code blocks and the front matter.
export function scanTables(doc) {
  const out = [];
  let fence = null;
  let n = 1;
  if (doc.lines > 1 && doc.line(1).text.trim() === "---") {
    for (n = 2; n <= doc.lines; n++) {
      const text = doc.line(n).text.trim();
      if (text === "---" || text === "...") { n += 1; break; }
    }
  }
  for (; n <= doc.lines; n++) {
    const line = doc.line(n);
    const f = FENCE.exec(line.text);
    if (f) {
      if (fence === null) fence = f[1][0];
      else if (f[1][0] === fence) fence = null;
      continue;
    }
    if (fence !== null || !ROW.test(line.text)) continue;
    if (n >= doc.lines || !DELIM.test(doc.line(n + 1).text)) continue;
    let last = n + 1;
    while (last < doc.lines && ROW.test(doc.line(last + 1).text)) last += 1;
    const lines = [];
    for (let i = n; i <= last; i++) lines.push(doc.line(i));
    // Its caption (": Caption {#tbl:id}" or "Table: Caption"), right after it or one blank line away.
    let caption = null;
    for (const at of [last + 1, last + 2, n - 1, n - 2]) {
      if (at < 1 || at > doc.lines) continue;
      const c = doc.line(at);
      const m = CAPTION.exec(c.text);
      const between = at > last ? doc.line(last + 1) : doc.line(n - 1);
      if (m && (Math.abs(at - (at > last ? last : n)) === 1 || !between.text.trim())) {
        caption = { from: c.from, to: c.to, markTo: c.from + m[0].length };
        break;
      }
    }
    out.push({ from: line.from, to: doc.line(last).to, lines: lines.map((l) => l.text), caption });
    n = last;
  }
  return out;
}

// Inline Markdown in a cell as DOM: code, bold, italics, strikethrough, links; the rest as text.
const INLINE = /(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)|\*\*([^*]+)\*\*|__([^_]+)__|\*([^*\s][^*]*)\*|_([^_\s][^_]*)_|~~([^~]+)~~|\[([^\]]*)\]\([^)]*\)|\\([\\|*_`[\]~])/g;
function inline(text) {
  const out = [];
  let at = 0;
  for (const m of text.matchAll(INLINE)) {
    if (m.index > at) out.push(text.slice(at, m.index));
    if (m[1]) out.push(el("code", {}, m[2]));
    else if (m[3] ?? m[4]) out.push(el("strong", {}, ...inline(m[3] ?? m[4])));
    else if (m[5] ?? m[6]) out.push(el("em", {}, ...inline(m[5] ?? m[6])));
    else if (m[7]) out.push(el("s", {}, ...inline(m[7])));
    else if (m[8] !== undefined) out.push(el("span", { class: "cm-md-link" }, ...inline(m[8])));
    else out.push(m[9]);
    at = m.index + m[0].length;
  }
  if (at < text.length) out.push(text.slice(at));
  return out;
}

// Where the content of cell `col` starts in a line of the table's source.
export function cellOffset(line, col) {
  const span = cellSpans(line)[col];
  if (span) return span.from;
  return line.replace(/\s+$/, "").length;
}

// The table that starts at (or contains) `pos`, with a way to get the line of each row: row 0 is
// the header, row r > 0 the r-th line of the body.
function locate(state, pos) {
  const table = tableAt(state, pos);
  if (!table) return null;
  const first = state.doc.lineAt(table.from).number;
  return { ...table, first, line: (r) => state.doc.line(first + (r === 0 ? 0 : r + 1)) };
}

// The cell that a position in a table's source is in, and where in its text (as shown).
function cellAt(state, table, pos) {
  const at = tableAt(state, pos);
  const row = at ? at.row : 0;
  const col = at ? at.col : 0;
  const line = table.line(row);
  const span = cellSpans(line.text)[col];
  if (!span || state.doc.lineAt(pos).number !== line.number) return { row, col, offset: 0 };
  const src = line.text.slice(span.from, span.to);
  const off = Math.max(0, Math.min(src.length, pos - line.from - span.from));
  return { row, col, offset: storedToShown(src, off) };
}

// Where a cell's text starts in the source, and the text.
function cellSource(table, row, col) {
  const line = table.line(row);
  const span = cellSpans(line.text)[col];
  if (!span) return null;
  return { from: line.from + span.from, to: line.from + span.to, text: line.text.slice(span.from, span.to) };
}

// The grid of a table's lines as tableAt reads it: every row as wide as the widest.
function grid(lines) {
  const aligns = splitRow(lines[1]).map(alignOf);
  const rows = [lines[0], ...lines.slice(2)].map(splitRow);
  const width = Math.max(aligns.length, ...rows.map((r) => r.length));
  for (const r of rows) while (r.length < width) r.push("");
  while (aligns.length < width) aligns.push("");
  return { rows, aligns };
}

// -- The caret in a cell, in characters of its text ------------------------------------------

function caretIn(cell) {
  const sel = cell.ownerDocument.getSelection();
  if (!sel?.rangeCount) return null;
  const r = sel.getRangeAt(0);
  if (!cell.contains(r.startContainer) || !cell.contains(r.endContainer)) return null;
  const offset = (node, o) => {
    const pre = cell.ownerDocument.createRange();
    pre.selectNodeContents(cell);
    pre.setEnd(node, o);
    return pre.toString().length;
  };
  return [offset(r.startContainer, r.startOffset), offset(r.endContainer, r.endOffset)];
}

function setCaret(cell, from, to = from) {
  const doc = cell.ownerDocument;
  const point = (n) => {
    const walk = doc.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
    let last = null;
    for (let node = walk.nextNode(); node; node = walk.nextNode()) {
      if (n <= node.length) return [node, n];
      n -= node.length;
      last = node;
    }
    return last ? [last, last.length] : [cell, 0];
  };
  const range = doc.createRange();
  range.setStart(...point(from));
  range.setEnd(...point(to));
  const sel = doc.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
}

// -- Cells -----------------------------------------------------------------------------------

const cellText = (cell) => cell.textContent.replace(/ /g, " ");

// A cell not being typed into shows its formatting; one being typed into, its Markdown.
function render(cell) {
  cell.replaceChildren(...inline(cell.dataset.src));
  delete cell.dataset.editing;
}
function showSource(cell) {
  const caret = caretIn(cell);
  const plain = cellShown(cell.dataset.src);
  cell.dataset.editing = "";
  cell.dataset.fresh = "";
  if (cell.childElementCount === 0 && cell.textContent === plain) return;
  cell.textContent = plain;
  if (caret) setCaret(cell, Math.min(caret[0], plain.length), Math.min(caret[1], plain.length));
}

function buildTable(lines, mode) {
  const { rows, aligns } = grid(lines);
  const cell = (tag, src, r, c) => {
    const node = el(tag, { "data-row": String(r), "data-col": String(c) });
    node.dataset.src = src;
    if (aligns[c]) node.style.textAlign = aligns[c];
    if (mode === "edit") {
      node.contentEditable = PLAIN;
      node.spellcheck = true;
    }
    render(node);
    return node;
  };
  const tr = (tag, cells, r) => el("tr", {}, cells.map((src, c) => cell(tag, src, r, c)));
  const table = el("table", {},
    el("thead", {}, tr("th", rows[0], 0)),
    rows.length > 1 ? el("tbody", {}, rows.slice(1).map((r, i) => tr("td", r, i + 1))) : null);
  table.dataset.rows = String(rows.length);
  table.dataset.cols = String(aligns.length);
  return table;
}

const cellNode = (dom, row, col) => dom.querySelector(`[data-row="${row}"][data-col="${col}"]`);
const activeIn = (view, dom) => {
  const active = view.root.activeElement;
  return active && dom.contains(active) && active.dataset?.row !== undefined ? active : null;
};
// The cell being typed into in this editor, if any.
function activeCell(view) {
  const active = view.root.activeElement;
  return active?.dataset?.row !== undefined && view.contentDOM.contains(active) && active.closest(".cm-table-view")
    ? active : null;
}
// The table view of the table that starts at `from`, if it is drawn.
function viewOf(view, from) {
  for (const dom of view.contentDOM.querySelectorAll(".cm-table-view")) {
    try { if (view.posAtDOM(dom) === from) return dom; } catch { /* not in the document any more */ }
  }
  return null;
}
function tableOf(view, dom) {
  let pos;
  try { pos = view.posAtDOM(dom); } catch { return null; }
  return locate(view.state, pos);
}

// The editor's selection to where the caret is in a cell.
function syncSelection(view, cell) {
  const dom = cell.closest(".cm-table-view");
  const table = dom && tableOf(view, dom);
  const caret = caretIn(cell);
  if (!table || !caret) return;
  const src = cellSource(table, Number(cell.dataset.row), Number(cell.dataset.col));
  if (!src) return;
  const text = cellText(cell);
  const shown = (o) => Math.min(src.to, src.from + shownToStored(text, Math.min(o, text.length)));
  const anchor = shown(caret[0]);
  const head = shown(caret[1]);
  const main = view.state.selection.main;
  if (main.anchor === anchor && main.head === head) return;
  view.dispatch({ selection: { anchor, head }, annotations: fromCell.of(true) });
}

// Typing in a cell: its text in the source, and nothing else.
function writeCell(view, dom, cell) {
  const table = tableOf(view, dom);
  if (!table) return;
  const row = Number(cell.dataset.row);
  const col = Number(cell.dataset.col);
  const text = cellText(cell).replace(/[\r\n]+/g, " ");
  const insert = cellStored(text);
  const caret = caretIn(cell) ?? [text.length, text.length];
  cell.dataset.src = insert.trim();
  // Typing in a cell just entered starts a step of its own to undo.
  const annotations = [fromCell.of(true)];
  if (cell.dataset.fresh !== undefined) {
    annotations.push(isolateHistory.of("before"));
    delete cell.dataset.fresh;
  }
  const src = cellSource(table, row, col);
  if (src) {
    const at = (o) => src.from + shownToStored(text, Math.min(o, text.length));
    if (src.text === insert) return;
    view.dispatch({ changes: charDiff(src.text, insert, src.from), selection: { anchor: at(caret[0]), head: at(caret[1]) },
                    annotations, userEvent: "input.type" });
    return;
  }
  // A cell that this row of the source doesn't have yet: the table is written out again with it.
  const rows = table.rows.map((r) => [...r]);
  rows[row][col] = insert.trim();
  const { text: md, starts } = formatTable(rows, table.aligns);
  const anchor = table.from + starts[row][col] + shownToStored(text, caret[1]);
  view.dispatch({ changes: tableChanges(view.state, table, md), selection: { anchor }, annotations, userEvent: "input.type" });
}

// Leaving a table: its columns padded again, without moving anything typed.
function tidy(view, dom) {
  if (!dom.isConnected || activeIn(view, dom)) return;
  const table = tableOf(view, dom);
  if (!table || view.state.readOnly) return;
  const changes = tidyChanges(view.state, table);
  if (changes.length) {
    view.dispatch({ changes, annotations: [fromCell.of(true), Transaction.addToHistory.of(false)] });
  }
}

// The caret into a cell of a table, by setting the editor's selection there.
function selectCell(view, table, row, col, { at = "end" } = {}) {
  const src = cellSource(table, row, col);
  if (!src) {
    const line = table.line(row);
    view.dispatch({ selection: { anchor: line.to }, scrollIntoView: true });
    return;
  }
  const anchor = at === "start" ? src.from : src.to;
  view.dispatch({ selection: { anchor }, scrollIntoView: true });
}

// Out of the table, above or below it, back into the text.
function leave(view, table, where) {
  const doc = view.state.doc;
  if (where === "above") {
    if (table.from === 0) return;
    view.dispatch({ selection: { anchor: table.from - 1 }, scrollIntoView: true });
  } else if (table.to < doc.length) {
    view.dispatch({ selection: { anchor: table.to + 1 }, scrollIntoView: true });
  } else {
    view.dispatch({ changes: { from: table.to, insert: "\n" }, selection: { anchor: table.to + 1 }, scrollIntoView: true });
  }
  view.focus();
}

// Cells copied from a spreadsheet (tab-separated lines): the table grows to take them.
function pasteGrid(view, table, row, col, cells) {
  const rows = table.rows.map((r) => [...r]);
  const aligns = [...table.aligns];
  const width = Math.max(aligns.length, col + Math.max(...cells.map((r) => r.length)));
  while (aligns.length < width) aligns.push("");
  for (const r of rows) while (r.length < width) r.push("");
  while (rows.length < row + cells.length) rows.push(aligns.map(() => ""));
  cells.forEach((line, i) => line.forEach((value, j) => { rows[row + i][col + j] = cellStored(value.trim()); }));
  const { text, starts } = formatTable(rows, aligns);
  const lastRow = row + cells.length - 1;
  const lastCol = col + cells[cells.length - 1].length - 1;
  const anchor = table.from + starts[lastRow][lastCol] + rows[lastRow][lastCol].length;
  view.dispatch({ changes: tableChanges(view.state, table, text), selection: { anchor },
                  scrollIntoView: true, userEvent: "input.paste" });
}

// Where a click in a cell's formatted text falls in its Markdown: the characters shown are
// matched up with the Markdown's, skipping its marks.
function clickOffset(cell, x, y) {
  const doc = cell.ownerDocument;
  let node;
  let offset;
  if (doc.caretPositionFromPoint) ({ offsetNode: node, offset } = doc.caretPositionFromPoint(x, y) ?? {});
  else if (doc.caretRangeFromPoint) ({ startContainer: node, startOffset: offset } = doc.caretRangeFromPoint(x, y) ?? {});
  if (!node || !cell.contains(node)) return null;
  const pre = doc.createRange();
  pre.selectNodeContents(cell);
  pre.setEnd(node, offset);
  const shown = pre.toString();
  const src = cellShown(cell.dataset.src);
  let i = 0;
  for (let j = 0; j < shown.length && i < src.length; i++) if (src[i] === shown[j]) j++;
  return i;
}

function listen(view, dom) {
  const target = (e) => {
    const cell = e.target.closest?.("[data-row]");
    return cell && dom.contains(cell) && dom.dataset.mode === "edit" ? cell : null;
  };
  // A click on a cell with formatting: its Markdown comes in under the pointer, so the caret is put
  // where the click was, in the Markdown.
  let clicked = null;
  dom.addEventListener("pointerdown", (e) => {
    const cell = target(e);
    clicked = cell && cell.dataset.editing === undefined && cell.childElementCount
      ? { cell, offset: clickOffset(cell, e.clientX, e.clientY) } : null;
  }, true);
  dom.addEventListener("focusin", (e) => {
    const cell = target(e);
    if (!cell) return;
    showSource(cell);
    const click = clicked?.cell === cell ? clicked : null;
    clicked = null;
    if (click) {
      setTimeout(() => {
        if (view.root.activeElement !== cell) return;
        const length = cellText(cell).length;
        setCaret(cell, Math.min(click.offset ?? length, length));
      }, 0);
    }
  });
  dom.addEventListener("focusout", (e) => {
    const cell = target(e);
    if (!cell) return;
    if (cell.isConnected) render(cell);
    if (!dom.contains(e.relatedTarget)) setTimeout(() => tidy(view, dom), 0);
  });
  dom.addEventListener("input", (e) => {
    const cell = target(e);
    if (cell) writeCell(view, dom, cell);
  });
  dom.addEventListener("beforeinput", (e) => {
    const cell = target(e);
    if (!cell) return;
    if (e.inputType === "historyUndo" || e.inputType === "historyRedo") {
      e.preventDefault();
      (e.inputType === "historyUndo" ? undo : redo)(view);
    } else if (e.inputType === "insertParagraph" || e.inputType === "insertLineBreak") {
      e.preventDefault();
      move(cell, 1, true);
    } else if (e.inputType === "insertFromDrop" || e.inputType === "formatBold" || e.inputType === "formatItalic") {
      e.preventDefault();
    }
  });
  dom.addEventListener("paste", (e) => {
    const cell = target(e);
    if (!cell) return;
    e.preventDefault();
    const text = (e.clipboardData?.getData("text/plain") ?? "").replace(/\r\n?/g, "\n").replace(/\n+$/, "");
    const fromSheet = text.includes("\t") || (text.includes("\n") && /<table/i.test(e.clipboardData?.getData("text/html") ?? ""));
    if (!fromSheet) {
      cell.ownerDocument.execCommand("insertText", false, text.replace(/\s*\n\s*/g, " "));
      return;
    }
    const table = tableOf(view, dom);
    if (table) pasteGrid(view, table, Number(cell.dataset.row), Number(cell.dataset.col), text.split("\n").map((l) => l.split("\t")));
  });
  dom.addEventListener("dragover", (e) => { if (e.dataTransfer?.types.includes("Files")) e.preventDefault(); });
  dom.addEventListener("drop", (e) => { if (e.dataTransfer?.types.includes("Files")) e.preventDefault(); });

  // Up and down a column; at the edge of the table, out of it (or, with Enter, a new row).
  const move = (cell, by, grow = false) => {
    const table = tableOf(view, dom);
    if (!table) return;
    const row = Number(cell.dataset.row) + by;
    const col = Number(cell.dataset.col);
    if (row < 0) leave(view, table, "above");
    else if (row >= table.rows.length) {
      if (grow) addRow(view);
      else leave(view, table, "below");
    } else selectCell(view, table, row, col);
  };
  dom.addEventListener("keydown", (e) => {
    const cell = target(e);
    if (!cell || e.isComposing || e.keyCode === 229) return;
    syncSelection(view, cell);
    const mod = e.ctrlKey || e.metaKey;
    const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    const caret = caretIn(cell);
    const length = cellText(cell).length;
    const plain = !e.shiftKey && !mod && !e.altKey;
    let done = true;
    if (key === "Tab" && !mod) (e.shiftKey ? previousCell : nextCell)(view);
    else if (key === "Enter" && !mod) move(cell, e.shiftKey ? -1 : 1, !e.shiftKey);
    else if (key === "Escape") { const table = tableOf(view, dom); if (table) leave(view, table, "below"); }
    else if (key === "ArrowUp" && plain) move(cell, -1);
    else if (key === "ArrowDown" && plain) move(cell, 1);
    else if (key === "ArrowLeft" && plain && caret && caret[0] === 0 && caret[1] === 0) {
      const table = tableOf(view, dom);
      const row = Number(cell.dataset.row);
      const col = Number(cell.dataset.col);
      if (!table) done = false;
      else if (col > 0) selectCell(view, table, row, col - 1);
      else if (row > 0) selectCell(view, table, row - 1, table.aligns.length - 1);
      else leave(view, table, "above");
    } else if (key === "ArrowRight" && plain && caret && caret[0] === length && caret[1] === length) {
      const table = tableOf(view, dom);
      const row = Number(cell.dataset.row);
      const col = Number(cell.dataset.col);
      if (!table) done = false;
      else if (col < table.aligns.length - 1) selectCell(view, table, row, col + 1, { at: "start" });
      else if (row < table.rows.length - 1) selectCell(view, table, row + 1, 0, { at: "start" });
      else leave(view, table, "below");
    } else if (mod && key === "z") (e.shiftKey ? redo : undo)(view);
    else if (mod && key === "y" && !e.shiftKey) redo(view);
    else if (mod && key === "b" && !e.shiftKey) toggleBold(view);
    else if (mod && key === "i" && !e.shiftKey) toggleItalic(view);
    else done = false;
    if (done) {
      e.preventDefault();
      e.stopPropagation();
    }
  });
  // In suggest mode, a cell opens the table's Markdown, where typing becomes a suggestion.
  dom.addEventListener("click", (e) => {
    if (dom.dataset.mode !== "locked") return;
    const cell = e.target.closest("[data-row]");
    const table = cell && tableOf(view, dom);
    if (!table || window.getSelection()?.toString()) return;
    const src = cellSource(table, Number(cell.dataset.row), Number(cell.dataset.col));
    const anchor = src ? src.from : table.line(Number(cell.dataset.row)).from;
    view.dispatch({ effects: showTableSource.of(table.from), selection: { anchor }, scrollIntoView: true });
    view.focus();
  });
}

class TableWidget extends WidgetType {
  constructor(table, mode) {
    super();
    this.table = table;
    this.mode = mode; // "read" (review, read only), "locked" (suggest mode) or "edit"
    this.key = table.lines.join("\n");
  }

  eq(other) { return other.key === this.key && other.mode === this.mode; }

  toDOM(view) {
    const dom = el("div", { class: "cm-table-view" }, el("div", { class: "cm-table-scroll" }, buildTable(this.table.lines, this.mode)));
    dom.dataset.mode = this.mode;
    if (this.mode !== "read") listen(view, dom);
    return dom;
  }

  // The same table with other text: only the cells that changed are redrawn, and the one being
  // typed into keeps its caret.
  updateDOM(dom, view) {
    if (dom.dataset.mode !== this.mode) return false;
    const table = dom.querySelector("table");
    const { rows, aligns } = grid(this.table.lines);
    if (Number(table.dataset.rows) !== rows.length || Number(table.dataset.cols) !== aligns.length) {
      const active = activeIn(view, dom);
      const next = buildTable(this.table.lines, this.mode);
      table.replaceWith(next);
      if (active) {
        const row = Math.min(rows.length - 1, Number(active.dataset.row));
        const col = Math.min(aligns.length - 1, Number(active.dataset.col));
        cellNode(dom, row, col)?.focus({ preventScroll: true });
      }
      return true;
    }
    rows.forEach((cells, r) => cells.forEach((src, c) => {
      const cell = cellNode(dom, r, c);
      if (!cell) return;
      cell.style.textAlign = aligns[c];
      if (cell.dataset.src === src) return;
      cell.dataset.src = src;
      if (cell.dataset.editing === undefined) render(cell);
      else if (cellText(cell).trim() !== cellShown(src)) {
        const caret = caretIn(cell);
        cell.textContent = cellShown(src);
        if (caret && view.root.activeElement === cell) setCaret(cell, Math.min(caret[1], cell.textContent.length));
      }
    }));
    return true;
  }

  ignoreEvent() { return true; }

  get estimatedHeight() { return 32 * this.table.lines.length; }
}

// -- The selection into cells ----------------------------------------------------------------

// The table a selection is in, and whether it is in one cell; edges count when coming from outside.
function selectionCell(state, tables, sel, before) {
  const table = tables.find((t) => sel.from >= t.from && sel.to <= t.to);
  if (!table) return null;
  const info = locate(state, table.from);
  if (!info) return null;
  if (sel.empty && sel.head === table.to && before < table.from) return { table, info, row: 0, col: 0, from: 0, to: 0 };
  if (sel.empty && sel.head === table.from && before > table.to) {
    return { table, info, row: info.rows.length - 1, col: 0, from: 0, to: 0 };
  }
  const a = cellAt(state, info, sel.from);
  const b = sel.empty ? a : cellAt(state, info, sel.to);
  if (a.row !== b.row || a.col !== b.col) return { table, info, cross: true };
  return { table, info, row: a.row, col: a.col, from: a.offset, to: b.offset };
}

// Puts the caret in the cell that the editor's selection is in. focus: whether the cell may take
// the focus (the editor or a cell had it); otherwise the cell is only marked, as a search match.
function mirror(view, before, focus) {
  const value = view.state.field(tableField, false);
  if (!value || value.mode !== "edit") return false;
  const sel = view.state.selection.main;
  const at = selectionCell(view.state, value.tables, sel, before);
  for (const marked of view.contentDOM.querySelectorAll(".cm-table-view .is-selected")) marked.classList.remove("is-selected");
  if (!at || at.cross || value.rawAt === at.table.from) {
    if (focus && activeCell(view)) view.focus();
    return false;
  }
  const dom = viewOf(view, at.table.from);
  const cell = dom && cellNode(dom, at.row, at.col);
  if (!cell) return false;
  if (!focus) {
    if (!sel.empty) cell.classList.add("is-selected"); // a search match
    return false;
  }
  if (view.root.activeElement !== cell) cell.focus({ preventScroll: true });
  const length = cellText(cell).length;
  setCaret(cell, Math.min(at.from, length), Math.min(at.to, length));
  cell.scrollIntoView({ block: "nearest", inline: "nearest" });
  return true;
}

// The focus back to the editor after a toolbar command: into the cell the selection is in, if any.
export function focusEditor(view) {
  if (!mirror(view, view.state.selection.main.head, true)) view.focus();
}

const mirrorPlugin = ViewPlugin.fromClass(class {
  constructor(view) {
    this.view = view;
    this.onSelection = () => {
      const cell = activeCell(view);
      if (cell && cell.dataset.editing !== undefined) syncSelection(view, cell);
    };
    view.dom.ownerDocument.addEventListener("selectionchange", this.onSelection);
    // The editor taking the focus back (closing the search panel, a popover) with the selection
    // in a table: the cell takes it. Not when a click or tap put the focus there.
    this.pointer = 0;
    this.onPointer = () => { this.pointer = Date.now(); };
    this.onFocus = (e) => {
      if (e.target !== view.contentDOM || Date.now() - this.pointer < 600) return;
      const head = view.state.selection.main.head;
      setTimeout(() => {
        if (view.hasFocus && view.state.selection.main.head === head && Date.now() - this.pointer > 600) mirror(view, head, true);
      }, 0);
    };
    view.contentDOM.addEventListener("pointerdown", this.onPointer, true);
    view.contentDOM.addEventListener("focus", this.onFocus);
  }

  update(u) {
    if (!u.selectionSet || u.transactions.every((tr) => !tr.selection || tr.annotation(fromCell))) return;
    const focus = u.view.hasFocus || Boolean(activeCell(u.view));
    const before = u.startState.selection.main.head;
    queueMicrotask(() => mirror(u.view, before, focus));
  }

  destroy() {
    this.view.dom.ownerDocument.removeEventListener("selectionchange", this.onSelection);
    this.view.contentDOM.removeEventListener("pointerdown", this.onPointer, true);
    this.view.contentDOM.removeEventListener("focus", this.onFocus);
  }
});

// The arrow keys into a table from the line above or below it (CodeMirror steps over a block
// widget): the caret goes into its first or last row.
function enter(forward) {
  return (view) => {
    const value = view.state.field(tableField, false);
    const sel = view.state.selection.main;
    if (!value || value.mode !== "edit" || !sel.empty) return false;
    const target = view.moveVertically(sel, forward).head;
    const table = value.tables.find((t) => t.from !== value.rawAt
      && (forward ? sel.head < t.from && target >= t.from : sel.head > t.to && target <= t.to));
    const info = table && locate(view.state, table.from);
    if (!info) return false;
    selectCell(view, info, forward ? 0 : info.rows.length - 1, 0, { at: "start" });
    return true;
  };
}

// -- The decorations -------------------------------------------------------------------------

function modeOf(state) {
  const { reading, locked } = state.facet(config);
  return reading || state.readOnly ? "read" : locked(state) ? "locked" : "edit";
}

function build(state, tables, rawAt, mode) {
  const sel = state.selection.ranges;
  const reading = mode === "read" && state.facet(config).reading;
  const out = [];
  const touches = (from, to) => sel.some((r) => r.from <= to && r.to >= from);
  for (const table of tables) {
    const cap = table.caption;
    const showCap = (c) => {
      out.push(captionLine.range(c.from));
      if (reading || !touches(c.from, c.to)) out.push(hidden.range(c.from, c.markTo));
    };
    if (cap && cap.from < table.from) showCap(cap);
    if (table.from !== rawAt) {
      out.push(Decoration.replace({ widget: new TableWidget(table, mode), block: true }).range(table.from, table.to));
    }
    if (cap && cap.from > table.to) showCap(cap);
  }
  return Decoration.set(out, true);
}

const tableField = StateField.define({
  create(state) {
    const tables = scanTables(state.doc);
    const mode = modeOf(state);
    return { tables, rawAt: null, mode, deco: build(state, tables, null, mode) };
  },
  update(value, tr) {
    let { tables, rawAt } = value;
    const mode = modeOf(tr.state);
    if (tr.docChanged) {
      tables = scanTables(tr.state.doc);
      if (rawAt !== null) rawAt = tr.changes.mapPos(rawAt, -1);
    }
    for (const e of tr.effects) if (e.is(showTableSource)) rawAt = e.value;
    const sel = tr.state.selection.main;
    // A selection over several cells, or from outside into a table, shows the table's Markdown.
    if (tr.selection && !tr.annotation(fromCell) && !sel.empty && mode !== "read") {
      for (const t of tables) {
        const inside = (p) => p > t.from && p < t.to;
        if (inside(sel.from) !== inside(sel.to)
            || (inside(sel.from) && selectionCell(tr.state, tables, sel, sel.head)?.cross)) rawAt = t.from;
      }
    }
    // The Markdown shows until the selection leaves the table.
    if (rawAt !== null) {
      const t = tables.find((x) => x.from === rawAt);
      if (!t || !tr.state.selection.ranges.some((r) => r.from <= t.to && r.to >= t.from)) rawAt = null;
    }
    if (!tr.docChanged && !tr.selection && !tr.effects.length && rawAt === value.rawAt && mode === value.mode) return value;
    return { tables, rawAt, mode, deco: build(tr.state, tables, rawAt, mode) };
  },
  provide: (f) => [EditorView.decorations.from(f, (v) => v.deco),
                   EditorView.atomicRanges.of((view) => view.state.field(f).deco)],
});

// Whether a table is being edited, for the table bar: a cell has the caret, or the cursor is in
// a table's Markdown.
export function editingTable(view) {
  const value = view.state.field(tableField, false);
  const head = view.state.selection.main.head;
  const table = value?.tables.find((t) => head >= t.from && head <= t.to);
  if (!table) return false;
  return Boolean(activeCell(view)) || value.mode !== "edit" || table.from === value.rawAt;
}

// reading: the review view, where tables are only read. locked(state): typing goes elsewhere
// (suggest mode), so a cell opens the table's Markdown instead.
export function tableView({ reading = false, locked = () => false } = {}) {
  return [config.of({ reading, locked }), tableField, mirrorPlugin,
          Prec.high(keymap.of([{ key: "ArrowDown", run: enter(true) }, { key: "ArrowUp", run: enter(false) }]))];
}

// Tables in the editor (ARCHITECTURE.md §6.3): a pipe table shows as a table, with its header,
// alignments and inline formatting, off the lines being edited. A click on a cell puts the cursor
// in that cell of the source, where the table tools (editor/tables.js) take over. Wide tables
// scroll sideways inside the text, so they never make the page wider than a phone.
import { Decoration, EditorView, StateField, WidgetType } from "codemirror";
import { el } from "../ui/dom.js";
import { splitRow } from "./tables.js";

const ROW = /^\s*\|/;
const DELIM = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;
const FENCE = /^\s{0,3}(```+|~~~+)/;
const CAPTION = /^(?::|Table:)[ \t]+/;
const captionLine = Decoration.line({ class: "cm-table-caption" });
const hidden = Decoration.replace({});

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
  let i = 0;
  while (i < line.length && /\s/.test(line[i])) i++;
  if (line[i] === "|") i++;
  let code = false;
  for (let c = 0; c < col && i < line.length; i++) {
    if (line[i] === "\\") { i++; continue; }
    if (line[i] === "`") code = !code;
    else if (line[i] === "|" && !code) c++;
  }
  while (i < line.length && line[i] === " ") i++;
  return Math.min(i, line.length);
}

class TableWidget extends WidgetType {
  constructor(table, reading) {
    super();
    this.table = table;
    this.reading = reading;
    this.key = table.lines.join("\n");
  }

  eq(other) { return other.key === this.key && other.reading === this.reading; }

  toDOM(view) {
    const [head, delim, ...body] = this.table.lines;
    const aligns = splitRow(delim).map(alignOf);
    const cell = (tag, text, r, c) => el(tag, { "data-row": String(r), "data-col": String(c),
                                               style: aligns[c] ? `text-align: ${aligns[c]}` : undefined }, ...inline(text));
    const width = Math.max(aligns.length, splitRow(head).length);
    const row = (tag, line, r) => {
      const cells = splitRow(line);
      while (cells.length < width) cells.push("");
      return el("tr", {}, cells.slice(0, width).map((text, c) => cell(tag, text, r, c)));
    };
    const table = el("table", {},
      el("thead", {}, row("th", head, 0)),
      body.length ? el("tbody", {}, body.map((line, i) => row("td", line, i + 2))) : null);
    const dom = el("div", { class: "cm-table-view" }, el("div", { class: "cm-table-scroll" }, table));
    if (!this.reading) {
      dom.addEventListener("click", (event) => {
        const target = event.target.closest("[data-row]");
        if (!target || window.getSelection()?.toString()) return;
        this.edit(view, dom, Number(target.dataset.row), Number(target.dataset.col));
      });
    }
    return dom;
  }

  // Into the source, at the start of the cell that was clicked.
  edit(view, dom, row, col) {
    let pos;
    try { pos = view.posAtDOM(dom); } catch { return; }
    const first = view.state.doc.lineAt(pos);
    const n = Math.min(view.state.doc.lines, first.number + row);
    const line = view.state.doc.line(n);
    view.dispatch({ selection: { anchor: line.from + cellOffset(line.text, col) }, scrollIntoView: true });
    view.focus();
  }

  ignoreEvent() { return true; }

  get estimatedHeight() { return 32 * this.table.lines.length; }
}

// reading: the review view, where tables always show as tables.
export function tableView({ reading = false } = {}) {
  const build = (state, tables) => {
    const sel = state.selection.ranges;
    const out = [];
    const touches = (from, to) => sel.some((r) => r.from <= to && r.to >= from);
    for (const table of tables) {
      const cap = table.caption;
      if (cap && cap.from < table.from) {
        out.push(captionLine.range(cap.from));
        if (reading || !touches(cap.from, cap.to)) out.push(hidden.range(cap.from, cap.markTo));
      }
      if (reading || !touches(table.from, table.to)) {
        out.push(Decoration.replace({ widget: new TableWidget(table, reading), block: true }).range(table.from, table.to));
      }
      if (cap && cap.from > table.to) {
        out.push(captionLine.range(cap.from));
        if (reading || !touches(cap.from, cap.to)) out.push(hidden.range(cap.from, cap.markTo));
      }
    }
    return Decoration.set(out, true);
  };
  return StateField.define({
    create(state) {
      const tables = scanTables(state.doc);
      return { tables, deco: build(state, tables) };
    },
    update(value, tr) {
      if (!tr.docChanged && !tr.selection) return value;
      const tables = tr.docChanged ? scanTables(tr.state.doc) : value.tables;
      return { tables, deco: build(tr.state, tables) };
    },
    provide: (f) => EditorView.decorations.from(f, (v) => v.deco),
  });
}

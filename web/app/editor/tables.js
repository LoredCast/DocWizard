// Pipe tables (ARCHITECTURE.md §6.3): inserting one from the toolbar's grid, moving between cells
// with Tab and Shift+Tab, adding and removing rows and columns, aligning columns. Every command
// rewrites the whole table with its columns padded, so the source stays readable. Works on any
// EditorState-like object (doc.lineAt, doc.line, doc.lines, sliceDoc); nothing here needs a DOM.

const DELIM = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;
const ROW = /^\s*\|/;

// The cells of a row, without the outer pipes; escaped pipes stay in their cell.
export function splitRow(line) {
  let text = line.trim();
  if (text.startsWith("|")) text = text.slice(1);
  if (text.endsWith("|") && !text.endsWith("\\|")) text = text.slice(0, -1);
  const cells = [];
  let cur = "";
  let code = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === "\\" && i + 1 < text.length) {
      cur += ch + text[i + 1];
      i += 1;
    } else if (ch === "`") {
      code = !code;
      cur += ch;
    } else if (ch === "|" && !code) {
      cells.push(cur.trim());
      cur = "";
    } else {
      cur += ch;
    }
  }
  cells.push(cur.trim());
  return cells;
}

function alignOf(cell) {
  const c = cell.trim();
  const left = c.startsWith(":");
  const right = c.endsWith(":");
  return left && right ? "center" : right ? "right" : left ? "left" : "";
}

// The table around a position: its lines, cells, alignments, and the cell the position is in.
export function tableAt(state, pos) {
  const doc = state.doc;
  const here = doc.lineAt(pos);
  if (!ROW.test(here.text)) return null;
  let first = here.number;
  let last = here.number;
  while (first > 1 && ROW.test(doc.line(first - 1).text)) first -= 1;
  while (last < doc.lines && ROW.test(doc.line(last + 1).text)) last += 1;
  if (last - first < 1 || !DELIM.test(doc.line(first + 1).text)) return null;
  const rows = [];
  let aligns = [];
  for (let n = first; n <= last; n++) {
    const cells = splitRow(doc.line(n).text);
    if (n === first + 1) aligns = cells.map(alignOf);
    else rows.push(cells);
  }
  const width = Math.max(aligns.length, ...rows.map((r) => r.length));
  for (const r of rows) while (r.length < width) r.push("");
  while (aligns.length < width) aligns.push("");
  // Which cell: the row (0 is the header; the delimiter row counts as the header), and the column
  // by the pipes before the position.
  const row = here.number <= first + 1 ? 0 : here.number - first - 1;
  const col = clampCol(here.text.slice(0, pos - here.from), width);
  return { from: doc.line(first).from, to: doc.line(last).to, rows, aligns, row, col };
}

function clampCol(before, width) {
  // Pipes before the position, minus the leading one, outside code and escapes.
  let n = 0;
  let code = false;
  const text = before.replace(/^\s*\|/, "");
  for (let i = 0; i < text.length; i++) {
    if (text[i] === "\\") { i += 1; continue; }
    if (text[i] === "`") code = !code;
    else if (text[i] === "|" && !code) n += 1;
  }
  return Math.max(0, Math.min(width - 1, n));
}

const cellWidth = (s) => [...s].length;

// The table's text with padded columns, and where each cell's content starts.
export function formatTable(rows, aligns) {
  const width = aligns.length;
  const widths = Array.from({ length: width }, (_, i) => Math.max(3, ...rows.map((r) => cellWidth(r[i] ?? ""))));
  const starts = [];
  const lines = [];
  let offset = 0;
  const line = (cells, r) => {
    let text = "|";
    const at = [];
    cells.forEach((c, i) => {
      text += " ";
      at.push(offset + text.length);
      const pad = widths[i] - cellWidth(c);
      const a = aligns[i];
      const left = a === "right" ? pad : a === "center" ? Math.floor(pad / 2) : 0;
      at[i] += left;
      text += " ".repeat(left) + c + " ".repeat(pad - left) + " |";
    });
    if (r !== null) starts[r] = at;
    lines.push(text);
    offset += text.length + 1;
  };
  line(rows[0], 0);
  line(widths.map((w, i) => {
    const a = aligns[i];
    const dashes = "-".repeat(Math.max(1, w - (a === "center" ? 2 : a ? 1 : 0)));
    return a === "center" ? `:${dashes}:` : a === "right" ? `${dashes}:` : a === "left" ? `:${dashes}` : dashes;
  }), null);
  rows.slice(1).forEach((r, i) => line(r, i + 1));
  return { text: lines.join("\n"), starts };
}

function rewrite(view, table, rows, aligns, row, col, { select = false } = {}) {
  const { text, starts } = formatTable(rows, aligns);
  const r = Math.max(0, Math.min(rows.length - 1, row));
  const c = Math.max(0, Math.min(aligns.length - 1, col));
  const anchor = table.from + starts[r][c];
  const len = rows[r][c].length;
  view.dispatch({
    changes: { from: table.from, to: table.to, insert: text },
    selection: select && len ? { anchor, head: anchor + len } : { anchor: anchor + len },
    scrollIntoView: true,
    userEvent: "input",
  });
  return true;
}

const copyRows = (rows) => rows.map((r) => [...r]);

export function nextCell(view) {
  const table = tableAt(view.state, view.state.selection.main.head);
  if (!table) return false;
  const rows = copyRows(table.rows);
  let { row, col } = table;
  col += 1;
  if (col >= table.aligns.length) {
    col = 0;
    row += 1;
    // Tab in the last cell adds a row, as in word processors.
    if (row >= rows.length) rows.push(table.aligns.map(() => ""));
  }
  return rewrite(view, table, rows, table.aligns, row, col, { select: true });
}

export function previousCell(view) {
  const table = tableAt(view.state, view.state.selection.main.head);
  if (!table) return false;
  let { row, col } = table;
  col -= 1;
  if (col < 0) {
    if (row === 0) return rewrite(view, table, table.rows, table.aligns, 0, 0, { select: true });
    row -= 1;
    col = table.aligns.length - 1;
  }
  return rewrite(view, table, table.rows, table.aligns, row, col, { select: true });
}

export function addRow(view) {
  const table = tableAt(view.state, view.state.selection.main.head);
  if (!table) return false;
  const rows = copyRows(table.rows);
  rows.splice(table.row + 1, 0, table.aligns.map(() => ""));
  return rewrite(view, table, rows, table.aligns, table.row + 1, table.col);
}

export function addColumn(view) {
  const table = tableAt(view.state, view.state.selection.main.head);
  if (!table) return false;
  const rows = copyRows(table.rows);
  rows.forEach((r) => r.splice(table.col + 1, 0, ""));
  const aligns = [...table.aligns];
  aligns.splice(table.col + 1, 0, "");
  return rewrite(view, table, rows, aligns, table.row, table.col + 1);
}

export function deleteRow(view) {
  const table = tableAt(view.state, view.state.selection.main.head);
  if (!table) return false;
  if (table.rows.length <= 1) return deleteTable(view);
  const rows = copyRows(table.rows);
  rows.splice(table.row, 1);
  return rewrite(view, table, rows, table.aligns, Math.min(table.row, rows.length - 1), table.col);
}

export function deleteColumn(view) {
  const table = tableAt(view.state, view.state.selection.main.head);
  if (!table) return false;
  if (table.aligns.length <= 1) return deleteTable(view);
  const rows = copyRows(table.rows);
  rows.forEach((r) => r.splice(table.col, 1));
  const aligns = [...table.aligns];
  aligns.splice(table.col, 1);
  return rewrite(view, table, rows, aligns, table.row, Math.min(table.col, aligns.length - 1));
}

export function deleteTable(view) {
  const table = tableAt(view.state, view.state.selection.main.head);
  if (!table) return false;
  const doc = view.state.doc;
  // The blank line after it goes too.
  const blank = table.to < doc.length && doc.lineAt(table.to + 1).text.trim() === "";
  const end = blank ? Math.min(doc.length, doc.lineAt(table.to + 1).to + 1) : table.to;
  view.dispatch({ changes: { from: table.from, to: end, insert: "" }, selection: { anchor: table.from },
                  scrollIntoView: true, userEvent: "delete" });
  return true;
}

// Left, centered, right, as the layout sets it: the column's alignment goes round.
export function cycleAlign(view) {
  const table = tableAt(view.state, view.state.selection.main.head);
  if (!table) return false;
  const order = ["", "center", "right", "left"];
  const aligns = [...table.aligns];
  aligns[table.col] = order[(order.indexOf(aligns[table.col]) + 1) % order.length];
  return rewrite(view, table, table.rows, aligns, table.row, table.col);
}

export function formatTableAt(view) {
  const table = tableAt(view.state, view.state.selection.main.head);
  if (!table) return false;
  return rewrite(view, table, table.rows, table.aligns, table.row, table.col);
}

// A new table of rows × cols (the header is one of the rows), as a paragraph of its own, with
// its first header cell selected to type over.
export function insertTable(view, rowCount, colCount, header = (i) => `Column ${i + 1}`) {
  const { state } = view;
  const doc = state.doc;
  const line = doc.lineAt(state.selection.main.from);
  const rows = [Array.from({ length: colCount }, (_, i) => header(i))];
  for (let r = 1; r < Math.max(2, rowCount); r++) rows.push(Array.from({ length: colCount }, () => ""));
  const { text, starts } = formatTable(rows, rows[0].map(() => ""));
  const blank = !line.text.trim();
  // On an empty line the table takes its place; otherwise it goes after the line.
  const from = blank ? line.from : line.to;
  const to = line.to;
  const prevText = blank ? line.number > 1 && doc.line(line.number - 1).text.trim() : true;
  const nextText = line.number < doc.lines && doc.line(line.number + 1).text.trim();
  const lead = blank ? (prevText ? "\n" : "") : "\n\n";
  const tail = to >= doc.length ? "\n" : nextText ? "\n" : "";
  const insert = lead + text + tail;
  const anchor = from + lead.length + starts[0][0];
  view.dispatch({
    changes: { from, to, insert },
    selection: { anchor, head: anchor + rows[0][0].length },
    scrollIntoView: true,
    userEvent: "input",
  });
  return true;
}

// Data sheets as the grid sees them (ARCHITECTURE.md §6.3): parsing .csv and .tsv text into
// cells that remember how they were written, writing them back so that only edited cells
// change in the text, and working out formulas. filters/dwsheet.lua does the same for renders.

export function separatorFor(text, path = "") {
  if (/\.tsv$/i.test(path)) return "\t";
  const first = (text.match(/^[^\n]*/)?.[0] ?? "").replace(/"[^"]*"/g, "");
  let best = ",";
  let count = -1;
  for (const sep of [",", ";", "\t"]) {
    const n = first.split(sep).length - 1;
    if (n > count) { best = sep; count = n; }
  }
  return best;
}

// Rows of cells: { value, src } where src is the cell as written in the file (quotes included).
export function parseSheet(text, sep = separatorFor(text)) {
  const rows = [];
  let row = [];
  let i = 0;
  const n = text.length;
  if (text.charCodeAt(0) === 0xfeff) i = 1;
  let start = i;
  let value = "";
  let quoted = false;
  let inQuotes = false;
  const endCell = (end) => {
    row.push({ value, src: text.slice(start, end).replace(/\r$/, "") });
    value = "";
    quoted = false;
  };
  while (i < n) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') { value += '"'; i += 1; } else inQuotes = false;
      } else value += c;
    } else if (c === '"' && value === "" && !quoted) {
      inQuotes = true;
      quoted = true;
    } else if (c === sep) {
      endCell(i);
      start = i + 1;
    } else if (c === "\n") {
      if (value.endsWith("\r") && !quoted) value = value.slice(0, -1);
      endCell(i);
      rows.push(row);
      row = [];
      start = i + 1;
    } else value += c;
    i += 1;
  }
  if (start < n || row.length) {
    if (value.endsWith("\r") && !quoted) value = value.slice(0, -1);
    endCell(n);
    rows.push(row);
  }
  return rows;
}

export function quoteCell(value, sep) {
  if (value === "") return "";
  if (value.includes(sep) || value.includes('"') || value.includes("\n") || value.includes("\r")
      || /^\s|\s$/.test(value)) {
    return `"${value.replace(/"/g, '""')}"`;
  }
  return value;
}

// The text for *rows*; unchanged cells keep how they were written.
export function serializeSheet(rows, sep, trailingNewline = true) {
  const lines = rows.map((row) => row.map((cell) => cell.src ?? quoteCell(cell.value, sep)).join(sep));
  return lines.join("\n") + (trailingNewline && rows.length ? "\n" : "");
}

// The smallest change from *before* to *after*: one replaced range.
export function minimalChange(before, after) {
  let start = 0;
  const max = Math.min(before.length, after.length);
  while (start < max && before.charCodeAt(start) === after.charCodeAt(start)) start += 1;
  let endB = before.length;
  let endA = after.length;
  while (endB > start && endA > start && before.charCodeAt(endB - 1) === after.charCodeAt(endA - 1)) {
    endB -= 1;
    endA -= 1;
  }
  if (start === endB && start === endA) return null;
  return { from: start, to: endB, insert: after.slice(start, endA) };
}

export function setCell(rows, r, c, value, sep) {
  while (rows.length <= r) rows.push([]);
  const row = rows[r];
  while (row.length <= c) row.push({ value: "", src: "" });
  row[c] = { value, src: quoteCell(value, sep) };
}

// -- Numbers and formulas -------------------------------------------------------
// "1,234.5", "1.234,5", "12%", "€3" -> number, or null.
export function toNumber(s) {
  if (typeof s === "number") return s;
  if (typeof s !== "string") return null;
  let t = s.trim();
  if (!t) return null;
  const percent = t.endsWith("%");
  t = t.replace(/%$/, "").replace(/^[$€£¥]|[$€£¥]$/g, "").replace(/[  ']/g, "");
  const grouped = (str, sep) => {
    const m = str.match(/^[-+]?(\d{1,3})(.*)$/);
    if (!m || m[2] === "") return false;
    return m[2].split(sep).slice(1).every((g) => /^\d{3}$/.test(g)) && m[2].startsWith(sep);
  };
  const dot = t.match(/^([^.]*)\.(\d*)$/);
  if (dot && grouped(dot[1], ",")) t = `${dot[1].replace(/,/g, "")}.${dot[2]}`;
  else if (!t.includes(".") && grouped(t, ",")) t = t.replace(/,/g, "");
  else {
    const comma = t.match(/^([^,]*),(\d+)$/);
    if (comma && (!comma[1].includes(".") || grouped(comma[1], "."))) t = `${comma[1].replace(/\./g, "")}.${comma[2]}`;
  }
  if (!/^[-+]?(\d+\.?\d*|\d*\.\d+)([eE][-+]?\d+)?$/.test(t)) return null;
  const v = Number(t);
  return percent ? v / 100 : v;
}

export function colName(n) { // 1 -> A
  let s = "";
  while (n > 0) {
    const r = (n - 1) % 26;
    s = String.fromCharCode(65 + r) + s;
    n = (n - 1 - r) / 26;
  }
  return s;
}

export function colIndex(letters) { // A -> 1
  let n = 0;
  for (const ch of letters.toUpperCase()) n = n * 26 + (ch.charCodeAt(0) - 64);
  return n;
}

function tokenize(src) {
  const tokens = [];
  let i = 0;
  const re = {
    range: /^\$?[A-Za-z]+\$?\d+:\$?[A-Za-z]+\$?\d+/, ref: /^\$?[A-Za-z]{1,3}\$?\d+/, name: /^[A-Za-z][\w]*/,
    num: /^(\d*\.?\d+)([eE][-+]?\d+)?/,
  };
  while (i < src.length) {
    const rest = src.slice(i);
    let m;
    if (/^\s/.test(rest)) { i += 1; continue; }
    if ((m = rest.match(re.range))) tokens.push(["range", m[0].replace(/\$/g, "")]);
    else if ((m = rest.match(re.ref)) && !/^[(\w]/.test(rest.slice(m[0].length))) tokens.push(["ref", m[0].replace(/\$/g, "")]);
    else if ((m = rest.match(re.name))) tokens.push(["name", m[0].toUpperCase()]);
    else if ((m = rest.match(re.num))) tokens.push(["num", Number(m[0])]);
    else if ("+-*/^(),;".includes(rest[0])) { m = [rest[0]]; tokens.push(["op", rest[0] === ";" ? "," : rest[0]]); }
    else throw new Error("#ERROR");
    i += m[0].length;
  }
  return tokens;
}

const FUNCS = {
  SUM: (v) => v.reduce((a, b) => a + b, 0),
  AVERAGE: (v) => { if (!v.length) throw new Error("#DIV/0"); return v.reduce((a, b) => a + b, 0) / v.length; },
  MIN: (v) => (v.length ? Math.min(...v) : 0),
  MAX: (v) => (v.length ? Math.max(...v) : 0),
  COUNT: (v) => v.length,
  ROUND: (v) => { const f = 10 ** (v[1] ?? 0); const x = v[0] ?? 0; return Math.sign(x) * Math.floor(Math.abs(x) * f + 0.5) / f; },
  ABS: (v) => Math.abs(v[0] ?? 0),
};
FUNCS.AVG = FUNCS.AVERAGE;
FUNCS.MEAN = FUNCS.AVERAGE;
export const FUNCTIONS = ["SUM", "AVERAGE", "MIN", "MAX", "COUNT", "ROUND", "ABS"];

// Values of a sheet's cells (0-based r, c), formulas worked out; errors are "#…" strings.
export class SheetValues {
  constructor(rows) {
    this.rows = rows;
    this.cache = new Map();
    this.busy = new Set();
  }

  raw(r, c) { return this.rows[r]?.[c]?.value ?? ""; }

  value(r, c) {
    const key = `${r}:${c}`;
    if (this.cache.has(key)) return this.cache.get(key);
    const raw = this.raw(r, c);
    let v;
    if (raw.length > 1 && raw[0] === "=") {
      if (this.busy.has(key)) return "#CYCLE";
      this.busy.add(key);
      try { v = this.evaluate(raw.slice(1)); } catch (e) { v = /^#/.test(e.message) ? e.message : "#ERROR"; }
      this.busy.delete(key);
    } else {
      v = toNumber(raw) ?? raw;
    }
    this.cache.set(key, v);
    return v;
  }

  display(r, c) {
    const raw = this.raw(r, c);
    return raw.length > 1 && raw[0] === "=" ? formatNumber(this.value(r, c)) : raw;
  }

  evaluate(src) {
    const tokens = tokenize(src);
    let pos = 0;
    const peek = () => tokens[pos];
    const take = () => tokens[pos++];
    const isOp = (t, op) => t && t[0] === "op" && t[1] === op;
    const numeric = (v) => {
      if (typeof v === "number") return v;
      if (typeof v === "string" && v.startsWith("#")) throw new Error(v);
      const n = toNumber(v);
      if (n !== null) return n;
      if (v === "" || v == null) return 0;
      throw new Error("#VALUE!");
    };
    const cell = (ref) => {
      const m = ref.match(/^([A-Za-z]+)(\d+)$/);
      return [Number(m[2]) - 1, colIndex(m[1]) - 1];
    };
    const args = () => {
      const list = [];
      if (isOp(peek(), ")")) { take(); return list; }
      for (;;) {
        const tk = peek();
        if (tk && tk[0] === "range") {
          take();
          const [a, b] = tk[1].split(":");
          const [r1, c1] = cell(a);
          const [r2, c2] = cell(b);
          for (let r = Math.min(r1, r2); r <= Math.max(r1, r2); r++) {
            for (let c = Math.min(c1, c2); c <= Math.max(c1, c2); c++) {
              const v = this.value(r, c);
              if (typeof v === "string" && v.startsWith("#")) throw new Error(v);
              if (typeof v === "number") list.push(v);
            }
          }
        } else list.push(numeric(expr()));
        const sep = take();
        if (isOp(sep, ")")) return list;
        if (!isOp(sep, ",")) throw new Error("#ERROR");
      }
    };
    const primary = () => {
      const tk = take();
      if (!tk) throw new Error("#ERROR");
      if (tk[0] === "num") return tk[1];
      if (tk[0] === "ref") return numeric(this.value(...cell(tk[1])));
      if (isOp(tk, "(")) {
        const v = expr();
        if (!isOp(take(), ")")) throw new Error("#ERROR");
        return v;
      }
      if (isOp(tk, "-")) return -numeric(primary());
      if (isOp(tk, "+")) return numeric(primary());
      if (tk[0] === "name") {
        if (!isOp(take(), "(")) throw new Error("#NAME?");
        const fn = FUNCS[tk[1]];
        const list = args();
        if (!fn) throw new Error("#NAME?");
        return fn(list);
      }
      throw new Error("#ERROR");
    };
    const power = () => {
      let v = primary();
      while (isOp(peek(), "^")) { take(); v = numeric(v) ** numeric(primary()); }
      return v;
    };
    const term = () => {
      let v = power();
      while (isOp(peek(), "*") || isOp(peek(), "/")) {
        const op = take()[1];
        const rhs = numeric(power());
        if (op === "*") v = numeric(v) * rhs;
        else { if (rhs === 0) throw new Error("#DIV/0"); v = numeric(v) / rhs; }
      }
      return v;
    };
    const expr = () => {
      let v = term();
      while (isOp(peek(), "+") || isOp(peek(), "-")) {
        const op = take()[1];
        const rhs = numeric(term());
        v = op === "+" ? numeric(v) + rhs : numeric(v) - rhs;
      }
      return v;
    };
    const v = expr();
    if (pos < tokens.length) throw new Error("#ERROR");
    return v;
  }
}

export function formatNumber(v) {
  if (typeof v !== "number") return String(v);
  if (!Number.isFinite(v)) return "#NUM!";
  if (Number.isInteger(v)) return String(v);
  return String(Math.round(v * 100) / 100);
}

// Pasted cells: tab-separated from spreadsheet programs, else the text's own separator.
export function parseClipboard(text) {
  const clean = text.replace(/\r\n?/g, "\n").replace(/\n$/, "");
  const sep = clean.includes("\t") ? "\t" : separatorFor(clean);
  return parseSheet(clean, sep).map((row) => row.map((cell) => cell.value));
}

// Cells as tab-separated text for the clipboard.
export function toClipboard(values) {
  return values.map((row) => row.map((v) => (/[\t\n"]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v)).join("\t")).join("\n");
}

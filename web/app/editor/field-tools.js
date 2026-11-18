// Text helpers for front matter fields (ARCHITECTURE.md §9.4): which keys the front matter
// has, whether they are filled in, and the change that adds one. Plain functions on strings,
// so they run (and are tested) without a DOM.
import { frontMatter } from "./cite-tools.js";

const KEY_LINE = /^([A-Za-z_][\w-]*)[ \t]*:(.*)$/;
const NESTED_LINE = /^[ \t]+(?:-[ \t]+)?([A-Za-z_][\w-]*)[ \t]*:(.*)$/;
const CURSOR = "${}";

// The text of a value without YAML's quotes, brackets, block markers, keys and comments,
// so an empty "" or [] or a list of empty entries counts as not filled in.
function content(value) {
  return value
    .replace(/(^|\s)#.*$/gm, "")
    .replace(/[A-Za-z_][\w-]*[ \t]*:/g, "")
    .replace(/["'[\]{}|>,\-\s]/g, "");
}

// The front matter's keys: the top-level ones, and the ones inside a top-level key
// (author.affiliation), each with where its line is and whether it has a value.
export function frontMatterKeys(text) {
  const fm = frontMatter(text);
  const top = new Map();
  const nested = new Map();
  if (!fm) return { fm, top, nested };
  let offset = fm.bodyFrom;
  let current = null;
  for (const raw of fm.body.split("\n")) {
    const line = raw.replace(/\r$/, "");
    const m = KEY_LINE.exec(line);
    if (m) {
      current = top.has(m[1]) ? null : { key: m[1], from: offset, to: offset + line.length, end: offset + line.length,
                                         value: m[2] };
      if (current) top.set(m[1], current);
    } else if (current && /^[ \t]/.test(line)) {
      current.value += `\n${line}`;
      if (line.trim()) current.end = offset + line.length;
      const n = NESTED_LINE.exec(line);
      const name = n && `${current.key}.${n[1]}`;
      if (n && !nested.has(name)) nested.set(name, { key: name, from: offset, to: offset + line.length, value: n[2] });
      else if (n && content(n[2]) && !content(nested.get(name).value)) nested.get(name).value = n[2];
    } else if (line.trim() && !line.trimStart().startsWith("#")) {
      current = null;
    }
    offset += raw.length + 1;
  }
  return { fm, top, nested };
}

// Where a field stands in the document: "missing", "empty" or "filled", and the position
// to go to. Fields written per author (affiliation) are looked up inside author:.
export function fieldState(keys, field) {
  const spots = [];
  if (field.in) {
    for (const [name, spot] of keys.nested) {
      if (name.startsWith(`${field.in}.`) && name.slice(field.in.length + 1) === field.key) spots.push(spot);
    }
  }
  if (!field.in || field.topLevel) {
    const spot = keys.top.get(field.key);
    if (spot) spots.push(spot);
  }
  if (!spots.length) {
    // Nothing yet, but the author entry it belongs in is there: go there.
    const parent = field.in && !field.topLevel ? keys.top.get(field.in) : null;
    return { state: "missing", at: parent ? parent.to : null, parent: Boolean(parent) };
  }
  const filled = spots.find((s) => content(s.value));
  const spot = filled ?? spots[0];
  return { state: filled ? "filled" : "empty", at: spot.to };
}

// A field's snippet with today's date filled in, and where in it the cursor goes.
export function snippetOf(field, today = "") {
  let text = field.snippet.replaceAll("{today}", today);
  if (!text.includes(CURSOR)) text += CURSOR;
  return { text: text.replace(CURSOR, ""), cursor: text.indexOf(CURSOR) };
}

// The change that adds *field* to the front matter, and where the cursor goes. It goes
// after the closest field before it in *order* (the layout's order), else at the end;
// without front matter, a new block starts the document. Null when the field can't be
// added on its own (affiliation, once there is an author).
export function insertField(text, field, { order = [], today = "" } = {}) {
  const keys = frontMatterKeys(text);
  if (field.in && !field.topLevel && keys.top.has(field.in)) return null;
  const { text: snippet, cursor: cursorAt } = snippetOf(field, today);
  if (!keys.fm) {
    return { changes: { from: 0, to: 0, insert: `---\n${snippet}\n---\n\n` }, cursor: 4 + cursorAt };
  }
  let at = keys.fm.bodyTo;
  const index = order.indexOf(field.key);
  for (let i = index - 1; i >= 0; i--) {
    const before = keys.top.get(order[i] === "affiliation" ? "author" : order[i]);
    if (before) {
      at = before.end;
      break;
    }
  }
  if (at === keys.fm.bodyTo && !keys.fm.body.trim()) {
    const { bodyFrom, bodyTo } = keys.fm;
    return { changes: { from: bodyFrom, to: bodyTo, insert: snippet }, cursor: bodyFrom + cursorAt };
  }
  return { changes: { from: at, to: at, insert: `\n${snippet}` }, cursor: at + 1 + cursorAt };
}

// The completion for a key typed at the start of a front matter line: what is typed so far,
// or null outside the front matter.
export function keyBeingTyped(text, pos) {
  const fm = frontMatter(text);
  if (!fm || pos < fm.bodyFrom || pos > fm.bodyTo) return null;
  const lineStart = text.lastIndexOf("\n", pos - 1) + 1;
  const before = text.slice(lineStart, pos);
  if (!/^[A-Za-z_]?[\w-]*$/.test(before)) return null;
  return { from: lineStart, typed: before };
}

// The key under *pos* in the front matter, for hover help: top-level or inside author:.
export function keyAt(text, pos) {
  const fm = frontMatter(text);
  if (!fm || pos < fm.bodyFrom || pos > fm.bodyTo) return null;
  const lineStart = text.lastIndexOf("\n", pos - 1) + 1;
  const lineEnd = text.indexOf("\n", pos);
  const line = text.slice(lineStart, lineEnd < 0 ? text.length : lineEnd);
  const m = /^([ \t]*(?:-[ \t]+)?)([A-Za-z_][\w-]*)[ \t]*:/.exec(line);
  if (!m) return null;
  const from = lineStart + m[1].length;
  const to = from + m[2].length;
  if (pos < from || pos > to) return null;
  return { key: m[2], from, to, nested: m[1].length > 0 };
}

// Reading and writing single front matter fields (ARCHITECTURE.md §9.4), for the title card in
// the editor: a field's value without YAML's quotes, lists and block markers, and the change that
// sets it again. Plain functions on strings, so they run (and are tested) without a DOM.
import { frontMatterKeys } from "./field-tools.js";

// Where a new field goes: title, subtitle, author and date stay in this order at the top.
const ORDER = ["title", "subtitle", "author", "date"];
const ISO_DATE = /^\d{4}(-\d{2}(-\d{2})?)?$/;

function unquote(raw) {
  const v = raw.trim();
  if (v.startsWith('"') && v.endsWith('"') && v.length >= 2) {
    try { return JSON.parse(v.replace(/\\'/g, "'").replace(/\t/g, "\\t")); } catch { return v.slice(1, -1); }
  }
  if (v.startsWith("'") && v.endsWith("'") && v.length >= 2) return v.slice(1, -1).replace(/''/g, "'");
  return v.replace(/\s+#.*$/, "");
}

// Items of a flow list ([a, "b, c"]), split at commas outside quotes.
function flowItems(inner) {
  const out = [];
  let cur = "";
  let quote = null;
  for (const ch of inner) {
    if (quote) {
      cur += ch;
      if (ch === quote) quote = null;
    } else if (ch === '"' || ch === "'") {
      quote = ch;
      cur += ch;
    } else if (ch === ",") {
      out.push(cur);
      cur = "";
    } else if (ch === "[" || ch === "{") {
      return null;
    } else {
      cur += ch;
    }
  }
  out.push(cur);
  return out.map(unquote).filter((s) => s !== "");
}

// A field's value: { kind: "text" | "list" | "block" | "complex", value }.
// "complex" values (lists of mappings, nested keys) are only for the YAML view.
export function readValue(raw) {
  const [first, ...rest] = raw.split("\n");
  const head = first.trim().replace(/^#.*$/, "");
  const lines = rest.filter((l) => l.trim() && !/^\s*#/.test(l));
  if (/^[|>][+-]?\d*\s*(#.*)?$/.test(head)) {
    const indent = Math.min(...rest.filter((l) => l.trim()).map((l) => l.match(/^\s*/)[0].length));
    const body = rest.map((l) => l.slice(Number.isFinite(indent) ? indent : 0)).join("\n").replace(/\s+$/, "");
    const value = head.startsWith(">") ? body.replace(/([^\n])\n(?=[^\n])/g, "$1 ") : body;
    return { kind: "block", value };
  }
  if (!head) {
    if (!lines.length) return { kind: "text", value: "" };
    if (lines.every((l) => /^\s*-\s+/.test(l)) && !lines.some((l) => /^\s*-\s+[\w-]+\s*:(\s|$)/.test(l))) {
      return { kind: "list", value: lines.map((l) => unquote(l.replace(/^\s*-\s+/, ""))) };
    }
    return { kind: "complex", value: "" };
  }
  if (head.startsWith("[") && head.endsWith("]") && !lines.length) {
    const items = flowItems(head.slice(1, -1));
    return items ? { kind: "list", value: items } : { kind: "complex", value: "" };
  }
  if (head.startsWith("{") || head.startsWith("[") || head.startsWith("&") || head.startsWith("*")) {
    return { kind: "complex", value: "" };
  }
  if (lines.length) return { kind: "text", value: [head, ...lines.map((l) => l.trim())].map(unquote).join(" ") };
  return { kind: "text", value: unquote(head) };
}

// The front matter's fields, in order: { has, fields: [{ key, kind, value }] }.
export function readFields(text) {
  const keys = frontMatterKeys(text);
  if (!keys.fm) return { has: false, fields: [] };
  return { has: true, fields: [...keys.top.values()].map((e) => ({ key: e.key, ...readValue(e.value) })) };
}

function quoted(value) {
  return JSON.stringify(value);
}

// The YAML for a field. Lists of names take one line per name; text with line breaks a block.
export function writeField(key, value, { date = false } = {}) {
  if (Array.isArray(value)) {
    const items = value.map((v) => v.trim()).filter(Boolean);
    if (key !== "author") return `${key}: [${items.map((v) => (/^[\p{L}\p{N}][\p{L}\p{N} .\-]*$/u.test(v) ? v : quoted(v))).join(", ")}]`;
    if (items.length <= 1) return `${key}: ${quoted(items[0] ?? "")}`;
    return `${key}:\n${items.map((v) => `  - ${quoted(v)}`).join("\n")}`;
  }
  const text = String(value ?? "");
  if (text.includes("\n")) {
    return `${key}: |\n${text.replace(/\s+$/, "").split("\n").map((l) => (l.trim() ? `  ${l}` : "")).join("\n")}`;
  }
  if (date && ISO_DATE.test(text.trim())) return `${key}: ${text.trim()}`;
  return `${key}: ${quoted(text)}`;
}

// The change that sets a field: { from, to, insert } on the document's text. An empty value
// removes the field, except the title, which keeps its place. Without a front matter, one is made.
export function setField(text, key, value, { keep = ["title"] } = {}) {
  const keys = frontMatterKeys(text);
  const empty = Array.isArray(value) ? !value.some((v) => v.trim()) : !String(value ?? "").trim();
  const line = writeField(key, value, { date: key === "date" });
  if (!keys.fm) {
    if (empty) return null;
    return { from: 0, to: 0, insert: `---\n${line}\n---\n\n` };
  }
  const entry = keys.top.get(key);
  if (entry) {
    if (empty && !keep.includes(key)) {
      const to = Math.min(text.length, entry.end + 1);
      return { from: entry.from, to, insert: "" };
    }
    const current = text.slice(entry.from, entry.end);
    return current === line ? null : { from: entry.from, to: entry.end, insert: line };
  }
  if (empty && !keep.includes(key)) return null;
  // After the field before it in ORDER, or at the top for title-like fields, or at the end.
  const at = ORDER.indexOf(key);
  let pos = null;
  if (at >= 0) {
    for (let i = at - 1; i >= 0 && pos === null; i--) {
      const prev = keys.top.get(ORDER[i]);
      if (prev) pos = Math.min(text.length, prev.end + 1);
    }
    if (pos === null) pos = keys.fm.bodyFrom;
  } else {
    pos = keys.fm.bodyTo + 1;
  }
  if (keys.fm.bodyTo <= keys.fm.bodyFrom && !keys.fm.body) pos = keys.fm.bodyFrom;
  return { from: pos, to: pos, insert: `${line}\n` };
}

// Several authors are written "Jane Doe; John Roe" in the card.
export function authorsOf(field) {
  if (!field) return "";
  return field.kind === "list" ? field.value.join("; ") : field.value;
}

export function authorValue(text) {
  const names = text.split(";").map((s) => s.trim()).filter(Boolean);
  return names.length > 1 ? names : (names[0] ?? "");
}

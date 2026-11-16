// Text helpers for citations (ARCHITECTURE.md §10.4): the front matter's bibliography,
// the citation at the cursor, inserting keys and locators, and finding a quoted passage
// in a PDF's text. Plain functions on strings, so they run (and are tested) without a DOM.

const KEY = String.raw`[\p{L}\p{N}_](?:[\p{L}\p{N}_:.#$%&\-+?<>~/]*[\p{L}\p{N}_])?`;
const CITE_KEY = new RegExp(String.raw`(-?)@(?:\{([^{}]+)\}|(${KEY}))`, "u");
const FRONT = /^---[ \t]*\r?\n([\s\S]*?)\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|$)/;

// -- Front matter ------------------------------------------------------------

export function frontMatter(text) {
  const m = FRONT.exec(text);
  if (!m) return null;
  const bodyFrom = m[0].indexOf("\n") + 1;
  return { from: 0, to: m[0].length, body: m[1], bodyFrom, bodyTo: bodyFrom + m[1].length };
}

function unquote(value) {
  const v = value.trim();
  if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) return v.slice(1, -1);
  return v;
}

function yamlScalar(value) {
  return /^[\w./@~-][^#]*$/.test(value) && !/:\s/.test(value) && !/\s$/.test(value) ? value : JSON.stringify(value);
}

// The bibliography key of the front matter: where it is and what it lists.
function bibliographyField(text) {
  const fm = frontMatter(text);
  if (!fm) return { fm: null };
  const lines = fm.body.split("\n");
  let offset = fm.bodyFrom;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const m = /^bibliography:[ \t]*(.*?)[ \t]*$/.exec(line.replace(/\r$/, ""));
    if (m) {
      const valueFrom = offset + line.indexOf(":") + 1;
      const lineEnd = offset + line.length;
      if (m[1].startsWith("[")) {
        const close = text.indexOf("]", valueFrom);
        const inner = text.slice(text.indexOf("[", valueFrom) + 1, close);
        const entries = inner.split(",").map(unquote).filter(Boolean);
        return { fm, kind: "flow", entries, close };
      }
      if (m[1] && !m[1].startsWith("#")) {
        return { fm, kind: "scalar", entries: [unquote(m[1])], valueFrom, lineEnd };
      }
      // A block list: the indented "- entry" lines that follow.
      const entries = [];
      let end = lineEnd;
      let indent = "  ";
      let pos = lineEnd + 1;
      for (let j = i + 1; j < lines.length; j++) {
        const item = /^(\s*)-\s+(.*?)\s*$/.exec(lines[j].replace(/\r$/, ""));
        if (!item) break;
        indent = item[1];
        entries.push(unquote(item[2]));
        end = pos + lines[j].length;
        pos += lines[j].length + 1;
      }
      return { fm, kind: "block", entries, end, indent };
    }
    offset += line.length + 1;
  }
  return { fm, kind: "none", entries: [] };
}

export function bibliographyEntries(text) {
  return bibliographyField(text).entries ?? [];
}

export function normalizeEntry(entry) {
  return entry.replace(/^\.\//, "");
}

// The change that adds *entry* to the bibliography, or null when it is listed already.
export function addBibliography(text, entry) {
  const field = bibliographyField(text);
  if (field.entries?.some((e) => normalizeEntry(e) === normalizeEntry(entry))) return null;
  const value = yamlScalar(entry);
  if (!field.fm) return { from: 0, to: 0, insert: `---\nbibliography: ${value}\n---\n\n` };
  switch (field.kind) {
    case "flow": {
      const before = text.slice(0, field.close).trimEnd();
      const sep = before.endsWith("[") ? "" : ", ";
      return { from: field.close, to: field.close, insert: `${sep}${value}` };
    }
    case "scalar":
      return { from: field.valueFrom, to: field.lineEnd,
               insert: `\n  - ${yamlScalar(field.entries[0])}\n  - ${value}` };
    case "block":
      return { from: field.end, to: field.end, insert: `\n${field.indent || "  "}- ${value}` };
    default:
      return { from: field.fm.bodyTo, to: field.fm.bodyTo, insert: `\nbibliography: ${value}` };
  }
}

export function documentLang(text) {
  const fm = frontMatter(text);
  const m = fm && /^lang:[ \t]*["']?([A-Za-z-]+)/m.exec(fm.body);
  return m ? m[1] : "";
}

// -- Citations ---------------------------------------------------------------

function parseCites(inner, innerFrom) {
  const cites = [];
  let start = 0;
  const parts = inner.split(";");
  for (const part of parts) {
    const m = CITE_KEY.exec(part);
    if (m) {
      const at = m.index;
      const key = m[2] ?? m[3];
      const keyTo = at + m[0].length;
      const lead = part.length - part.trimStart().length;
      const trail = part.length - part.trimEnd().length;
      cites.push({
        key, suppress: m[1] === "-",
        from: innerFrom + start + lead, to: innerFrom + start + part.length - trail,
        keyFrom: innerFrom + start + at, keyTo: innerFrom + start + keyTo,
        prefix: part.slice(0, at).trim(), suffix: part.slice(keyTo).trimEnd(),
      });
    }
    start += part.length + 1;
  }
  return cites;
}

// The bracketed citation around *pos*: [see @doe2020, p. 4; @roe2019].
export function citationAt(text, pos) {
  const lo = Math.max(0, pos - 2000);
  const hi = Math.min(text.length, pos + 2000);
  const slice = text.slice(lo, hi);
  const re = /\[(?!\^)([^[\]]*?@[^[\]]*?)\](?!\()/g;
  let m;
  while ((m = re.exec(slice))) {
    const from = lo + m.index;
    const to = from + m[0].length;
    if (from > pos) break;
    if (pos >= from && pos <= to) {
      const cites = parseCites(m[1], from + 1);
      if (cites.length) return { from, to, cites };
    }
  }
  return null;
}

// Every cited key in the text, in order of first use.
export function citedKeys(text) {
  const out = new Set();
  const re = new RegExp(String.raw`(?:^|[\s[;(-])-?@(?:\{([^{}]+)\}|(${KEY}))`, "gmu");
  let m;
  while ((m = re.exec(text))) out.add(m[1] ?? m[2]);
  return [...out];
}

const LOCATOR = /^,\s*((?:[\p{L}]+\.?\s*)?[\dIVXLCivxlc]+(?:\s*[-–]\s*[\dIVXLCivxlc]+)?)/u;

export function locatorOf(cite) {
  const m = LOCATOR.exec(cite.suffix);
  return m ? m[1] : "";
}

// The change that sets the locator of *cite* (empty: removes it), keeping the rest of the suffix.
export function setLocator(cite, locator) {
  const m = LOCATOR.exec(cite.suffix);
  const rest = m ? cite.suffix.slice(m[0].length) : cite.suffix;
  const loc = locator ? `, ${locator}` : "";
  return { from: cite.keyTo, to: cite.to, insert: loc + rest };
}

// Page locators in the document's language: "p. 23" and "pp. 23-25" in English, "S. 23" in
// German; a bare number elsewhere, which citeproc reads as a page in every language.
export function formatLocator(lang, from, to = "") {
  const range = to && to !== from;
  const pages = range ? `${from}-${to}` : `${from}`;
  const l = (lang || "en").toLowerCase();
  if (l.startsWith("en")) return `${range ? "pp." : "p."} ${pages}`;
  if (l.startsWith("de")) return `S. ${pages}`;
  return pages;
}

// The changes that cite *key* at the selection: into the citation around the cursor
// (setting the locator if the key is there already), or as a new [@key] after the selection.
export function citeChanges(text, selection, key, locator = "") {
  const keyText = /^[\p{L}\p{N}_](?:[\p{L}\p{N}_:.#$%&\-+?<>~/]*[\p{L}\p{N}_])?$/u.test(key) ? key : `{${key}}`;
  const around = citationAt(text, selection.from);
  if (around && selection.from > around.from && selection.from < around.to) {
    const existing = around.cites.find((c) => c.key === key);
    if (existing) {
      if (!locator) return { changes: [], cursor: existing.to };
      const change = setLocator(existing, locator);
      return { changes: [change], cursor: change.from + change.insert.length };
    }
    const insert = `; @${keyText}${locator ? `, ${locator}` : ""}`;
    return { changes: [{ from: around.to - 1, to: around.to - 1, insert }], cursor: around.to - 1 + insert.length };
  }
  const at = selection.to;
  const before = text.slice(Math.max(0, at - 1), at);
  const space = before && !/[\s([{]/.test(before) ? " " : "";
  const insert = `${space}[@${keyText}${locator ? `, ${locator}` : ""}]`;
  return { changes: [{ from: at, to: at, insert }], cursor: at + insert.length };
}

// -- Finding a quotation in a PDF --------------------------------------------

// The quotation in the paragraph just before *pos*, which a citation there usually gives.
export function quoteBefore(text, pos) {
  const start = Math.max(text.lastIndexOf("\n\n", pos - 1), pos - 3000, 0);
  const before = text.slice(start, pos);
  const re = /"([^"]{12,800})"|“([^”“]{12,800})[”“]|„([^“”]{12,800})[“”]|»([^«»]{12,800})«|«([^«»]{12,800})»|‚([^‘’]{12,800})‘/g;
  let last = null;
  let m;
  while ((m = re.exec(before))) last = m;
  if (!last) return "";
  const after = before.slice(last.index + last[0].length);
  if (/[.!?]\s+\S/.test(after.replace(/\[[^\]]*\]/g, ""))) return ""; // a new sentence began since
  return (last.slice(1).find(Boolean) ?? "").replace(/\s+/g, " ").replace(/\[…\]|\[\.\.\.\]|…/g, " ").trim();
}

// Lower case, no accents or ligatures, words joined across line-end hyphens, one space.
export function normalizeText(s) {
  return s.normalize("NFKD").replace(/\p{M}/gu, "").toLowerCase()
    .replace(/[‐‑‒–—―]/g, "-")
    .replace(/[“”„‟«»"‘’‚‛']/g, "")
    .replace(/(\p{L})-\s+(\p{Ll})/gu, "$1$2")
    .replace(/\s+/g, " ")
    .trim();
}

function search(pages, needle, max) {
  const hits = [];
  for (let i = 0; i < pages.length && hits.length < max; i++) {
    // A passage may run on to the next page.
    const text = pages[i] + " " + (pages[i + 1] ?? "").slice(0, needle.length + 20);
    let at = text.indexOf(needle);
    while (at !== -1 && at < pages[i].length && hits.length < max) {
      hits.push({ page: i + 1, index: at, length: needle.length,
                  snippet: text.slice(Math.max(0, at - 60), at + needle.length + 60) });
      at = text.indexOf(needle, at + 1);
    }
  }
  return hits;
}

// Where *query* occurs in the pages' text (already normalized). Long quotations that
// don't match whole (an ellipsis, a changed word) are tried by their first and last words.
export function findInPages(pages, query, max = 50) {
  const q = normalizeText(query);
  if (q.length < 2) return [];
  const hits = search(pages, q, max);
  if (hits.length) return hits;
  const words = q.split(" ");
  if (words.length < 8) return [];
  const head = search(pages, words.slice(0, 6).join(" "), max);
  if (head.length) return head.map((h) => ({ ...h, partial: true }));
  return search(pages, words.slice(-6).join(" "), max).map((h) => ({ ...h, partial: true }));
}

// The first printed page of a CSL page range such as "123-145", or null.
export function firstPage(range) {
  const m = /^\s*(\d+)\s*(?:[-–]\s*(\d+))?/.exec(range ?? "");
  if (!m) return null;
  return { first: Number(m[1]), last: m[2] ? Number(m[2]) : null };
}

// How a PDF's page index maps to printed page numbers: the PDF's own page labels when it has
// real ones, else the item's page range (an article on pp. 123-145 starts at 123), else none.
export function pageOffset(labels, numPages, range) {
  if (Array.isArray(labels) && labels.some((l, i) => l && l !== String(i + 1))) return null;
  const pr = firstPage(range);
  if (!pr || pr.first <= 1) return null;
  const length = pr.last && pr.last >= pr.first ? pr.last - pr.first + 1 : null;
  const extra = length ? numPages - length : 0;
  // A cover sheet or two in front (as JSTOR adds them) shifts the start.
  return pr.first - 1 - (extra > 0 && extra <= 3 ? extra : 0);
}

export function pageLabel(index, labels, offset) {
  if (offset !== null && offset !== undefined && offset !== "") return String(index + Number(offset));
  const own = labels?.[index - 1];
  return own || String(index);
}

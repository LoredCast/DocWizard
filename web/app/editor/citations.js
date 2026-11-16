// Citation library, autocomplete and chip labels (ARCHITECTURE.md §10.4).
//
// The library holds the items of every bibliography in the project: its .bib, .json and
// .ris files and the zotero:<name> libraries its documents use. Autocomplete ranks the
// items of the document's own bibliography first, offers the project's other items and
// your other Zotero libraries after them, and adds an item's file or library to the
// front matter when you pick it, so pandoc finds it.
import { StateEffect, StateField } from "codemirror";
import { t } from "../ui/dom.js";
import { addBibliography, bibliographyEntries, citedKeys, normalizeEntry } from "./cite-tools.js";

export const setLibrary = StateEffect.define();

export const libraryField = StateField.define({
  create: () => new Map(),
  update(value, tr) {
    for (const e of tr.effects) if (e.is(setLibrary)) return e.value;
    return value;
  },
});

// Set by the editor page: search your Zotero libraries, and hear about new citations.
const hooks = { remoteSearch: null, afterCite: null };
export function setCiteHooks(next) {
  Object.assign(hooks, next);
}

export async function loadLibrary(view, projectId) {
  try {
    const res = await fetch(`/api/projects/${projectId}/citations`, { credentials: "same-origin" });
    if (!res.ok) return;
    const data = await res.json();
    const map = new Map(data.items.map((item) => [item.id, item]));
    view.dispatch({ effects: setLibrary.of(map) });
  } catch {
    /* offline: keep the current library */
  }
}

export function fold(s) {
  return s.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();
}

// Fuzzy score: every query character in order; earlier and contiguous matches score higher.
function fuzzy(query, text) {
  let qi = 0, last = -1, total = 0;
  for (let i = 0; i < text.length && qi < query.length; i++) {
    if (text[i] === query[qi]) {
      total += last === i - 1 ? 3 : 1;
      if (i === 0 || /\W/.test(text[i - 1])) total += 2;
      last = i;
      qi++;
    }
  }
  return qi === query.length ? total : -1;
}

// Keys and surnames that start with the query beat words that contain it, which beat
// fuzzy matches; items already cited in the document come a little earlier.
export function scoreItem(query, item) {
  if (!query) return 1;
  const key = fold(item.id);
  const names = item.authors.map(fold);
  if (key.startsWith(query)) return 100 + (key === query ? 20 : 0);
  if (names.some((n) => n.startsWith(query))) return 90;
  const words = fold(`${item.title} ${item.container ?? ""}`).split(/[^\p{L}\p{N}]+/u);
  if (words.some((w) => w.startsWith(query))) return 70;
  if (item.year && item.year.startsWith(query)) return 65;
  const hay = fold(`${item.id} ${item.authors.join(" ")} ${item.year} ${item.title}`);
  if (hay.includes(query)) return 50;
  const s = fuzzy(query, hay);
  return s < 0 ? -1 : Math.min(45, s);
}

export function sourceName(source) {
  if (!source) return "";
  return source.startsWith("zotero:") ? `Zotero: ${source.slice(7)}` : source;
}

// Which bibliography a document already lists; an item from elsewhere gets added on use.
export function inBibliography(entries, item) {
  return entries.includes(normalizeEntry(item.source ?? ""));
}

function infoNode(item, listed) {
  const node = document.createElement("div");
  node.className = "cite-info";
  const head = document.createElement("strong");
  head.textContent = item.label;
  const title = document.createElement("div");
  title.textContent = [item.title, item.container].filter(Boolean).join(". ");
  const where = document.createElement("div");
  where.className = "muted";
  where.textContent = listed ? sourceName(item.source)
    : t("{source} · picking it adds this to the document's bibliography", { source: sourceName(item.source) });
  node.append(head, title, where);
  return node;
}

// The changes that cite an item: its key at the completion, and its bibliography in the
// front matter when the document doesn't list it yet.
export function applyCitation(view, item, from, to, insertKey = item.id) {
  const text = view.state.doc.toString();
  const changes = [{ from, to, insert: insertKey }];
  let cursor = from + insertKey.length;
  const bib = item.source ? addBibliography(text, item.source) : null;
  if (bib) {
    changes.push(bib);
    if (bib.from <= from) cursor += bib.insert.length - (bib.to - bib.from);
  }
  view.dispatch({ changes, selection: { anchor: cursor }, userEvent: "input.complete", scrollIntoView: true });
  if (bib || !view.state.field(libraryField, false)?.has(item.id)) hooks.afterCite?.(item);
}

const remoteCache = new Map();
async function remote(query) {
  if (!hooks.remoteSearch || query.length < 2) return [];
  if (!remoteCache.has(query)) {
    remoteCache.set(query, hooks.remoteSearch(query).catch(() => []));
    if (remoteCache.size > 50) remoteCache.delete(remoteCache.keys().next().value);
  }
  return remoteCache.get(query);
}

export async function citationCompletions(context) {
  const word = context.matchBefore(/(?:^|[\s[;(-])@[\p{L}\p{N}_:.#$%&\-+?<>~/]*$/u);
  if (!word) return null;
  const at = word.text.indexOf("@");
  const from = word.from + at + 1;
  const query = fold(word.text.slice(at + 1));
  const { state } = context;
  const library = state.field(libraryField, false) ?? new Map();
  const head = state.sliceDoc(0, Math.min(state.doc.length, 20000));
  const entries = bibliographyEntries(head).map(normalizeEntry);
  const cited = new Set(citedKeys(state.sliceDoc(0, Math.min(state.doc.length, 400000))));

  const scored = [];
  for (const item of library.values()) {
    const s = scoreItem(query, item);
    if (s < 0) continue;
    const listed = inBibliography(entries, item);
    scored.push({ item, listed, group: listed ? 0 : 1, s: s + (cited.has(item.id) ? 8 : 0) });
  }
  if (query.length >= 2 && hooks.remoteSearch) {
    // Your other Zotero libraries, after a pause in typing.
    await new Promise((r) => setTimeout(r, 120));
    if (context.aborted) return null;
    for (const item of await remote(query)) {
      if (library.has(item.id)) continue;
      const s = scoreItem(query, item);
      if (s >= 0) scored.push({ item, listed: false, group: 2, s });
    }
    if (context.aborted) return null;
  }
  if (!scored.length) return null;
  scored.sort((a, b) => a.group - b.group || b.s - a.s || a.item.id.localeCompare(b.item.id));
  const sections = [
    { name: t("In this document's bibliography"), rank: 0 },
    { name: t("Elsewhere in this project"), rank: 1 },
    { name: t("Your Zotero libraries"), rank: 2 },
  ];
  const options = scored.slice(0, 60).map(({ item, listed, group, s }) => ({
    label: item.id,
    detail: listed ? item.label : `${item.label} · ${sourceName(item.source)}`,
    info: () => infoNode(item, listed),
    type: "text",
    section: sections[group],
    boost: Math.max(-99, Math.min(99, s - 60)),
    apply: (view, _completion, f, t) => applyCitation(view, item, f, t),
  }));
  return { from, options, filter: false };
}

// "[see @doe2020, p. 4; @smith2019]" -> "see Doe 2020, p. 4; Smith and Lee 2019"
export function chipText(source, library) {
  let inner = source;
  const bracketed = inner.startsWith("[") && inner.endsWith("]");
  if (bracketed) inner = inner.slice(1, -1);
  const text = inner.replace(/(-?)@([\p{L}\p{N}_][\p{L}\p{N}_:.#$%&\-+?<>~/]*[\p{L}\p{N}_])|(-?)@([\p{L}\p{N}_])/gu,
    (m, dash1, key1, dash2, key2) => {
      const key = key1 ?? key2;
      const item = library.get(key);
      if (!item) return key;
      const suppress = (dash1 ?? dash2) === "-";
      return suppress ? item.year || item.label : item.label;
    });
  return text.trim();
}

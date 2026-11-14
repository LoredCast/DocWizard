// Citation library, autocomplete and chip labels (ARCHITECTURE.md §10.4).
import { StateEffect, StateField } from "codemirror";

export const setLibrary = StateEffect.define();

export const libraryField = StateField.define({
  create: () => new Map(),
  update(value, tr) {
    for (const e of tr.effects) if (e.is(setLibrary)) return e.value;
    return value;
  },
});

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

function fold(s) {
  return s.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();
}

// Fuzzy score: every query character in order; earlier and contiguous matches score higher.
function score(query, text) {
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

export function citationCompletions(context) {
  const word = context.matchBefore(/(?:^|[\s[;(-])@[\p{L}\p{N}_:.#$%&\-+?<>~/]*$/u);
  if (!word) return null;
  const at = word.text.indexOf("@");
  const from = word.from + at + 1;
  const query = fold(word.text.slice(at + 1));
  const library = context.state.field(libraryField, false);
  if (!library || !library.size) return null;
  const options = [];
  for (const item of library.values()) {
    const hay = fold(`${item.id} ${item.authors.join(" ")} ${item.year} ${item.title}`);
    const s = query ? score(query, hay) : 0;
    if (s < 0) continue;
    options.push({
      label: item.id,
      detail: item.label,
      info: [item.title, item.container].filter(Boolean).join(". "),
      type: "text",
      boost: Math.min(99, s),
    });
  }
  options.sort((a, b) => b.boost - a.boost);
  return { from, options: options.slice(0, 50), filter: false };
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

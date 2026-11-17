// Grammar and spelling underlines (ARCHITECTURE.md §6.8). Harper checks English in the
// browser; a LanguageTool server, when the admin set one up, checks other languages through
// the app. A click on an underline (or Ctrl+. at the cursor) shows what's wrong and the fixes.
import { Decoration, EditorView, StateEffect, StateField, keymap, showTooltip } from "codemirror";
import { api, el, t } from "../ui/dom.js";
import { checkerFor, dialectOf } from "../workers/grammar-text.js";

const setMatches = StateEffect.define();
const openAt = StateEffect.define(); // a match's from, or null to close

const mark = (m) => Decoration.mark({
  class: m.kind === "spelling" ? "cm-grammar cm-grammar-spelling" : "cm-grammar",
  attributes: { "data-grammar": "" },
  match: m,
});

// The matches as decorations, mapped through edits; an edit inside one removes it until the
// next check.
const matchesField = StateField.define({
  create: () => Decoration.none,
  update(deco, tr) {
    for (const e of tr.effects) {
      if (e.is(setMatches)) {
        return Decoration.set(e.value.filter((m) => m.to <= tr.state.doc.length).map((m) => mark(m).range(m.from, m.to)), true);
      }
    }
    if (!tr.docChanged) return deco;
    deco = deco.map(tr.changes);
    tr.changes.iterChangedRanges((_fa, _ta, from, to) => {
      deco = deco.update({ filterFrom: from, filterTo: to, filter: (f, t2) => t2 < from || f > to });
    });
    return deco;
  },
  provide: (f) => EditorView.decorations.from(f),
});

function matchAt(state, pos) {
  let found = null;
  state.field(matchesField).between(pos, pos, (from, to, value) => {
    if (!found && pos >= from && pos <= to) found = { from, to, match: value.spec.match };
  });
  return found;
}

function card(view, hit, options) {
  const { match } = hit;
  const close = () => view.dispatch({ effects: openAt.of(null) });
  const apply = (s) => {
    const change = s.kind === "remove" ? { from: hit.from, to: hit.to, insert: "" }
      : s.kind === "insert" ? { from: hit.to, insert: s.text }
        : { from: hit.from, to: hit.to, insert: s.text };
    view.dispatch({ changes: change, effects: openAt.of(null), userEvent: "input.grammar" });
    view.focus();
  };
  const word = view.state.sliceDoc(hit.from, hit.to);
  const fixes = match.suggestions.map((s) => el("button", {
    type: "button", class: "grammar-fix", onclick: () => apply(s),
  }, s.kind === "remove" ? t("Remove “{text}”", { text: word }) : s.text || t("(nothing)")));
  const dom = el("div", { class: "grammar-card", role: "dialog", "aria-label": match.title || t("Grammar") },
    match.title ? el("p", { class: "grammar-title" }, match.title) : null,
    el("p", { class: "grammar-message" }, match.message),
    fixes.length ? el("div", { class: "grammar-fixes" }, ...fixes) : null,
    el("div", { class: "grammar-actions" },
      el("button", { type: "button", class: "link", onclick: () => { options.ignore(match, word); close(); view.focus(); } },
        t("Ignore")),
      match.kind === "spelling" && options.addWord
        ? el("button", { type: "button", class: "link", onclick: () => { options.addWord(word); close(); view.focus(); } },
          t("Add to dictionary"))
        : null));
  dom.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); close(); view.focus(); }
  });
  return dom;
}

function tooltipField(options) {
  return StateField.define({
    create: () => null,
    update(open, tr) {
      for (const e of tr.effects) {
        if (e.is(openAt)) {
          if (e.value === null) return null;
          const hit = matchAt(tr.state, e.value);
          return hit ? tooltipFor(hit, options) : null;
        }
        if (e.is(setMatches)) return null;
      }
      if (open && (tr.docChanged || (tr.selection && !matchAt(tr.state, tr.state.selection.main.head)))) return null;
      return open;
    },
    provide: (f) => showTooltip.from(f),
  });
}

function tooltipFor(hit, options) {
  return {
    pos: hit.from,
    end: hit.to,
    above: false,
    create: (view) => {
      const dom = card(view, hit, options);
      return { dom, mount: () => { if (options.focusCard) dom.querySelector("button")?.focus(); options.focusCard = false; } };
    },
  };
}

// *check(text)* resolves to the matches for that text, or null when nothing can check it now.
export function grammarExtension(options) {
  const opts = { ...options, focusCard: false };
  return [
    matchesField,
    tooltipField(opts),
    EditorView.domEventHandlers({
      click(event, view) {
        if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return false;
        if (!event.target.closest?.("[data-grammar]")) return false;
        const pos = view.posAtCoords({ x: event.clientX, y: event.clientY });
        if (pos == null || !matchAt(view.state, pos)) return false;
        view.dispatch({ effects: openAt.of(pos) });
        return false;
      },
    }),
    keymap.of([{
      key: "Mod-.",
      run(view) {
        const pos = view.state.selection.main.head;
        if (!matchAt(view.state, pos)) return false;
        opts.focusCard = true;
        view.dispatch({ effects: openAt.of(pos) });
        return true;
      },
    }]),
  ];
}

export function showMatches(view, matches) {
  view.dispatch({ effects: setMatches.of(matches) });
}

// -- The checkers ----------------------------------------------------------------

const DICT_KEY = "dw-grammar-words";
const IGNORE_KEY = "dw-grammar-ignored";

function load(key) {
  try { return JSON.parse(localStorage.getItem(key) || "[]"); } catch { return []; }
}
function save(key, list) {
  try { localStorage.setItem(key, JSON.stringify(list.slice(-2000))); } catch { /* private window */ }
}

export class GrammarChecker {
  constructor({ grammar, docKey, onError }) {
    this.grammar = grammar;
    this.docKey = docKey;
    this.onError = onError;
    this.worker = null;
    this.id = 0;
    this.pending = new Map();
    this.words = load(DICT_KEY);
    // Ignored findings, per document: "message\u0000word".
    this.ignored = new Set(load(`${IGNORE_KEY}:${docKey}`));
    this.failed = false;
  }

  ignore(match, word) {
    this.ignored.add(`${match.message}\u0000${word}`);
    save(`${IGNORE_KEY}:${this.docKey}`, [...this.ignored]);
  }

  addWord(word) {
    if (!this.words.includes(word)) this.words.push(word);
    save(DICT_KEY, this.words);
  }

  filter(text, matches) {
    return matches.filter((m) => !this.ignored.has(`${m.message}\u0000${text.slice(m.from, m.to)}`)
      && !(m.kind === "spelling" && this.words.includes(text.slice(m.from, m.to))));
  }

  async check(text, lang) {
    if (this.failed) return null;
    const which = checkerFor(this.grammar, lang);
    let matches = null;
    if (which === "harper") matches = await this.harper(text, dialectOf(lang));
    else if (which === "languagetool") matches = await this.languageTool(text, lang);
    return matches && this.filter(text, matches);
  }

  harper(text, dialect) {
    if (!this.worker) {
      this.worker = new Worker(new URL("../workers/grammar.js", import.meta.url), { type: "module" });
      this.worker.onmessage = (event) => {
        const resolve = this.pending.get(event.data.id);
        this.pending.delete(event.data.id);
        if (event.data.error) {
          this.failed = true;
          this.onError?.(event.data.error);
          resolve?.(null);
        } else {
          resolve?.(event.data.matches);
        }
      };
      this.worker.onerror = (event) => {
        event.preventDefault?.();
        this.failed = true;
        this.onError?.(event.message || "worker");
        for (const resolve of this.pending.values()) resolve(null);
        this.pending.clear();
      };
    }
    const id = ++this.id;
    return new Promise((resolve) => {
      // Only the newest check matters; older ones resolve to null.
      for (const [old, done] of this.pending) { done(null); this.pending.delete(old); }
      this.pending.set(id, resolve);
      this.worker.postMessage({ id, text, dialect, words: this.words });
    });
  }

  async languageTool(text, lang) {
    try {
      const res = await api("POST", "/api/grammar/check", { text, language: lang || "auto" });
      return res.matches.map((m) => ({ ...m, suggestions: m.suggestions.map((s) => ({ text: s, kind: "replace" })) }));
    } catch (error) {
      if (error?.status === 429) return null; // try again on the next pause
      this.failed = true;
      this.onError?.(error?.message || String(error));
      return null;
    }
  }
}

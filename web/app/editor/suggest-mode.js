// Suggest mode (ARCHITECTURE.md §8.10): while it is on, typing doesn't change the text.
// A run of typing at one place collects into a pending suggestion, shown in place: the
// passage it replaces struck through, the new text after it. The pending suggestion is sent
// as an ordinary suggestion thread once the cursor leaves it, the editor loses focus, typing
// pauses for a while, or the mode is turned off.
//
// Typed text never enters the document, so it can't be typed into the editor itself: input
// methods (phone keyboards, dead keys, Chinese and Japanese input) compose text in place and
// would compose into text that isn't there. While the cursor is in the text, a hidden
// textarea at the cursor takes the typing instead, as CodeMirror 5 did; the editor shows its
// contents and a cursor. Edits that start in the editor (typing over a selection, cutting,
// pasting, dropping, completions) become pending suggestions too, through a filter.
import {
  Decoration, EditorSelection, EditorState, EditorView, Prec, StateEffect, StateField, Transaction, ViewPlugin,
  WidgetType, keymap, runScopeHandlers,
} from "codemirror";
import { suggestionDecorations } from "./anchors.js";

const IDLE = 20000; // ms of no typing after which a pending suggestion is sent anyway
const WORD_BEFORE = /(?:[\p{L}\p{N}_]+|[^\s\p{L}\p{N}_])\s*$|\s+$/u;
const WORD_AFTER = /^\s*(?:[\p{L}\p{N}_]+|[^\s\p{L}\p{N}_])|^\s+/u;

export const setSuggestMode = StateEffect.define(); // boolean
const setPending = StateEffect.define(); // {from, to, text, caret} | null
const showCursor = StateEffect.define(); // boolean: the typing box has focus
const addSent = StateEffect.define(); // {key, from, to, text, fresh}
const dropSent = StateEffect.define(); // key
const markFailed = StateEffect.define(); // key

function mapItem(item, changes) {
  const from = changes.mapPos(item.from, 1);
  return { ...item, from, to: Math.max(from, changes.mapPos(item.to, -1)) };
}

// The typed text with a cursor in it, or only the cursor.
class PendingWidget extends WidgetType {
  constructor(text, caret, cursor) { super(); this.text = text; this.caret = caret; this.cursor = cursor; }
  eq(other) { return other.text === this.text && other.caret === this.caret && other.cursor === this.cursor; }
  toDOM() {
    const span = document.createElement("span");
    span.className = this.text ? "cm-suggest-ins cm-suggest-pending" : "cm-suggest-pending";
    span.append(this.text.slice(0, this.caret));
    if (this.cursor) {
      const caret = document.createElement("span");
      caret.className = "cm-suggest-caret";
      caret.setAttribute("aria-hidden", "true");
      span.append(caret);
    }
    span.append(this.text.slice(this.caret));
    return span;
  }
  ignoreEvent() { return false; }
}

// {on, pending: {from, to, text, caret} | null, cursor, sent: [{key, from, to, text, fresh, failed}]}
// pending covers the passage it replaces ([from, to) may be empty); its text goes at to.
export const suggestField = StateField.define({
  create: () => ({ on: false, pending: null, cursor: false, sent: [] }),
  update(value, tr) {
    let next = value;
    if (tr.docChanged) {
      next = { ...next, pending: next.pending && mapItem(next.pending, tr.changes),
               sent: next.sent.map((s) => mapItem(s, tr.changes)) };
    }
    for (const e of tr.effects) {
      if (e.is(setSuggestMode)) next = { ...next, on: e.value };
      else if (e.is(setPending)) next = { ...next, pending: e.value };
      else if (e.is(showCursor)) next = { ...next, cursor: e.value };
      else if (e.is(addSent)) next = { ...next, sent: [...next.sent, e.value] };
      else if (e.is(dropSent)) next = { ...next, sent: next.sent.filter((s) => s.key !== e.value) };
      else if (e.is(markFailed)) next = { ...next, sent: next.sent.map((s) => (s.key === e.value ? { ...s, failed: true } : s)) };
    }
    return next;
  },
  provide: (field) => EditorView.decorations.compute(["doc", field], (state) => {
    const { pending, sent, cursor } = state.field(field);
    const ranges = [];
    for (const s of sent) ranges.push(...suggestionDecorations(state.doc, s.from, s.to, s.text, { pending: true }));
    if (pending) {
      if (pending.to > pending.from) {
        ranges.push(Decoration.mark({ class: "cm-suggest-del cm-suggest-pending" }).range(pending.from, pending.to));
      }
      if (pending.text || cursor) {
        const caret = Math.min(pending.caret ?? pending.text.length, pending.text.length);
        ranges.push(Decoration.widget({ widget: new PendingWidget(pending.text, caret, cursor), side: -1 })
          .range(pending.to));
      }
    }
    return Decoration.set(ranges, true);
  }),
});

const meaningful = (p) => Boolean(p && (p.text || p.to > p.from));

// What one edit of the real text means for the pending suggestion. Returns the new pending
// suggestion, plus the one it replaces when the edit starts somewhere else.
export function absorb(doc, pending, changes, userEvent = "") {
  let from = Infinity;
  let to = -Infinity;
  let fromB = Infinity;
  let toB = -Infinity;
  changes.iterChangedRanges((fA, tA, fB, tB) => {
    from = Math.min(from, fA); to = Math.max(to, tA);
    fromB = Math.min(fromB, fB); toB = Math.max(toB, tB);
  });
  if (from === Infinity) return { pending, finished: null };
  const inserted = changes.apply(doc).sliceString(fromB, toB);
  if (pending) {
    if (from === pending.to && to === pending.to) {
      const text = pending.text + inserted;
      return { pending: { ...pending, text, caret: text.length }, finished: null };
    }
    if (to === pending.to && !inserted && from < to) {
      if (pending.text) {
        const text = userEvent.startsWith("delete.group") ? pending.text.replace(WORD_BEFORE, "")
          : Array.from(pending.text).slice(0, -1).join("");
        return { pending: { ...pending, text, caret: text.length }, finished: null };
      }
      return { pending: strikeBefore(doc, pending, userEvent.startsWith("delete.group")), finished: null };
    }
    if (from === pending.to && !inserted && to > from) return { pending: { ...pending, to }, finished: null };
  }
  return { pending: { from, to, text: inserted, caret: inserted.length }, finished: pending };
}

// Backspace before the typed text: strike one more character (or word) before it.
export function strikeBefore(doc, pending, word = false) {
  const line = doc.lineAt(pending.from);
  const before = line.text.slice(0, pending.from - line.from);
  const m = word ? WORD_BEFORE.exec(before) : /.$/su.exec(before);
  const count = m ? m[0].length : Math.min(1, pending.from); // at the start of a line, the line break
  return { ...pending, from: pending.from - count };
}

// Delete after the typed text: strike one more character (or word) after it.
export function strikeAfter(doc, pending, word = false) {
  const line = doc.lineAt(pending.to);
  const after = line.text.slice(pending.to - line.from);
  const m = word ? WORD_AFTER.exec(after) : /^./su.exec(after);
  const count = m ? m[0].length : (pending.to < doc.length ? 1 : 0);
  return { ...pending, to: pending.to + count };
}

// Suggestions need some text to sit on (§8.10, like Word's insertions in §8.11): an insertion
// takes in the word it is in or follows, or the word after it at the start of a line.
export function widen(doc, { from, to, text }) {
  if (to > from) return { from, to, text };
  const line = doc.lineAt(from);
  const before = line.text.slice(0, from - line.from);
  const after = line.text.slice(from - line.from);
  let m = /\S+\s*$/u.exec(before);
  if (m) {
    const rest = /\S$/u.test(before) ? (/^\S+/u.exec(after)?.[0] ?? "") : "";
    return { from: from - m[0].length, to: to + rest.length, text: m[0] + text + rest };
  }
  m = /^\s*\S+/u.exec(after);
  if (m) return { from, to: to + m[0].length, text: text + m[0] };
  if (from > 0) return { from: from - 1, to, text: doc.sliceString(from - 1, from) + text };
  if (to < doc.length) return { from, to: to + 1, text: text + doc.sliceString(to, to + 1) };
  return null;
}

// The editor side of suggest mode. submit(locate) sends one suggestion and resolves to true
// once it shows as a thread (or can never be sent), false to try again later; locate() gives
// its {from, to, text} in the current text, insertions widened, or null if nothing is left.
// labels name the typing box and the selection toolbar's buttons; touch is true on touch
// screens, where a selection gets that toolbar, with onComment for its Comment button.
const LABELS = { box: "Suggestion", strike: "Delete", replace: "Replace", comment: "Comment" };

export function suggestMode({ submit, labels = LABELS, touch = false, onComment = null }) {
  let keys = 0;

  // Edits that start in the editor become part of a suggestion instead. Remote updates and
  // undo don't pass filters; edits replayed from this browser's offline copy are let through.
  const filter = EditorState.transactionFilter.of((tr) => {
    const state = tr.startState;
    const mode = state.field(suggestField, false);
    if (!mode?.on || !tr.docChanged || tr.isUserEvent("input.offline")) return tr;
    const { pending, finished } = absorb(state.doc, mode.pending, tr.changes, tr.annotation(Transaction.userEvent) ?? "");
    const effects = [setPending.of(meaningful(pending) ? pending : null)];
    if (meaningful(finished)) effects.push(addSent.of({ key: ++keys, ...finished, fresh: true }));
    return { effects, selection: EditorSelection.cursor(pending.to), scrollIntoView: true, userEvent: "suggest" };
  });

  class Typing {
    constructor(view) {
      this.view = view;
      this.timer = null;
      this.busy = false;
      this.leaving = false; // focus goes back to the editor on purpose
      this.composing = false;
      const box = document.createElement("textarea");
      box.className = "cm-suggest-box";
      box.rows = 1;
      box.tabIndex = -1;
      box.spellcheck = true;
      for (const [k, v] of Object.entries({ "aria-label": labels.box, autocapitalize: "sentences", autocomplete: "off" })) {
        box.setAttribute(k, v);
      }
      box.addEventListener("input", () => this.typed());
      box.addEventListener("keydown", (e) => this.key(e));
      box.addEventListener("keyup", () => this.typed());
      box.addEventListener("pointerup", () => this.typed());
      box.addEventListener("compositionstart", () => { this.composing = true; });
      box.addEventListener("compositionend", () => { this.composing = false; this.typed(); });
      box.addEventListener("focus", () => this.view.dispatch({ effects: showCursor.of(true) }));
      box.addEventListener("blur", () => queueMicrotask(() => {
        if (this.focused) return; // moved along with the cursor
        if (this.state?.cursor) this.view.dispatch({ effects: showCursor.of(false) });
        if (!this.leaving) this.finish(false);
        this.leaving = false;
      }));
      this.box = box;
      view.dom.append(box);
    }

    get state() { return this.view.state.field(suggestField, false); }

    get focused() { return document.activeElement === this.box; }

    update(u) {
      const now = this.state;
      const before = u.startState.field(suggestField, false);
      if (!now) return;
      if (now.pending !== before?.pending) {
        clearTimeout(this.timer);
        if (meaningful(now.pending)) this.timer = setTimeout(() => this.idle(), IDLE);
      }
      if (!now.on) {
        if (now.pending || this.focused) queueMicrotask(() => { this.finish(false); if (this.focused) this.leave(); });
        return;
      }
      if (now.sent.some((s) => s.fresh)) queueMicrotask(() => this.flush());
      if (u.selectionSet || u.focusChanged || (before && !before.on)) queueMicrotask(() => this.follow());
      if (this.focused && (u.docChanged || u.geometryChanged || now.pending !== before?.pending)) this.place();
    }

    // Keep the typing box at the cursor: focused while the editor has a cursor, and the
    // editor focused while it has a selection.
    follow() {
      const mode = this.state;
      if (!mode?.on) return;
      const sel = this.view.state.selection.main;
      if (mode.pending && (!sel.empty || sel.head !== mode.pending.to)) this.finish(false);
      if (!sel.empty) {
        if (this.focused) this.leave();
        return;
      }
      if (this.view.hasFocus) this.enter(sel.head);
    }

    // Take typing at pos, continuing the pending suggestion if it ends there.
    enter(pos) {
      let pending = this.state.pending;
      if (!pending || pending.to !== pos) {
        if (pending) this.finish(false);
        pending = { from: pos, to: pos, text: "", caret: 0 };
        this.view.dispatch({ effects: setPending.of(pending), selection: EditorSelection.cursor(pos) });
      }
      if (this.box.value !== pending.text) this.box.value = pending.text;
      this.place();
      if (!this.focused) {
        this.box.focus({ preventScroll: true });
        this.box.setSelectionRange(pending.caret, pending.caret);
      }
    }

    // Give the keyboard back to the editor.
    leave() {
      this.leaving = this.focused;
      this.view.focus();
    }

    place() {
      this.view.requestMeasure({
        key: this,
        read: (view) => {
          const pending = this.state?.pending;
          if (!pending) return null;
          return { at: view.coordsAtPos(pending.to, -1) ?? view.coordsAtPos(pending.to, 1),
                   box: view.dom.getBoundingClientRect() };
        },
        write: (m) => {
          if (!m?.at) return;
          this.box.style.left = `${Math.max(0, m.at.left - m.box.left)}px`;
          this.box.style.top = `${m.at.top - m.box.top}px`;
        },
      });
    }

    // Show what the typing box holds, and where its cursor is.
    typed() {
      if (!this.state?.on) return;
      let pending = this.state.pending;
      if (!pending) {
        const head = this.view.state.selection.main.head;
        pending = { from: head, to: head, text: "", caret: 0 };
      }
      const text = this.box.value;
      const caret = this.box.selectionEnd;
      if (text === pending.text && caret === pending.caret) return;
      this.view.dispatch({ effects: setPending.of({ ...pending, text, caret }), scrollIntoView: false });
    }

    key(event) {
      const pending = this.state?.pending;
      if (!pending || this.composing || event.isComposing || event.keyCode === 229) return;
      const box = this.box;
      const atStart = box.selectionStart === 0 && box.selectionEnd === 0;
      const atEnd = box.selectionStart === box.value.length && box.selectionEnd === box.value.length;
      const mod = event.ctrlKey || event.metaKey;
      const word = event.altKey || event.ctrlKey;
      const doc = this.view.state.doc;
      if (event.key === "Backspace" && atStart && !event.metaKey) {
        event.preventDefault();
        if (pending.from > 0) this.view.dispatch({ effects: setPending.of(strikeBefore(doc, pending, word)) });
        return;
      }
      if (event.key === "Delete" && atEnd && !event.metaKey) {
        event.preventDefault();
        if (pending.to < doc.length) this.view.dispatch({ effects: setPending.of(strikeAfter(doc, pending, word)) });
        return;
      }
      if (mod && event.key.toLowerCase() === "z" && !box.value && pending.to > pending.from) {
        event.preventDefault();
        this.view.dispatch({ effects: setPending.of({ ...pending, from: pending.to }) });
        return;
      }
      const nav = /^(Arrow(Left|Right|Up|Down)|Home|End|Page(Up|Down))$/.test(event.key);
      const oneLine = !box.value.includes("\n");
      const leavesText = nav && (event.shiftKey || !box.value
        || (event.key === "ArrowLeft" && atStart) || (event.key === "ArrowRight" && atEnd)
        || (oneLine && /^(ArrowUp|ArrowDown|PageUp|PageDown)$/.test(event.key)));
      const editorKey = mod && !event.altKey && !/^([zyxcv]|Backspace|Delete|Arrow(Left|Right))$/i.test(event.key);
      if (event.key === "Escape" || leavesText || editorKey) {
        // Send what was typed, put the editor's cursor by it and let the editor handle the
        // key from there.
        event.preventDefault();
        const at = event.key === "ArrowLeft" && !event.shiftKey && meaningful(pending) ? pending.from : pending.to;
        this.finish(false);
        this.leaving = true;
        this.view.dispatch({ selection: EditorSelection.cursor(at) });
        this.view.focus();
        if (event.key !== "Escape") runScopeHandlers(this.view, event, "editor");
      }
    }

    idle() {
      if (this.composing) { this.timer = setTimeout(() => this.idle(), IDLE); return; }
      this.finish(true);
    }

    // Send the pending suggestion. With stay, typing goes on at the same place.
    finish(stay) {
      clearTimeout(this.timer);
      const pending = this.state?.pending;
      if (!pending) return;
      const effects = [setPending.of(stay ? { from: pending.to, to: pending.to, text: "", caret: 0 } : null)];
      if (meaningful(pending)) {
        effects.push(addSent.of({ key: ++keys, from: pending.from, to: pending.to, text: pending.text, fresh: true }));
      }
      this.box.value = "";
      this.view.dispatch({ effects });
    }

    // Send what is waiting, one at a time; with retry, also what failed before.
    async flush(retry = false) {
      if (this.busy) return;
      this.busy = true;
      try {
        for (;;) {
          const item = this.state?.sent.find((s) => s.fresh || (retry && s.failed));
          if (!item) break;
          const { key } = item;
          this.view.dispatch({ effects: [dropSent.of(key), addSent.of({ ...item, fresh: false, failed: false })] });
          const locate = () => {
            const doc = this.view.state.doc;
            const now = this.state.sent.find((s) => s.key === key);
            const wide = now && widen(doc, now);
            return wide && doc.sliceString(wide.from, wide.to) !== wide.text ? wide : null;
          };
          const ok = locate() ? await submit(locate) : true;
          this.view.dispatch({ effects: ok ? dropSent.of(key) : markFailed.of(key) });
        }
      } finally {
        this.busy = false;
      }
    }

    destroy() {
      clearTimeout(this.timer);
      this.box.remove();
    }
  }

  const typing = ViewPlugin.fromClass(Typing, {
    eventHandlers: {
      // A tap puts the cursor and opens the keyboard for the typing box within the tap,
      // which phones require before they show a keyboard.
      click(event, view) {
        if (!touch || !view.state.field(suggestField, false)?.on || !view.state.selection.main.empty) return false;
        const pos = view.posAtCoords({ x: event.clientX, y: event.clientY });
        if (pos !== null) this.enter(pos);
        return false;
      },
    },
  });

  // Undo takes back the suggestion being typed (undo doesn't pass the filter).
  const takeBack = (view) => {
    const mode = view.state.field(suggestField, false);
    if (!mode?.on || !meaningful(mode.pending)) return false;
    view.dispatch({ effects: setPending.of(null), selection: EditorSelection.cursor(mode.pending.to) });
    return true;
  };

  // Typing goes in as typed: no closing brackets or quotes added after the cursor.
  const plainInput = EditorView.inputHandler.of((view, from, to, text, insert) => {
    if (!view.state.field(suggestField, false)?.on) return false;
    view.dispatch(insert());
    return true;
  });

  // On touch screens the editor's own keyboard stays closed while suggesting: typing goes
  // to the typing box, and a selection is struck or replaced from the selection toolbar.
  const noKeyboard = EditorView.contentAttributes.compute([suggestField], (state) =>
    (touch && state.field(suggestField).on ? { inputmode: "none" } : {}));

  const startOn = (view, sel) => {
    view.dispatch({ effects: setPending.of({ from: sel.from, to: sel.to, text: "", caret: 0 }),
                    selection: EditorSelection.cursor(sel.to) });
  };

  // On touch screens, a selection gets a small toolbar: strike it, replace it, or comment.
  class Toolbar {
    constructor(view) {
      this.view = view;
      this.node = null;
      this.scroll = () => this.position();
      view.scrollDOM.addEventListener("scroll", this.scroll);
    }

    update(u) {
      if (u.selectionSet || u.docChanged || u.geometryChanged || u.startState.field(suggestField, false)?.on !== u.state.field(suggestField, false)?.on) {
        this.position();
      }
    }

    position() {
      const view = this.view;
      const sel = view.state.selection.main;
      if (!touch || !view.state.field(suggestField, false)?.on || sel.empty) {
        if (this.node) this.node.hidden = true;
        return;
      }
      if (!this.node) {
        const button = (text, run, cls = "") => {
          const b = document.createElement("button");
          b.type = "button";
          b.textContent = text;
          if (cls) b.className = cls;
          b.addEventListener("mousedown", (e) => e.preventDefault());
          b.addEventListener("click", () => run(view));
          return b;
        };
        this.node = document.createElement("div");
        this.node.className = "review-bubble suggest-bubble";
        this.node.setAttribute("role", "toolbar");
        this.node.append(button(labels.strike, api.strike), button(labels.replace, api.replace, "primary"));
        if (labels.comment && onComment) this.node.append(button(labels.comment, () => onComment()));
        view.dom.append(this.node);
      }
      view.requestMeasure({
        key: this,
        read: () => ({ end: view.coordsAtPos(sel.to, -1), box: view.dom.getBoundingClientRect() }),
        write: ({ end, box }) => {
          if (!end) { this.node.hidden = true; return; }
          this.node.hidden = false;
          const width = this.node.offsetWidth;
          this.node.style.left = `${Math.min(Math.max(8, end.left - box.left - width / 2), box.width - width - 8)}px`;
          this.node.style.top = `${end.bottom - box.top + 6}px`;
        },
      });
    }

    destroy() {
      this.view.scrollDOM.removeEventListener("scroll", this.scroll);
      this.node?.remove();
    }
  }

  const api = {
    extension: [suggestField, filter, Prec.highest(plainInput), typing, noKeyboard, ViewPlugin.fromClass(Toolbar), Prec.highest(keymap.of([
      { key: "Mod-z", run: takeBack },
      { key: "Mod-Alt-g", run: () => true }, // the page's shortcut for suggest mode, not Go to line
    ]))],
    // For the selection toolbar: strike the selection, or strike it and type its replacement.
    strike(view) {
      const sel = view.state.selection.main;
      if (sel.empty) return;
      startOn(view, sel);
      view.plugin(typing)?.finish(false);
    },
    replace(view) {
      const sel = view.state.selection.main;
      if (sel.empty) return;
      startOn(view, sel);
      view.plugin(typing)?.enter(sel.to);
    },
    takeBack,
    // Send the suggestion being typed now (the page is going away).
    finish: (view) => view.plugin(typing)?.finish(false),
    // Send suggestions that failed, once the server is reachable again.
    retry: (view) => view.plugin(typing)?.flush(true),
  };
  return api;
}

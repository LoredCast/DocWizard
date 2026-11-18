// Comment and conflict anchors in the editor (ARCHITECTURE.md §8.8, §12.3, §25.8).
// The server sends ranges at a version; once mapped to the current document they
// live in this field, which maps them through every transaction.
import { Decoration, EditorView, GutterMarker, RangeSet, StateEffect, StateField, WidgetType, gutter } from "codemirror";
import { sharedEnds } from "../ui/worddiff.js";

// The text a suggestion would put in place of its passage (§8.10).
class SuggestionWidget extends WidgetType {
  constructor(text, id, pending) { super(); this.text = text; this.id = id; this.pending = pending; }
  eq(other) { return other.text === this.text && other.id === this.id && other.pending === this.pending; }
  toDOM() {
    const span = document.createElement("span");
    span.className = this.pending ? "cm-suggest-ins cm-suggest-pending" : "cm-suggest-ins";
    if (this.id !== null) span.dataset.thread = String(this.id);
    span.textContent = this.text;
    return span;
  }
  ignoreEvent() { return false; }
}

// A suggestion in place: the words it removes struck through, the words it brings right
// after them. Words it keeps at either end stay as they are, so a suggestion that adds a
// word to "cat" shows the new word, not "cat" struck and written again. A pending
// suggestion (being typed, §8.10) puts its new text before the cursor.
export function suggestionDecorations(doc, from, to, text, { id = null, active = false, pending = false } = {}) {
  const before = doc.sliceString(from, to);
  const { head, tail } = sharedEnds(before, text);
  const cut = { from: from + head, to: to - tail };
  const insert = text.slice(head, text.length - tail);
  const attributes = id !== null ? { "data-thread": String(id) } : {};
  const ranges = [];
  if (active && to > from) ranges.push(Decoration.mark({ class: "cm-comment-active", attributes }).range(from, to));
  if (cut.to > cut.from) {
    ranges.push(Decoration.mark({ class: pending ? "cm-suggest-del cm-suggest-pending" : "cm-suggest-del", attributes })
      .range(cut.from, cut.to));
  }
  if (insert || (!pending && cut.to <= cut.from)) {
    ranges.push(Decoration.widget({ widget: new SuggestionWidget(insert || "\u2009", id, pending), side: pending ? -1 : 1 })
      .range(cut.to));
  }
  return ranges;
}

// {kind: "comment" | "conflict", items: [{id, from, to}]} replaces every anchor of that kind.
export const setAnchors = StateEffect.define();
export const setActiveThread = StateEffect.define();

export const anchorsField = StateField.define({
  create: () => ({ comment: [], conflict: [], draft: [], active: null }),
  update(value, tr) {
    let next = value;
    if (tr.docChanged) {
      const map = (items) => items.map((a) => {
        const from = tr.changes.mapPos(a.from, 1);
        return { ...a, from, to: Math.max(from, tr.changes.mapPos(a.to, -1)) };
      });
      next = { ...value, comment: map(value.comment), conflict: map(value.conflict), draft: map(value.draft) };
    }
    for (const e of tr.effects) {
      if (e.is(setAnchors)) {
        const len = tr.state.doc.length;
        const items = e.value.items.map((a) => {
          const from = Math.min(Math.max(0, a.from), len);
          return { id: a.id, from, to: Math.min(Math.max(from, a.to), len),
                   ...(typeof a.suggestion === "string" ? { suggestion: a.suggestion } : {}) };
        });
        next = { ...next, [e.value.kind]: items };
      } else if (e.is(setActiveThread)) {
        next = { ...next, active: e.value };
      }
    }
    return next;
  },
  provide: (field) => EditorView.decorations.compute(["doc", field], (state) => {
    const value = state.field(field);
    const ranges = [];
    for (const a of value.comment) {
      if (typeof a.suggestion === "string") {
        ranges.push(...suggestionDecorations(state.doc, a.from, a.to, a.suggestion, { id: a.id, active: a.id === value.active }));
        continue;
      }
      if (a.to <= a.from) continue;
      const cls = a.id === value.active ? "cm-comment-mark cm-comment-active" : "cm-comment-mark";
      ranges.push(Decoration.mark({ class: cls, attributes: { "data-thread": String(a.id) } }).range(a.from, a.to));
    }
    for (const a of value.draft) {
      if (a.to > a.from) ranges.push(Decoration.mark({ class: "cm-comment-mark cm-comment-active" }).range(a.from, a.to));
    }
    return Decoration.set(ranges, true);
  }),
});

class ConflictMarker extends GutterMarker {
  constructor(id) { super(); this.id = id; }
  eq(other) { return other.id === this.id; }
  toDOM() {
    const bar = document.createElement("span");
    bar.className = "cm-conflict-bar";
    bar.title = "Conflict: open to compare both versions";
    bar.dataset.conflict = String(this.id);
    return bar;
  }
}

// A 2px bar in the gutter next to every line a conflict touches; clicking it calls onClick(id).
export function conflictGutter(onClick) {
  return gutter({
    class: "cm-conflict-gutter",
    markers(view) {
      const { conflict } = view.state.field(anchorsField);
      const marks = [];
      for (const a of conflict) {
        const first = view.state.doc.lineAt(a.from).number;
        const last = view.state.doc.lineAt(a.to).number;
        for (let n = first; n <= last; n++) marks.push(new ConflictMarker(a.id).range(view.state.doc.line(n).from));
      }
      return RangeSet.of(marks, true);
    },
    initialSpacer: () => new ConflictMarker(0),
    domEventHandlers: {
      mousedown(view, line) {
        const { conflict } = view.state.field(anchorsField);
        const hit = conflict.find((a) => a.from <= line.to && a.to >= line.from);
        if (hit) { onClick(hit.id); return true; }
        return false;
      },
    },
  });
}

// Loads anchored items, maps them to the current document and keeps retrying while
// this editor hasn't reached the version they were sent at.
export class AnchorLoader {
  constructor(session, kind, fetcher) {
    this.session = session;
    this.kind = kind;
    this.fetcher = fetcher; // async () => {version, items: [{id, from, to}]}
    this.pending = null;
    this.retried = false;
  }

  async load() {
    try {
      this.pending = await this.fetcher();
      this.retried = false;
    } catch {
      return;
    }
    this.flush();
  }

  flush() {
    if (!this.pending || !this.session.view) return;
    const { version, items } = this.pending;
    const placed = items.filter((a) => a.from !== null && a.from !== undefined);
    const mapped = this.session.toCurrent(version, placed.map((a) => [a.from, a.to]));
    if (!mapped) {
      // Behind: wait for updates. Too far ahead of our history: fetch again once.
      if (version < this.session.version() && !this.retried) { this.retried = true; this.load(); }
      return;
    }
    this.pending = null;
    this.session.view.dispatch({ effects: setAnchors.of({
      kind: this.kind, items: placed.map((a, i) => ({ ...a, from: mapped[i][0], to: mapped[i][1] })) }) });
  }
}

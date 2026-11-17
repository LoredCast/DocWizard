// Comment and conflict anchors in the editor (ARCHITECTURE.md §8.8, §12.3, §25.8).
// The server sends ranges at a version; once mapped to the current document they
// live in this field, which maps them through every transaction.
import { Decoration, EditorView, GutterMarker, RangeSet, StateEffect, StateField, WidgetType, gutter } from "codemirror";

// The text a suggestion would put in place of its passage, shown right after it (§8.10).
class SuggestionWidget extends WidgetType {
  constructor(text, id) { super(); this.text = text; this.id = id; }
  eq(other) { return other.text === this.text && other.id === this.id; }
  toDOM() {
    const span = document.createElement("span");
    span.className = "cm-suggest-ins";
    span.dataset.thread = String(this.id);
    span.textContent = this.text || "\u2009";
    return span;
  }
  ignoreEvent() { return false; }
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
  provide: (field) => EditorView.decorations.from(field, (value) => {
    const ranges = [];
    for (const a of value.comment) {
      const suggesting = typeof a.suggestion === "string";
      if (a.to <= a.from && !suggesting) continue;
      let cls = a.id === value.active ? "cm-comment-mark cm-comment-active" : "cm-comment-mark";
      if (suggesting) cls += " cm-suggest-del";
      if (a.to > a.from) {
        ranges.push(Decoration.mark({ class: cls, attributes: { "data-thread": String(a.id) } }).range(a.from, a.to));
      }
      if (suggesting) {
        ranges.push(Decoration.widget({ widget: new SuggestionWidget(a.suggestion, a.id), side: 1 }).range(a.to));
      }
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

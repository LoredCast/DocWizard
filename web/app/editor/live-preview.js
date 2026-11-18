// Live preview (ARCHITECTURE.md §6.3): on every line except those with the
// cursor, hide syntax marks, style content, show citations as chips, math
// as MathML and footnotes as numbers. The cursor line shows raw source, except
// in writer mode, where the marks stay hidden there too.
import { Decoration, EditorView, StateEffect, ViewPlugin, WidgetType, syntaxTree } from "codemirror";
import { chipText, libraryField, setLibrary } from "./citations.js";
import { loadTemml, onTemmlReady, renderMath, temmlReady } from "./math.js";
import { t } from "../ui/dom.js";
import { crossRefTargets } from "./figures.js";
import { writerMode } from "./writer-mode.js";

// Cross-references ([@fig:map]) as what they print: "Figure 1". Numbered per document text.
const XREF = /^(fig|tbl|eq|sec):/;
const xrefCache = new WeakMap();
function xrefsOf(doc) {
  let map = xrefCache.get(doc);
  if (!map) {
    map = new Map(crossRefTargets(doc.toString()).map((x) => [x.id, x]));
    xrefCache.set(doc, map);
  }
  return map;
}
const XREF_NAMES = { fig: "Figure", tbl: "Table", eq: "Equation", sec: "Section" };

export const refreshDecorations = StateEffect.define();

const hidden = Decoration.replace({});
const marks = {
  em: Decoration.mark({ class: "cm-md-em" }),
  strong: Decoration.mark({ class: "cm-md-strong" }),
  strike: Decoration.mark({ class: "cm-md-strike" }),
  link: Decoration.mark({ class: "cm-md-link" }),
  code: Decoration.mark({ class: "cm-md-code" }),
  unknown: Decoration.mark({ class: "cm-cite-unknown" }),
};
const lineClass = {};
const lineDeco = (cls) => (lineClass[cls] ??= Decoration.line({ class: cls }));

class CiteWidget extends WidgetType {
  constructor(text, title) { super(); this.text = text; this.title = title; }
  eq(other) { return other.text === this.text && other.title === this.title; }
  toDOM() {
    const span = document.createElement("span");
    span.className = "cm-cite-chip";
    span.textContent = this.text;
    span.title = this.title;
    return span;
  }
  ignoreEvent() { return false; }
}

class MathWidget extends WidgetType {
  constructor(tex, display) { super(); this.tex = tex; this.display = display; }
  eq(other) { return other.tex === this.tex && other.display === this.display; }
  toDOM() { return renderMath(this.tex, this.display); }
  ignoreEvent() { return false; }
}

// A footnote as its number; a click opens it to read and edit (editor/insert-tools.js).
class NoteWidget extends WidgetType {
  constructor(n, from, to, text) { super(); this.n = n; this.from = from; this.to = to; this.text = text; }
  eq(other) { return other.n === this.n && other.from === this.from && other.to === this.to && other.text === this.text; }
  toDOM() {
    const sup = document.createElement("sup");
    sup.className = "cm-note-chip";
    sup.textContent = String(this.n);
    sup.title = this.text.length > 160 ? `${this.text.slice(0, 160)}…` : this.text;
    sup.setAttribute("role", "button");
    sup.setAttribute("aria-label", t("Footnote {n}", { n: this.n }));
    sup.addEventListener("mousedown", (e) => e.preventDefault());
    sup.addEventListener("click", () => sup.dispatchEvent(new CustomEvent("dw:note", {
      bubbles: true, detail: { from: this.from, to: this.to } })));
    return sup;
  }
  ignoreEvent() { return true; }
}

class TaskWidget extends WidgetType {
  constructor(checked, pos) { super(); this.checked = checked; this.pos = pos; }
  eq(other) { return other.checked === this.checked && other.pos === this.pos; }
  toDOM(view) {
    const box = document.createElement("input");
    box.type = "checkbox";
    box.className = "cm-task";
    box.checked = this.checked;
    box.setAttribute("aria-label", t("Task done"));
    box.addEventListener("mousedown", (e) => e.preventDefault());
    box.addEventListener("click", () => {
      if (view.state.readOnly) return;
      view.dispatch({ changes: { from: this.pos + 1, to: this.pos + 2, insert: this.checked ? " " : "x" } });
    });
    return box;
  }
  ignoreEvent() { return true; }
}

function cursorLines(state) {
  const lines = new Set();
  for (const r of state.selection.ranges) {
    const a = state.doc.lineAt(r.from).number, b = state.doc.lineAt(r.to).number;
    for (let n = a; n <= b; n++) lines.add(n);
  }
  return lines;
}

function build(view, enabled, reading = false) {
  const { state } = view;
  const out = [];
  // Reading (the review view, §14.1) and writer mode: no line shows its marks.
  const active = reading || writerMode(state) ? new Set() : cursorLines(state);
  // Footnotes are numbered in order; the ones before the visible text count too.
  const notes = [];
  const lastVisible = view.visibleRanges.length ? view.visibleRanges[view.visibleRanges.length - 1].to : 0;
  syntaxTree(state).iterate({ from: 0, to: lastVisible, enter(node) {
    if (node.name === "InlineNote") { notes.push(node.from); return false; }
    return undefined;
  } });
  const library = state.field(libraryField, false) ?? new Map();
  const onActive = (from, to) => {
    const a = state.doc.lineAt(from).number, b = state.doc.lineAt(to).number;
    for (let n = a; n <= b; n++) if (active.has(n)) return true;
    return false;
  };
  const hide = (from, to) => { if (to > from) out.push(hidden.range(from, to)); };
  const linesOf = (from, to, cls) => {
    for (let pos = from; pos <= to;) {
      const line = state.doc.lineAt(pos);
      out.push(lineDeco(cls).range(line.from));
      pos = line.to + 1;
    }
  };

  for (const { from, to } of view.visibleRanges) {
    syntaxTree(state).iterate({
      from, to,
      enter(node) {
        const name = node.name;
        const live = enabled && !onActive(node.from, node.to);
        if (/^ATXHeading[1-6]$/.test(name)) {
          out.push(lineDeco(`cm-h${name.slice(-1)}`).range(state.doc.lineAt(node.from).from));
        } else if (name === "SetextHeading1" || name === "SetextHeading2") {
          out.push(lineDeco(name === "SetextHeading1" ? "cm-h1" : "cm-h2").range(state.doc.lineAt(node.from).from));
        } else if (name === "HeaderMark") {
          if (live && state.doc.lineAt(node.from).from === node.from) {
            const after = state.sliceDoc(node.to, node.to + 1) === " " ? 1 : 0;
            hide(node.from, node.to + after);
          }
        } else if (name === "Emphasis") {
          out.push(marks.em.range(node.from, node.to));
        } else if (name === "StrongEmphasis") {
          out.push(marks.strong.range(node.from, node.to));
        } else if (name === "Strikethrough") {
          out.push(marks.strike.range(node.from, node.to));
        } else if (name === "EmphasisMark" || name === "StrikethroughMark" || name === "CodeMark") {
          if (live && !(name === "CodeMark" && node.node.parent?.name === "FencedCode")) hide(node.from, node.to);
        } else if (name === "InlineCode") {
          out.push(marks.code.range(node.from, node.to));
        } else if (name === "Link") {
          const children = [];
          for (let c = node.node.firstChild; c; c = c.nextSibling) children.push(c);
          const linkMarks = children.filter((c) => c.name === "LinkMark");
          if (linkMarks.length >= 2) {
            const textFrom = linkMarks[0].to, textTo = linkMarks[1].from;
            if (textTo > textFrom) out.push(marks.link.range(textFrom, textTo));
            if (live && textTo > textFrom) {
              hide(linkMarks[0].from, linkMarks[0].to);
              hide(linkMarks[1].from, node.to);
            }
          }
          return false;
        } else if (name === "FencedCode" || name === "CodeBlock") {
          linesOf(node.from, node.to, "cm-md-code");
        } else if (name === "Table") {
          // Monospaced, so padded columns line up.
          linesOf(node.from, node.to, "cm-md-table");
        } else if (name === "InlineNote") {
          if (live) {
            const n = notes.indexOf(node.from) + 1 || notes.length + 1;
            const text = state.sliceDoc(node.from + 2, Math.max(node.from + 2, node.to - 1));
            out.push(Decoration.replace({ widget: new NoteWidget(n, node.from, node.to, text) }).range(node.from, node.to));
            return false;
          }
        } else if (name === "Attributes") {
          // {#fig:map}, {#sec:method .unnumbered}: labels for cross-references, out of the way.
          if (live) {
            const start = state.sliceDoc(node.from - 1, node.from) === " " ? node.from - 1 : node.from;
            hide(start, node.to);
          }
        } else if (name === "FrontMatter") {
          linesOf(node.from, node.to, "cm-md-frontmatter");
          return false;
        } else if (name === "Citation") {
          const keys = [];
          for (let c = node.node.firstChild; c; c = c.nextSibling) {
            if (c.name === "CitationKey") keys.push(c);
          }
          const ids = keys.map((k) => state.sliceDoc(k.from, k.to).replace(/^@/, ""));
          if (ids.length && ids.every((id) => XREF.test(id))) {
            const xrefs = xrefsOf(state.doc);
            const found = ids.map((id) => xrefs.get(id));
            if (live && found.every(Boolean) && state.doc.lineAt(node.from).number === state.doc.lineAt(node.to).number) {
              const label = found.map((x) => `${t(XREF_NAMES[x.kind])} ${x.n}`).join(", ");
              const title = found.map((x) => x.caption || x.id).join("\n");
              out.push(Decoration.replace({ widget: new CiteWidget(label, title) }).range(node.from, node.to));
            } else {
              keys.forEach((k, i) => { if (!found[i]) out.push(marks.unknown.range(k.from, k.to)); });
            }
            return false;
          }
          const allKnown = ids.length > 0 && ids.every((id) => library.has(id));
          if (live && allKnown && state.doc.lineAt(node.from).number === state.doc.lineAt(node.to).number) {
            const source = state.sliceDoc(node.from, node.to);
            const title = ids.map((id) => library.get(id)).map((i) => `${i.label}: ${i.title}`).join("\n");
            out.push(Decoration.replace({ widget: new CiteWidget(chipText(source, library), title) })
              .range(node.from, node.to));
          } else if (library.size) {
            for (const k of keys) {
              const id = state.sliceDoc(k.from, k.to).replace(/^@/, "");
              if (!library.has(id)) out.push(marks.unknown.range(k.from, k.to));
            }
          }
          return false;
        } else if (name === "InlineMath" || name === "DisplayMath") {
          const display = name === "DisplayMath";
          const single = state.doc.lineAt(node.from).number === state.doc.lineAt(node.to).number;
          if (!temmlReady()) {
            loadTemml();
          } else if (live && single) {
            const pad = display ? 2 : 1;
            const tex = state.sliceDoc(node.from + pad, node.to - pad);
            out.push(Decoration.replace({ widget: new MathWidget(tex, display) }).range(node.from, node.to));
          }
          return false;
        } else if (name === "TaskMarker") {
          if (live) {
            const checked = /x/i.test(state.sliceDoc(node.from, node.to));
            out.push(Decoration.replace({ widget: new TaskWidget(checked, node.from) }).range(node.from, node.to));
          }
        }
        return undefined;
      },
    });
  }
  return Decoration.set(out, true);
}

export function livePreview({ enabled = true, reading = false } = {}) {
  return ViewPlugin.fromClass(class {
    constructor(view) {
      this.enabled = enabled;
      this.decorations = build(view, this.enabled, reading);
      onTemmlReady(() => view.dispatch({ effects: refreshDecorations.of(null) }));
    }
    update(update) {
      const refresh = update.transactions.some((tr) => tr.effects.some((e) => e.is(setLibrary) || e.is(refreshDecorations)));
      const writer = writerMode(update.startState) !== writerMode(update.state);
      if (update.docChanged || update.viewportChanged || update.selectionSet || refresh || writer
          || syntaxTree(update.startState) !== syntaxTree(update.state)) {
        this.decorations = build(update.view, this.enabled, reading);
      }
    }
  }, {
    decorations: (v) => v.decorations,
    provide: (plugin) => EditorView.atomicRanges.of((view) => view.plugin(plugin)?.decorations ?? Decoration.none),
  });
}

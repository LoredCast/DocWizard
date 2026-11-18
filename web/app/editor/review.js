// The review view (ARCHITECTURE.md §14.1): the document as it reads, for people who don't
// write Markdown. The text stays the real document in a read-only editor, so a selection is
// a range of the source and comments and suggestions anchor exactly as in the editor.
import { Decoration, EditorView, StateField, WidgetType } from "codemirror";
import { el, t } from "../ui/dom.js";

const FRONT = /^---[ \t]*\r?\n[\s\S]*?\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|$)/;

function frontMatterRange(doc) {
  const head = doc.sliceString(0, Math.min(doc.length, 20000));
  const m = FRONT.exec(head);
  return m ? { from: 0, to: m[0].length, text: m[0] } : null;
}

// title, subtitle and author from simple front matter lines; enough for a title block.
function titleBlock(yaml) {
  const field = (name) => {
    const m = new RegExp(`^${name}:[ \\t]*(.+)$`, "m").exec(yaml);
    return m ? m[1].trim().replace(/^["']|["']$/g, "") : "";
  };
  let author = field("author");
  if (!author) {
    const list = /^author:[ \t]*\r?\n((?:[ \t]+-[^\n]*\n?)+)/m.exec(yaml);
    if (list) author = list[1].split("\n").map((l) => l.replace(/^[ \t]+-[ \t]*(name:)?/, "").trim()).filter(Boolean).join(", ");
  }
  return { title: field("title"), subtitle: field("subtitle"), author, date: field("date") };
}

class TitleWidget extends WidgetType {
  constructor(text) { super(); this.text = text; }
  eq(other) { return other.text === this.text; }
  toDOM() {
    const info = titleBlock(this.text);
    return el("div", { class: "cm-review-title" },
      info.title ? el("h1", {}, info.title) : null,
      info.subtitle ? el("p", { class: "subtitle" }, info.subtitle) : null,
      info.author || info.date ? el("p", { class: "muted" }, [info.author, info.date].filter(Boolean).join(" · ")) : null);
  }
  ignoreEvent() { return true; }
}

function decorations(state) {
  const range = frontMatterRange(state.doc);
  if (!range || range.to <= range.from) return Decoration.none;
  const trail = range.text.endsWith("\r\n") ? 2 : range.text.endsWith("\n") ? 1 : 0;
  return Decoration.set([Decoration.replace({ widget: new TitleWidget(range.text), block: true })
    .range(range.from, Math.max(range.from, range.to - trail))]);
}

// The front matter becomes a title block.
export const reviewFrontMatter = StateField.define({
  create: (state) => decorations(state),
  update: (value, tr) => (tr.docChanged ? decorations(tr.state) : value),
  provide: (field) => [EditorView.decorations.from(field), EditorView.atomicRanges.of((view) => view.state.field(field))],
});

// A small toolbar for the selection: Comment and Suggest change. With a mouse it sits by the
// selection. On touch screens it is a bar at the bottom of the text instead: the phone's own
// selection handles and menu sit by the selection, and a bar that doesn't move can't lag
// behind them while the selection is dragged.
export class SelectionBubble {
  constructor(view, { onComment, onSuggest }) {
    this.view = view;
    this.docked = window.matchMedia("(pointer: coarse)").matches;
    this.node = el("div", { class: this.docked ? "review-bubble docked" : "review-bubble", role: "toolbar",
                            "aria-label": t("Selection"), hidden: true },
      el("button", { type: "button", onmousedown: (e) => e.preventDefault(), onclick: () => { this.hide(); onComment(); } }, t("Comment")),
      el("button", { type: "button", onmousedown: (e) => e.preventDefault(), onclick: () => { this.hide(); onSuggest(); } }, t("Suggest change")));
    (this.docked ? view.dom.closest(".pane") ?? view.dom : view.dom).append(this.node);
    if (!this.docked) view.scrollDOM.addEventListener("scroll", () => this.update());
  }

  hide() { this.node.hidden = true; }

  update() {
    const sel = this.view.state.selection.main;
    if (sel.empty) { this.hide(); return; }
    if (this.docked) { this.node.hidden = false; return; }
    const end = this.view.coordsAtPos(sel.to, -1) ?? this.view.coordsAtPos(sel.head);
    if (!end) { this.hide(); return; }
    const box = this.view.dom.getBoundingClientRect();
    this.node.hidden = false;
    const width = this.node.offsetWidth;
    const left = Math.min(Math.max(8, end.left - box.left - width / 2), box.width - width - 8);
    this.node.style.left = `${left}px`;
    this.node.style.top = `${end.bottom - box.top + 6}px`;
  }
}

// Figures in the editor (ARCHITECTURE.md §6.3): a paragraph that is only an image,
// ![Caption](figures/map.png){#fig:map}, shows as the picture with its caption to type into, which
// wraps as it grows. The label for cross-references, the file and the actions sit behind a small
// button in its corner. Off the line being edited (or always, in writer mode); "Edit as Markdown"
// shows the source.
import { Decoration, EditorView, StateEffect, StateField, WidgetType } from "codemirror";
import { el, t } from "../ui/dom.js";
import { writerMode } from "./writer-mode.js";

export const FIGURE = /^!\[((?:\\.|[^\]\\])*)\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)(\{[^}]*\})?[ \t]*$/;
const editRaw = StateEffect.define(); // a figure's line to show as source, until the cursor leaves it

export const unescapeCaption = (s) => s.replace(/\\([[\]\\])/g, "$1");
export const escapeCaption = (s) => s.replace(/[\\[\]]/g, (c) => `\\${c}`).replace(/\n/g, " ");

// The figure on a line of text, or null: its caption, path, label, and where each part starts.
export function parseFigure(text) {
  const m = FIGURE.exec(text);
  if (!m) return null;
  const attrs = m[3] ?? "";
  const label = /#(fig:[\w:.-]+)/.exec(attrs)?.[1] ?? "";
  return { alt: m[1], path: m[2], attrs, label, altFrom: 2, altTo: 2 + m[1].length };
}

function scan(doc) {
  const out = [];
  let pos = 0;
  for (const line of doc.iterLines()) {
    if (line.startsWith("![")) {
      const fig = parseFigure(line);
      if (fig) out.push({ from: pos, to: pos + line.length, ...fig });
    }
    pos += line.length + 1;
  }
  return out;
}

// A caption field as tall as its text.
function fit(area) {
  area.style.height = "auto";
  if (area.scrollHeight) area.style.height = `${area.scrollHeight}px`;
}

function assetUrl(projectId, path) {
  if (!projectId || /^[a-z]+:/i.test(path)) return null;
  const clean = path.replace(/^\.\//, "");
  return `/api/projects/${projectId}/assets/${clean.split("/").map(encodeURIComponent).join("/")}`;
}

class FigureWidget extends WidgetType {
  constructor(fig, ctx) {
    super();
    this.fig = fig;
    this.ctx = ctx;
  }

  eq(other) {
    return other.fig.alt === this.fig.alt && other.fig.path === this.fig.path && other.fig.attrs === this.fig.attrs
      && other.ctx.locked === this.ctx.locked;
  }

  // The figure's line now: widgets move with the text.
  line(view, dom) {
    const pos = view.posAtDOM(dom);
    const line = view.state.doc.lineAt(pos);
    const fig = parseFigure(line.text);
    return fig ? { line, fig } : null;
  }

  toDOM(view) {
    const dom = el("figure", { class: "cm-figure" });
    const url = assetUrl(this.ctx.projectId, this.fig.path);
    const picture = el("div", { class: "cm-figure-img" });
    if (url) {
      const img = el("img", { src: url, alt: "", loading: "lazy" });
      img.addEventListener("error", () => picture.replaceChildren(el("span", { class: "muted" }, t("Picture not found: {path}", { path: this.fig.path }))));
      picture.append(img);
    } else {
      picture.append(el("span", { class: "muted" }, this.fig.path));
    }
    // The caption wraps onto as many lines as it needs, under the picture.
    const caption = el("textarea", {
      class: "cm-figure-caption", rows: "1", placeholder: t("Caption"), "aria-label": t("Caption"), disabled: this.ctx.locked,
      oninput: () => {
        fit(caption);
        const at = this.line(view, dom);
        if (!at) return;
        view.dispatch({ changes: { from: at.line.from + at.fig.altFrom, to: at.line.from + at.fig.altTo,
                                   insert: escapeCaption(caption.value) }, userEvent: "input.type" });
      },
      onkeydown: (e) => {
        if (e.key === "Enter" || e.key === "Escape") {
          e.preventDefault();
          const at = this.line(view, dom);
          if (at) view.dispatch({ selection: { anchor: Math.min(view.state.doc.length, at.line.to + 1) }, scrollIntoView: true });
          view.focus();
        }
      },
    });
    caption.value = unescapeCaption(this.fig.alt);
    const label = el("input", {
      class: "cm-figure-label", "aria-label": t("Label for cross-references"), disabled: this.ctx.locked, spellcheck: "false",
      placeholder: t("No label"),
      onchange: () => this.setLabel(view, dom, label.value),
      onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); label.blur(); } },
    });
    label.value = this.fig.label.replace(/^fig:/, "");
    // The label, the file and the actions sit behind one small button in the corner.
    const meta = el("div", { class: "cm-figure-meta", hidden: true },
      el("label", { class: "cm-figure-label-field" }, el("span", {}, t("Label")),
         el("span", { class: "cm-figure-label-input" }, el("span", { class: "muted" }, "fig:"), label)),
      el("span", { class: "muted cm-figure-path", title: this.fig.path }, this.fig.path),
      el("div", { class: "cm-figure-actions" },
         el("button", { type: "button", class: "link", onclick: () => this.raw(view, dom) }, t("Edit as Markdown")),
         this.ctx.locked ? null : el("button", { type: "button", class: "link danger-text", onclick: () => this.remove(view, dom) }, t("Remove"))));
    const more = el("button", {
      type: "button", class: "cm-figure-more", title: t("Label and file"), "aria-label": t("Label and file"), "aria-expanded": "false",
      onclick: () => {
        meta.hidden = !meta.hidden;
        more.setAttribute("aria-expanded", String(!meta.hidden));
      },
    }, el("span", { class: "cm-figure-more-label" }, this.fig.label), el("span", { "aria-hidden": "true" }, "⋯"));
    dom.append(picture, caption, more, meta);
    requestAnimationFrame(() => fit(caption));
    return dom;
  }

  updateDOM(dom) {
    const caption = dom.querySelector(".cm-figure-caption");
    const value = unescapeCaption(this.fig.alt);
    if (caption && caption !== document.activeElement && caption.value !== value) {
      caption.value = value;
      fit(caption);
    }
    const tag = dom.querySelector(".cm-figure-more-label");
    if (tag) tag.textContent = this.fig.label;
    const label = dom.querySelector(".cm-figure-label");
    if (label && label !== document.activeElement) label.value = this.fig.label.replace(/^fig:/, "");
    const img = dom.querySelector(".cm-figure-img img");
    const url = assetUrl(this.ctx.projectId, this.fig.path);
    if (img && url && !img.src.endsWith(url)) img.src = url;
    const path = dom.querySelector(".cm-figure-path");
    path.textContent = this.fig.path;
    path.title = this.fig.path;
    return dom.querySelector(".cm-figure-caption")?.disabled === this.ctx.locked;
  }

  setLabel(view, dom, value) {
    const at = this.line(view, dom);
    if (!at) return;
    const id = value.trim().replace(/^fig:/, "").replace(/[^\w:.-]+/g, "-").replace(/^-+|-+$/g, "");
    const text = at.line.text.trimEnd();
    const attrs = at.fig.attrs;
    let next;
    if (!attrs) next = id ? `${text}{#fig:${id}}` : text;
    else if (/#fig:[\w:.-]+/.test(attrs)) {
      next = text.slice(0, text.length - attrs.length) + (id ? attrs.replace(/#fig:[\w:.-]+/, `#fig:${id}`)
        : attrs.replace(/\s*#fig:[\w:.-]+\s*/, " ").replace(/^\{\s*\}$/, "").replace(/\{\s+/, "{"));
    } else {
      next = text.slice(0, text.length - attrs.length) + (id ? attrs.replace(/^\{/, `{#fig:${id} `).replace(/ \}$/, "}") : attrs);
    }
    if (next !== at.line.text) view.dispatch({ changes: { from: at.line.from, to: at.line.to, insert: next }, userEvent: "input" });
  }

  raw(view, dom) {
    const at = this.line(view, dom);
    if (!at) return;
    view.dispatch({ effects: editRaw.of(at.line.from), selection: { anchor: at.line.from + at.fig.altFrom }, scrollIntoView: true });
    view.focus();
  }

  remove(view, dom) {
    const at = this.line(view, dom);
    if (!at) return;
    const doc = view.state.doc;
    const next = at.line.number < doc.lines ? doc.line(at.line.number + 1) : null;
    const to = next && !next.text.trim() ? Math.min(doc.length, next.to + 1) : at.line.to;
    view.dispatch({ changes: { from: at.line.from, to, insert: "" }, selection: { anchor: at.line.from }, userEvent: "delete" });
    view.focus();
  }

  ignoreEvent() { return true; }

  get estimatedHeight() { return 260; }
}

// The figures of a Markdown document. projectId: for the pictures; locked(state): no typing.
export function figures({ projectId, locked = () => false }) {
  const build = (state, figs, rawAt) => {
    const writer = writerMode(state);
    const sel = state.selection.ranges;
    const ctx = { projectId, locked: locked(state) };
    const out = [];
    for (const fig of figs) {
      if (fig.from === rawAt) continue;
      if (!writer && sel.some((r) => r.from <= fig.to && r.to >= fig.from)) continue;
      out.push(Decoration.replace({ widget: new FigureWidget(fig, ctx), block: true }).range(fig.from, fig.to));
    }
    return Decoration.set(out);
  };
  const field = StateField.define({
    create(state) {
      const figs = scan(state.doc);
      return { figs, rawAt: null, deco: build(state, figs, null) };
    },
    update(value, tr) {
      let { figs, rawAt } = value;
      if (tr.docChanged) {
        figs = scan(tr.state.doc);
        rawAt = rawAt === null ? null : tr.changes.mapPos(rawAt);
      }
      for (const e of tr.effects) if (e.is(editRaw)) rawAt = e.value;
      // The source stays shown until the cursor leaves the line.
      if (rawAt !== null) {
        const line = tr.state.doc.lineAt(Math.min(rawAt, tr.state.doc.length));
        if (!tr.state.selection.ranges.some((r) => r.from <= line.to && r.to >= line.from)) rawAt = null;
      }
      const relevant = tr.docChanged || tr.selection || tr.effects.length || rawAt !== value.rawAt;
      if (!relevant) return value;
      return { figs, rawAt, deco: build(tr.state, figs, rawAt) };
    },
    provide: (f) => [EditorView.decorations.from(f, (v) => v.deco),
                     EditorView.atomicRanges.of((view) => view.state.field(f).deco)],
  });
  return field;
}

// Everything with a label to refer to, in order and numbered: figures, tables, equations, sections.
export function crossRefTargets(text) {
  const out = [];
  const count = { fig: 0, tbl: 0, eq: 0, sec: 0 };
  const lines = text.split("\n");
  let fence = null;
  lines.forEach((line, i) => {
    const f = /^(```+|~~~+)/.exec(line);
    if (f) { fence = fence === null ? f[1][0] : (line.startsWith(fence) ? null : fence); return; }
    if (fence) return;
    for (const m of line.matchAll(/\{[^}]*#((fig|tbl|eq|sec):[\w:.-]+)[^}]*\}/g)) {
      const kind = m[2];
      count[kind] += 1;
      let caption = "";
      if (kind === "fig") caption = unescapeCaption(parseFigure(line)?.alt ?? "");
      else if (kind === "tbl") caption = line.replace(/^:\s*/, "").replace(m[0], "").trim();
      else if (kind === "sec") caption = line.replace(/^#+\s*/, "").replace(m[0], "").trim();
      else caption = line.replace(m[0], "").replace(/\$\$/g, "").trim().slice(0, 60);
      out.push({ id: m[1], kind, n: count[kind], caption, line: i + 1 });
    }
  });
  return out;
}

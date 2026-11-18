// Inserting without punctuation (ARCHITECTURE.md §6.3): small popovers for links (Text and
// Address), footnotes (written in a card, shown in the text as their number), tables (a grid
// to pick the size from) and cross-references (a list of the document's figures, tables,
// equations and sections). Each one writes ordinary Pandoc Markdown.
import { EditorSelection, EditorView, keymap, syntaxTree } from "codemirror";
import { el, t } from "../ui/dom.js";
import { crossRefTargets } from "./figures.js";
import { insertTable } from "./tables.js";

// -- Popover ---------------------------------------------------------------
// Anchored under a button or the cursor; on phones, a sheet at the bottom of the editor.
export class Popover {
  constructor(host) {
    this.host = host;
    this.node = null;
    this.onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); this.close(true); } };
    this.onDown = (e) => {
      if (this.node && !this.node.contains(e.target) && !this.anchorEl?.contains(e.target)) this.close();
    };
  }

  get isOpen() { return Boolean(this.node); }

  open({ anchor, content, label, onClose = null, wide = false, focus = true }) {
    this.close();
    this.anchorEl = anchor instanceof Element ? anchor : null;
    this.onClose = onClose;
    const sheet = window.matchMedia("(max-width: 640px)").matches;
    this.node = el("div", { class: `dw-popover${sheet ? " sheet" : ""}${wide ? " wide" : ""}`, role: "dialog",
                            "aria-label": label }, content);
    this.host.append(this.node);
    if (!sheet) {
      const box = this.host.getBoundingClientRect();
      const r = anchor instanceof Element ? anchor.getBoundingClientRect() : anchor;
      const width = this.node.offsetWidth;
      const left = Math.max(8, Math.min(r.left - box.left, box.width - width - 8));
      let top = r.bottom - box.top + 6;
      if (top + this.node.offsetHeight > box.height - 8 && r.top - box.top - this.node.offsetHeight - 6 > 8) {
        top = r.top - box.top - this.node.offsetHeight - 6;
      }
      this.node.style.left = `${left}px`;
      this.node.style.top = `${Math.max(8, top)}px`;
    }
    this.node.addEventListener("keydown", this.onKey);
    document.addEventListener("mousedown", this.onDown, true);
    document.addEventListener("touchstart", this.onDown, true);
    if (focus) this.node.querySelector("input, textarea, button")?.focus();
    return this.node;
  }

  close(refocus = false) {
    if (!this.node) return;
    this.node.remove();
    this.node = null;
    document.removeEventListener("mousedown", this.onDown, true);
    document.removeEventListener("touchstart", this.onDown, true);
    const done = this.onClose;
    this.onClose = null;
    done?.(refocus);
  }
}

// Where the cursor is on screen, for a popover opened from the keyboard.
function cursorRect(view) {
  const pos = view.state.selection.main.head;
  const c = view.coordsAtPos(pos) ?? view.coordsAtPos(Math.max(0, pos - 1));
  if (c) return c;
  const r = view.contentDOM.getBoundingClientRect();
  return { left: r.left + 16, right: r.left + 16, top: r.top, bottom: r.top + 20 };
}

function nodeAt(state, pos, name) {
  for (let cur = syntaxTree(state).resolveInner(pos, -1); cur; cur = cur.parent) {
    if (cur.name === name) return cur;
  }
  for (let cur = syntaxTree(state).resolveInner(pos, 1); cur; cur = cur.parent) {
    if (cur.name === name) return cur;
  }
  return null;
}

const buttons = (...items) => el("div", { class: "buttons" }, ...items);

// An address as typed: example.org/page becomes https://example.org/page.
export function normalizeUrl(raw) {
  const url = raw.trim();
  if (!url) return "";
  if (/^[a-z][a-z0-9+.-]*:/i.test(url) || url.startsWith("#") || url.startsWith("/")) return url;
  if (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(url)) return `mailto:${url}`;
  if (/^[\w-]+(\.[\w-]+)+(\/|$|:\d)/.test(url) || /^[\w-]+(\.[\w-]+)+$/.test(url)) return `https://${url}`;
  return url;
}

// A footnote's text can hold citations and links ([@doe], [text](url)); a ] without its [ would
// end the note early, so only those are escaped.
export function balanceBrackets(text) {
  const chars = [...text];
  const open = [];
  const escape = new Set();
  for (let i = 0; i < chars.length; i++) {
    if (chars[i] === "\\") { i += 1; continue; }
    if (chars[i] === "[") open.push(i);
    else if (chars[i] === "]") {
      if (open.length) open.pop();
      else escape.add(i);
    }
  }
  for (const i of open) escape.add(i);
  return chars.map((c, i) => (escape.has(i) ? `\\${c}` : c)).join("");
}

const linkText = (s) => s.replace(/([[\]\\])/g, "\\$1");
const linkUrl = (s) => s.replace(/[()\s<>]/g, (c) => `%${c.charCodeAt(0).toString(16).toUpperCase().padStart(2, "0")}`);

export class InsertTools {
  constructor({ view, host, readOnly = () => false }) {
    this.view = view;
    this.pop = new Popover(host);
    this.readOnly = readOnly;
  }

  // Ctrl+K opens the link popover; clicks on footnote numbers open the footnote card; in writer
  // mode, a click on a link opens the link popover.
  extension({ writer = () => false } = {}) {
    return [
      keymap.of([{ key: "Mod-k", run: (view) => { this.view = view; this.link(); return true; } }]),
      EditorView.domEventHandlers({
        click: (event, view) => {
          this.view = view;
          if (!writer(view.state) || event.button !== 0 || this.readOnly(view.state)) return false;
          const link = event.target.closest?.(".cm-md-link");
          if (!link) return false;
          const pos = view.posAtDOM(link);
          if (!nodeAt(view.state, pos + 1, "Link")) return false;
          setTimeout(() => this.link(link), 0);
          return false;
        },
      }),
    ];
  }

  // Notes, from the live preview's numbers.
  listen(dom) {
    dom.addEventListener("dw:note", (e) => this.note({ from: e.detail.from, to: e.detail.to, anchor: e.target }));
  }

  done(refocus) {
    if (refocus !== false) this.view.focus();
  }

  // -- Links ----------------------------------------------------------------
  link(anchor = null) {
    const { view } = this;
    if (this.readOnly(view.state)) return;
    const { state } = view;
    const range = state.selection.main;
    let target = { from: range.from, to: range.to, text: state.sliceDoc(range.from, range.to), url: "" };
    const node = nodeAt(state, range.head, "Link");
    let existing = false;
    if (node && node.from <= range.from && range.to <= node.to) {
      const marks = [];
      for (let c = node.firstChild; c; c = c.nextSibling) if (c.name === "LinkMark") marks.push(c);
      const url = node.getChild("URL");
      if (marks.length >= 2) {
        target = { from: node.from, to: node.to, text: state.sliceDoc(marks[0].to, marks[1].from),
                   url: url ? state.sliceDoc(url.from, url.to) : "" };
        existing = true;
      }
    } else if (/^\s*(https?:\/\/|www\.)\S+\s*$/i.test(target.text)) {
      target.url = target.text.trim();
    }
    const text = el("input", { name: "text", value: target.text.replace(/\\([[\]\\])/g, "$1"), autocomplete: "off" });
    const url = el("input", { name: "url", value: target.url, type: "url", inputmode: "url", autocomplete: "off",
                              placeholder: "https://", spellcheck: "false" });
    const apply = () => {
      const href = normalizeUrl(url.value);
      const label = text.value.trim() || href;
      const now = view.state;
      // The text around may have changed while the popover was open: map the range.
      const insert = href ? `[${linkText(label)}](${linkUrl(href)})` : linkText(label);
      const from = Math.min(target.from, now.doc.length);
      const to = Math.min(target.to, now.doc.length);
      view.dispatch({ changes: { from, to, insert }, selection: EditorSelection.cursor(from + insert.length),
                      scrollIntoView: true, userEvent: "input" });
      this.pop.close(true);
    };
    const form = el("form", { class: "form dw-link-form", novalidate: true, onsubmit: (e) => { e.preventDefault(); apply(); } },
      el("label", {}, t("Text"), text),
      el("label", {}, t("Address"), url),
      buttons(
        existing ? el("button", { type: "button", class: "danger", onclick: () => {
          const insert = linkText(text.value);
          view.dispatch({ changes: { from: target.from, to: target.to, insert }, userEvent: "input" });
          this.pop.close(true);
        } }, t("Remove link")) : null,
        existing && target.url && /^(https?|mailto):/i.test(target.url)
          ? el("a", { class: "button", href: target.url, target: "_blank", rel: "noopener noreferrer" }, t("Open")) : null,
        el("button", { type: "button", onclick: () => this.pop.close(true) }, t("Cancel")),
        el("button", { type: "submit", class: "primary" }, existing ? t("Save") : t("Add link"))));
    this.pop.open({ anchor: anchor ?? cursorRect(view), content: form, label: t("Link"), onClose: (r) => this.done(r) });
    (target.text && !target.url ? url : text).focus();
  }

  // -- Footnotes -------------------------------------------------------------
  // A new footnote at the cursor, or the one between from and to.
  note({ from = null, to = null, anchor = null } = {}) {
    const { view } = this;
    const locked = this.readOnly(view.state);
    let existing = from !== null;
    if (!existing) {
      const node = nodeAt(view.state, view.state.selection.main.head, "InlineNote");
      if (node) { from = node.from; to = node.to; existing = true; }
    }
    if (!existing && locked) return;
    const body = existing ? view.state.sliceDoc(from + 2, Math.max(from + 2, to - 1)) : "";
    const n = existing ? this.noteNumber(from) : null;
    const area = el("textarea", { rows: 4, "aria-label": t("Footnote text"), placeholder: t("Footnote text"), disabled: locked });
    area.value = body;
    const save = () => {
      const text = balanceBrackets(area.value.trim().replace(/\n{2,}/g, "\n"));
      if (existing) {
        if (text) view.dispatch({ changes: { from: from + 2, to: to - 1, insert: text }, userEvent: "input" });
        else view.dispatch({ changes: { from, to, insert: "" }, userEvent: "delete" });
      } else if (text) {
        const pos = view.state.selection.main.to;
        const insert = `^[${text}]`;
        view.dispatch({ changes: { from: pos, insert }, selection: EditorSelection.cursor(pos + insert.length),
                        scrollIntoView: true, userEvent: "input" });
      }
      this.pop.close(true);
    };
    const form = el("form", { class: "form dw-note-form", onsubmit: (e) => { e.preventDefault(); save(); } },
      el("div", { class: "dw-note-head" }, el("strong", {}, n ? t("Footnote {n}", { n }) : t("New footnote"))),
      area,
      el("p", { class: "muted hint" }, t("Shown in the text as its number. It prints at the foot of the page.")),
      locked ? buttons(el("button", { type: "button", onclick: () => this.pop.close(true) }, t("Close")))
        : buttons(
          existing ? el("button", { type: "button", class: "danger", onclick: () => {
            view.dispatch({ changes: { from, to, insert: "" }, userEvent: "delete" });
            this.pop.close(true);
          } }, t("Delete")) : null,
          el("button", { type: "button", onclick: () => this.pop.close(true) }, t("Cancel")),
          el("button", { type: "submit", class: "primary" }, existing ? t("Save") : t("Add footnote"))));
    area.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); save(); }
    });
    this.pop.open({ anchor: anchor ?? cursorRect(view), content: form, label: t("Footnote"), wide: true,
                    onClose: (r) => this.done(r) });
    area.focus();
  }

  noteNumber(from) {
    let n = 0;
    syntaxTree(this.view.state).iterate({ to: from + 1, enter(node) {
      if (node.name === "InlineNote") { if (node.from <= from) n += 1; return false; }
      return undefined;
    } });
    return n || 1;
  }

  // -- Tables ------------------------------------------------------------------
  table(anchor) {
    const { view } = this;
    if (this.readOnly(view.state)) return;
    const MAX = 8;
    const label = el("p", { class: "muted dw-grid-label", "aria-live": "polite" }, t("Pick a size"));
    const cells = [];
    const grid = el("div", { class: "dw-grid", role: "grid", "aria-label": t("Table size") });
    const mark = (r, c) => {
      for (const cell of cells) cell.classList.toggle("on", cell.dataset.r <= r && cell.dataset.c <= c);
      label.textContent = t("{cols} × {rows} table", { cols: c, rows: r });
    };
    const pick = (r, c) => {
      this.pop.close(false);
      insertTable(view, r, c, (i) => t("Column {n}", { n: i + 1 }));
      view.focus();
    };
    for (let r = 1; r <= MAX; r++) {
      for (let c = 1; c <= MAX; c++) {
        const cell = el("button", { type: "button", class: "dw-grid-cell", "data-r": r, "data-c": c,
                                    "aria-label": t("{cols} × {rows} table", { cols: c, rows: r }),
                                    onmouseenter: () => mark(r, c), onfocus: () => mark(r, c), onclick: () => pick(r, c) });
        cells.push(cell);
        grid.append(cell);
      }
    }
    grid.addEventListener("keydown", (e) => {
      const at = cells.indexOf(document.activeElement);
      if (at < 0) return;
      const step = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: MAX, ArrowUp: -MAX }[e.key];
      if (step && cells[at + step]) { e.preventDefault(); cells[at + step].focus(); }
    });
    const content = el("div", { class: "dw-table-pick" }, grid, label,
      el("p", { class: "muted hint" }, t("The first row is the header. In a table, Tab goes to the next cell.")));
    this.pop.open({ anchor, content, label: t("Insert table"), onClose: (r) => this.done(r) });
    mark(3, 3);
    cells[2 * MAX + 2].focus();
  }

  // -- Cross-references ---------------------------------------------------------
  crossRef(anchor) {
    const { view } = this;
    if (this.readOnly(view.state)) return;
    const KIND = { fig: t("Figure"), tbl: t("Table"), eq: t("Equation"), sec: t("Section") };
    const targets = crossRefTargets(view.state.doc.toString());
    const insert = (id) => {
      const pos = view.state.selection.main;
      const text = `[@${id}]`;
      view.dispatch({ changes: { from: pos.from, to: pos.to, insert: text },
                      selection: EditorSelection.cursor(pos.from + text.length), scrollIntoView: true, userEvent: "input" });
      this.pop.close(true);
    };
    const content = targets.length
      ? el("div", { class: "dw-xref" },
        el("p", { class: "muted hint" }, t("Refer to it by its number, such as “Figure 2”. The number follows the document.")),
        el("ul", { class: "plain dw-xref-list" }, targets.map((x) => el("li", {},
          el("button", { type: "button", onclick: () => insert(x.id) },
            el("strong", {}, `${KIND[x.kind]} ${x.n}`), " ",
            el("span", { class: "muted" }, x.caption || x.id))))))
      : el("div", { class: "dw-xref" },
        el("p", {}, t("Nothing to refer to yet.")),
        el("p", { class: "muted hint" }, t("Figures get a label when you add a picture. Headings, tables and equations get one with {#sec:name}, {#tbl:name} or {#eq:name} after them.")));
    this.pop.open({ anchor, content, label: t("Cross-reference"), wide: true, onClose: (r) => this.done(r) });
  }

  // -- The Insert menu ------------------------------------------------------------
  menu(anchor, items) {
    const content = el("div", { class: "actions-list dw-insert-menu", role: "menu" },
      items.map(([label, hint, run]) => el("button", { type: "button", role: "menuitem",
                                                        onclick: () => { this.pop.close(false); run(anchor); } },
        el("span", {}, label), hint ? el("span", { class: "muted dw-menu-hint" }, hint) : null)));
    this.pop.open({ anchor, content, label: t("Insert"), onClose: (r) => this.done(r) });
  }
}

// The grid for data sheets (ARCHITECTURE.md §6.3): .csv and .tsv documents open as a table to
// type into, like a spreadsheet. The text stays the document: every change in the grid is an
// ordinary edit of the CSV, as small as the cells it touches, so collaboration, history and
// undo work as for any text, and others' edits show in the grid as they arrive.
//
// Keys: arrows move, Enter or F2 or typing edits, Enter and Tab commit and move on, Escape
// cancels, Delete clears, Shift+arrows select, Ctrl+C / Ctrl+V copy and paste (tab-separated,
// as Excel, LibreOffice and Google Sheets put it on the clipboard), Ctrl+Z undoes.
// Formulas start with = (SUM, AVERAGE, MIN, MAX, COUNT, ROUND, ABS, + - * / ^, A1, A1:B9).
import { redo, undo } from "codemirror";
import { api, el, t, toast } from "../ui/dom.js";
import { Popover } from "./insert-tools.js";
import {
  FUNCTIONS, SheetValues, colName, formatNumber, minimalChange, parseClipboard, parseSheet,
  separatorFor, serializeSheet, setCell, toClipboard, toNumber,
} from "./sheet-model.js";

const MAX_SHOWN = 5000;

export class SheetView {
  constructor({ view, host, path, docId, readOnly = false, canEdit = !readOnly }) {
    this.view = view;
    this.path = path;
    this.docId = docId;
    this.readOnly = readOnly;
    this.canEdit = canEdit;
    this.sep = separatorFor(view.state.doc.toString(), path);
    this.active = { r: 0, c: 0 };
    this.anchor = null; // the other corner of a selected range
    this.editing = null;
    this.pop = new Popover(host);

    this.address = el("span", { class: "dw-sheet-address", "aria-live": "polite" });
    this.formula = el("input", { class: "dw-sheet-formula", "aria-label": t("Cell contents"), spellcheck: "false",
                                 autocomplete: "off", disabled: readOnly });
    this.summary = el("span", { class: "dw-sheet-summary muted", "aria-live": "polite" });
    this.table = el("table", { class: "dw-sheet-table", role: "grid", "aria-label": t("Data sheet") });
    this.scroller = el("div", { class: "dw-sheet-scroll", tabindex: "0" }, this.table);
    const button = (label, title, run, cls = "") => el("button", { type: "button", title, class: cls,
                                                                    onclick: run, disabled: readOnly }, label);
    this.toolbar = el("div", { class: "dw-sheet-bar", role: "toolbar", "aria-label": t("Sheet") },
      button(`+ ${t("Row")}`, t("Add a row below"), () => this.insertRow()),
      button(`+ ${t("Column")}`, t("Add a column to the right"), () => this.insertColumn()),
      button(`− ${t("Row")}`, t("Delete the row"), () => this.deleteRow()),
      button(`− ${t("Column")}`, t("Delete the column"), () => this.deleteColumn()),
      button(t("Sort"), t("Sort the rows below the header by this column"), () => this.sort()),
      button("Σ", t("Insert a formula: sum, average, minimum, maximum, count"), (e) => this.formulaMenu(e.currentTarget)),
      el("span", { class: "spacer" }),
      el("button", { type: "button", title: t("Keep this sheet up to date from a web address"),
                     onclick: (e) => this.sourceDialog(e.currentTarget) }, t("Web source")));
    this.node = el("div", { class: "dw-sheet" }, this.toolbar,
      el("div", { class: "dw-sheet-formula-bar" }, this.address, this.formula), this.scroller, this.summary);
    host.prepend(this.node);
    this.wire();
    this.refresh();
  }

  // -- Model ------------------------------------------------------------------------
  text() { return this.view.state.doc.toString(); }

  refresh() {
    const text = this.text();
    this.rows = parseSheet(text, this.sep);
    this.values = new SheetValues(this.rows);
    this.trailing = text.endsWith("\n") || !text;
    this.render();
  }

  get width() { return Math.max(1, ...this.rows.map((r) => r.length)); }

  // Applies a change to the rows as one edit of the text.
  commit(mutate, userEvent = "input") {
    if (this.readOnly) return;
    const before = this.text();
    const rows = parseSheet(before, this.sep);
    mutate(rows);
    const after = serializeSheet(rows, this.sep, this.trailing || rows.length > 0);
    const change = minimalChange(before, after);
    if (change) this.view.dispatch({ changes: change, userEvent });
  }

  // -- Rendering --------------------------------------------------------------------
  render() {
    const width = this.width + (this.readOnly ? 0 : 1);
    const height = Math.min(this.rows.length + (this.readOnly ? 0 : 1), MAX_SHOWN);
    const head = el("tr", {}, el("th", { class: "dw-sheet-corner", "aria-hidden": "true" }),
      ...Array.from({ length: width }, (_, c) => el("th", { scope: "col", "data-c": c }, colName(c + 1))));
    const body = [];
    for (let r = 0; r < height; r++) {
      const cells = [el("th", { scope: "row", "data-r": r }, String(r + 1))];
      for (let c = 0; c < width; c++) {
        const raw = this.values.raw(r, c);
        const shown = this.values.display(r, c);
        const isFormula = raw.length > 1 && raw[0] === "=";
        const numeric = toNumber(shown) !== null;
        const cls = ["dw-cell", r === 0 ? "dw-head" : "", numeric ? "num" : "", isFormula ? "formula" : "",
                     /^#/.test(shown) && isFormula ? "error" : ""].filter(Boolean).join(" ");
        cells.push(el("td", { class: cls, role: "gridcell", "data-r": r, "data-c": c, title: isFormula ? raw : null },
          shown));
      }
      body.push(el("tr", {}, cells));
    }
    this.table.replaceChildren(el("thead", {}, head), el("tbody", {}, body));
    if (this.rows.length > MAX_SHOWN) {
      this.table.append(el("caption", { class: "muted" },
        t("Showing the first {n} rows. Edit the rest as text.", { n: MAX_SHOWN })));
    }
    this.select(this.active.r, this.active.c, { keepAnchor: true, scroll: false });
  }

  cellNode(r, c) { return this.table.querySelector(`td[data-r="${r}"][data-c="${c}"]`); }

  range() {
    const a = this.anchor ?? this.active;
    return { r1: Math.min(a.r, this.active.r), r2: Math.max(a.r, this.active.r),
             c1: Math.min(a.c, this.active.c), c2: Math.max(a.c, this.active.c) };
  }

  select(r, c, { extend = false, keepAnchor = false, scroll = true } = {}) {
    const maxR = Math.min(this.rows.length + (this.readOnly ? -1 : 0), MAX_SHOWN - 1);
    r = Math.max(0, Math.min(r, Math.max(0, maxR)));
    c = Math.max(0, Math.min(c, this.width - (this.readOnly ? 1 : 0)));
    if (extend) this.anchor = this.anchor ?? { ...this.active };
    else if (!keepAnchor) this.anchor = null;
    this.active = { r, c };
    for (const node of this.table.querySelectorAll(".sel, .active")) node.classList.remove("sel", "active");
    const { r1, r2, c1, c2 } = this.range();
    for (let i = r1; i <= r2; i++) {
      for (let j = c1; j <= c2; j++) this.cellNode(i, j)?.classList.add("sel");
    }
    const node = this.cellNode(r, c);
    node?.classList.add("active");
    if (scroll) node?.scrollIntoView({ block: "nearest", inline: "nearest" });
    const ref = `${colName(c + 1)}${r + 1}`;
    this.address.textContent = r1 === r2 && c1 === c2 ? ref
      : `${colName(c1 + 1)}${r1 + 1}:${colName(c2 + 1)}${r2 + 1}`;
    if (document.activeElement !== this.formula) this.formula.value = this.values.raw(r, c);
    this.summarize();
  }

  // Sum, average and count of the selected numbers, as spreadsheet programs show them.
  summarize() {
    const { r1, r2, c1, c2 } = this.range();
    if (r1 === r2 && c1 === c2) { this.summary.textContent = ""; return; }
    const nums = [];
    for (let r = r1; r <= r2; r++) {
      for (let c = c1; c <= c2; c++) {
        const v = this.values.value(r, c);
        if (typeof v === "number") nums.push(v);
      }
    }
    if (!nums.length) { this.summary.textContent = ""; return; }
    const sum = nums.reduce((a, b) => a + b, 0);
    this.summary.textContent = t("Sum {sum} · Average {avg} · Count {n}",
      { sum: formatNumber(sum), avg: formatNumber(sum / nums.length), n: nums.length });
  }

  // -- Editing ----------------------------------------------------------------------
  startEdit(initial = null) {
    if (this.readOnly || this.editing) return;
    const { r, c } = this.active;
    const node = this.cellNode(r, c);
    if (!node) return;
    const input = el("input", { class: "dw-cell-input", "aria-label": `${colName(c + 1)}${r + 1}`, spellcheck: "false",
                                autocomplete: "off" });
    input.value = initial ?? this.values.raw(r, c);
    node.classList.add("editing");
    node.replaceChildren(input);
    this.editing = { r, c, input, node };
    input.focus();
    if (initial === null) input.select();
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); this.finishEdit(true); this.move(e.shiftKey ? -1 : 1, 0); }
      else if (e.key === "Tab") { e.preventDefault(); this.finishEdit(true); this.move(0, e.shiftKey ? -1 : 1); }
      else if (e.key === "Escape") { e.preventDefault(); this.finishEdit(false); }
      else if ((e.key === "ArrowUp" || e.key === "ArrowDown") && initial !== null) {
        e.preventDefault();
        this.finishEdit(true);
        this.move(e.key === "ArrowUp" ? -1 : 1, 0);
      }
    });
    input.addEventListener("input", () => { this.formula.value = input.value; });
    input.addEventListener("blur", () => { if (this.editing?.input === input) this.finishEdit(true, false); });
  }

  finishEdit(save, refocus = true) {
    const editing = this.editing;
    if (!editing) return;
    this.editing = null;
    const value = editing.input.value;
    if (save && value !== this.values.raw(editing.r, editing.c)) this.setValue(editing.r, editing.c, value);
    else this.refresh();
    if (refocus) this.scroller.focus({ preventScroll: true });
  }

  setValue(r, c, value) {
    this.commit((rows) => setCell(rows, r, c, value, this.sep));
  }

  move(dr, dc, extend = false) {
    this.select(this.active.r + dr, this.active.c + dc, { extend });
  }

  clear() {
    const { r1, r2, c1, c2 } = this.range();
    this.commit((rows) => {
      for (let r = r1; r <= r2; r++) {
        for (let c = c1; c <= c2; c++) if (rows[r]?.[c] && rows[r][c].value !== "") setCell(rows, r, c, "", this.sep);
      }
    }, "delete");
  }

  insertRow() {
    const at = this.active.r + 1;
    this.commit((rows) => { while (rows.length < at) rows.push([]); rows.splice(at, 0, []); });
    this.select(at, this.active.c);
  }

  insertColumn() {
    const at = this.active.c + 1;
    this.commit((rows) => {
      for (const row of rows) if (row.length >= at) row.splice(at, 0, { value: "", src: "" });
    });
    this.select(this.active.r, at);
  }

  deleteRow() {
    const { r1, r2 } = this.range();
    this.commit((rows) => rows.splice(r1, r2 - r1 + 1), "delete");
    this.select(r1, this.active.c);
  }

  deleteColumn() {
    const { c1, c2 } = this.range();
    this.commit((rows) => { for (const row of rows) row.splice(c1, c2 - c1 + 1); }, "delete");
    this.select(this.active.r, c1);
  }

  // Rows below the header by the active column: numbers by value, text alphabetically; again to reverse.
  sort() {
    const c = this.active.c;
    const desc = this.lastSort?.c === c && !this.lastSort.desc;
    this.lastSort = { c, desc };
    const values = this.values;
    this.commit((rows) => {
      const keyed = rows.slice(1).map((row, i) => ({ row, v: values.value(i + 1, c) }));
      keyed.sort((a, b) => {
        const an = typeof a.v === "number";
        const bn = typeof b.v === "number";
        let d;
        if (an && bn) d = a.v - b.v;
        else if (an !== bn) d = an ? -1 : 1;
        else d = String(a.v).localeCompare(String(b.v), undefined, { numeric: true, sensitivity: "base" });
        return desc ? -d : d;
      });
      // Formulas refer to cells by position, so sorted rows keep them as written.
      rows.splice(1, rows.length - 1, ...keyed.map((k) => k.row));
    });
    toast(desc ? t("Sorted largest first") : t("Sorted smallest first"));
  }

  formulaMenu(anchor) {
    if (this.readOnly) return;
    const { r1, r2, c1, c2 } = this.range();
    // Without a selected range: the numbers above the active cell.
    let ref;
    if (r1 !== r2 || c1 !== c2) ref = `${colName(c1 + 1)}${r1 + 1}:${colName(c2 + 1)}${r2 + 1}`;
    else ref = `${colName(c1 + 1)}2:${colName(c1 + 1)}${Math.max(2, r1)}`;
    const target = r1 !== r2 || c1 !== c2 ? { r: r2 + 1, c: c1 } : { r: r1, c: c1 };
    const items = FUNCTIONS.filter((f) => !["ROUND", "ABS"].includes(f)).map((f) => el("button", {
      type: "button", role: "menuitem", onclick: () => {
        this.pop.close(false);
        this.setValue(target.r, target.c, `=${f}(${ref})`);
        this.select(target.r, target.c);
        this.scroller.focus();
      } }, el("span", {}, t({ SUM: "Sum", AVERAGE: "Average", MIN: "Minimum", MAX: "Maximum", COUNT: "Count" }[f])),
    el("span", { class: "muted dw-menu-hint" }, `=${f}(${ref})`)));
    this.pop.open({ anchor, label: t("Formula"), content: el("div", { class: "actions-list dw-insert-menu", role: "menu" },
      items, el("p", { class: "muted hint" }, t("Or type = and a formula into any cell, such as =B2*1.19 or =AVERAGE(B2:B9).")))) });
  }

  // -- Clipboard --------------------------------------------------------------------
  copy(event) {
    const { r1, r2, c1, c2 } = this.range();
    const out = [];
    for (let r = r1; r <= r2; r++) {
      const row = [];
      for (let c = c1; c <= c2; c++) row.push(this.values.display(r, c));
      out.push(row);
    }
    event.clipboardData.setData("text/plain", toClipboard(out));
    event.preventDefault();
  }

  paste(event) {
    if (this.readOnly) return;
    const text = event.clipboardData.getData("text/plain");
    if (!text) return;
    event.preventDefault();
    const cells = parseClipboard(text);
    const { r, c } = this.active;
    this.commit((rows) => {
      cells.forEach((row, i) => row.forEach((value, j) => setCell(rows, r + i, c + j, value, this.sep)));
    }, "input.paste");
    this.anchor = { r, c };
    this.select(r + cells.length - 1, c + Math.max(0, ...cells.map((x) => x.length)) - 1, { keepAnchor: true });
  }

  // -- Web source -------------------------------------------------------------------
  async sourceDialog(anchor) {
    let current = { url: null };
    try { current = await api("GET", `/api/documents/${this.docId}/source`); } catch { /* offline */ }
    const url = el("input", { type: "url", name: "url", placeholder: "https://example.org/data.csv", value: current.url ?? "",
                              disabled: !this.canEdit, inputmode: "url", spellcheck: "false" });
    const status = el("p", { class: "muted hint", role: "status" },
      current.refreshedAt ? t("Last refreshed {when}: {status}", { when: current.refreshedAt, status: current.status ?? "" })
        : t("The sheet is replaced by what the address gives when you press Refresh. Changes made here are then overwritten."));
    const save = async () => {
      try {
        await api("PUT", `/api/documents/${this.docId}/source`, { url: url.value.trim() });
      } catch (e) { status.textContent = e.message; throw e; }
    };
    const refresh = async () => {
      try {
        if (url.value.trim() !== (current.url ?? "")) await save();
        if (!url.value.trim()) { this.pop.close(true); return; }
        status.textContent = t("Fetching…");
        const res = await api("POST", `/api/documents/${this.docId}/source/refresh`);
        this.pop.close(true);
        toast(res.changed ? t("Updated: {n} rows", { n: res.rows }) : t("Already up to date"));
      } catch (e) { status.textContent = e.message; }
    };
    const content = el("form", { class: "form", onsubmit: (e) => { e.preventDefault(); refresh(); } },
      el("label", {}, t("Web address of a CSV file"), url), status,
      el("div", { class: "buttons" },
        el("button", { type: "button", onclick: () => this.pop.close(true) }, t("Close")),
        this.canEdit ? el("button", { type: "button", onclick: async () => { await save(); this.pop.close(true); } },
          t("Save")) : null,
        this.canEdit ? el("button", { type: "submit", class: "primary" }, t("Refresh now")) : null));
    this.pop.open({ anchor, content, label: t("Web source"), wide: true });
  }

  // -- Events -----------------------------------------------------------------------
  wire() {
    this.table.addEventListener("mousedown", (e) => {
      const td = e.target.closest("td[data-r]");
      if (!td || td.classList.contains("editing")) return;
      e.preventDefault();
      if (this.editing) this.finishEdit(true, false);
      this.select(Number(td.dataset.r), Number(td.dataset.c), { extend: e.shiftKey });
      this.scroller.focus({ preventScroll: true });
      this.dragging = true;
    });
    this.table.addEventListener("mouseover", (e) => {
      if (!this.dragging) return;
      const td = e.target.closest("td[data-r]");
      if (td) this.select(Number(td.dataset.r), Number(td.dataset.c), { extend: true, scroll: false });
    });
    document.addEventListener("mouseup", () => { this.dragging = false; });
    this.table.addEventListener("dblclick", (e) => {
      if (e.target.closest("td[data-r]")) this.startEdit();
    });
    this.table.addEventListener("click", (e) => {
      const th = e.target.closest("th[data-c], th[data-r]");
      if (!th) return;
      if (th.dataset.c !== undefined) {
        const c = Number(th.dataset.c);
        this.select(0, c);
        this.select(Math.max(0, this.rows.length - 1), c, { extend: true });
      } else {
        const r = Number(th.dataset.r);
        this.select(r, 0);
        this.select(r, this.width - 1, { extend: true });
      }
    });
    this.scroller.addEventListener("keydown", (e) => {
      if (this.editing) return;
      const mod = e.ctrlKey || e.metaKey;
      const moves = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] };
      if (moves[e.key]) { e.preventDefault(); this.move(...moves[e.key], e.shiftKey); return; }
      if (e.key === "Tab") { e.preventDefault(); this.move(0, e.shiftKey ? -1 : 1); return; }
      if (e.key === "Enter" || e.key === "F2") { e.preventDefault(); this.startEdit(); return; }
      if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); this.clear(); return; }
      if (mod && e.key.toLowerCase() === "z") { e.preventDefault(); (e.shiftKey ? redo : undo)(this.view); return; }
      if (mod && e.key.toLowerCase() === "y") { e.preventDefault(); redo(this.view); return; }
      if (mod && e.key.toLowerCase() === "a") {
        e.preventDefault();
        this.select(0, 0);
        this.select(this.rows.length - 1, this.width - 1, { extend: true });
        return;
      }
      if (e.key === "Home") { e.preventDefault(); this.select(mod ? 0 : this.active.r, 0, { extend: e.shiftKey }); return; }
      if (e.key === "End") {
        e.preventDefault();
        this.select(mod ? this.rows.length - 1 : this.active.r, this.width - 1, { extend: e.shiftKey });
        return;
      }
      if (e.key.length === 1 && !mod && !e.altKey) { e.preventDefault(); this.startEdit(e.key); }
    });
    this.scroller.addEventListener("copy", (e) => { if (!this.editing) this.copy(e); });
    this.scroller.addEventListener("cut", (e) => { if (!this.editing) { this.copy(e); this.clear(); } });
    this.scroller.addEventListener("paste", (e) => { if (!this.editing) this.paste(e); });
    // The formula bar edits the active cell too.
    this.formula.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        this.setValue(this.active.r, this.active.c, this.formula.value);
        this.move(1, 0);
        this.scroller.focus();
      } else if (e.key === "Escape") {
        this.formula.value = this.values.raw(this.active.r, this.active.c);
        this.scroller.focus();
      }
    });
    this.formula.addEventListener("blur", () => {
      if (!this.readOnly && this.formula.value !== this.values.raw(this.active.r, this.active.c)) {
        this.setValue(this.active.r, this.active.c, this.formula.value);
      }
    });
  }

  // The view's document changed (here, by undo, or by someone else): show it again.
  docChanged() {
    if (this.editing) {
      // Keep typing: redraw once the cell is done.
      this.pending = true;
      return;
    }
    this.refresh();
  }

  setVisible(on) {
    this.node.hidden = !on;
  }

  focus() { this.scroller.focus({ preventScroll: true }); }
}


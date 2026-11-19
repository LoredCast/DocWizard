// "Find in all chapters" (ARCHITECTURE.md §8.12): search every document of the project,
// preview each match with what it becomes, chapter by chapter, and replace the chosen ones.
// The server applies them through each document's room, after keeping a version, so people
// editing see the change at once and nothing typed since the search is overwritten.
import { EditorSelection, EditorView } from "codemirror";
import { $, api, el, t, toast } from "../ui/dom.js";
import { searchDocs } from "./project-find.js";

export class ProjectFind {
  constructor({ projectId, docId, view, canEdit, docUrl, settle }) {
    this.projectId = projectId;
    this.docId = docId;
    this.view = view;
    this.canEdit = canEdit;
    this.docUrl = docUrl;
    this.settle = settle;
    this.dialog = $("#project-find-dialog");
    this.form = $("#project-find-form");
    this.results = $("#project-find-results");
    this.summary = $("#project-find-summary");
    this.replaceBtn = $("#project-find-replace");
    this.docs = null;
    this.found = null;
    if (!this.dialog) return;
    if (!canEdit) {
      this.form.elements.replacement.closest("label").hidden = true;
      this.replaceBtn.hidden = true;
    }
    this.form.addEventListener("submit", (event) => {
      event.preventDefault();
      this.search(true);
    });
    let timer = null;
    this.form.addEventListener("input", (event) => {
      if (event.target.name === "replacement" || event.target.type === "checkbox") {
        clearTimeout(timer);
        timer = setTimeout(() => this.search(false), 150);
      }
    });
    this.results.addEventListener("change", (event) => {
      const all = event.target.closest("[data-doc-all]");
      if (all) {
        for (const box of this.results.querySelectorAll(`[data-doc="${CSS.escape(all.dataset.docAll)}"]`)) box.checked = all.checked;
      }
      this.count();
    });
    this.results.addEventListener("click", (event) => {
      const go = event.target.closest("[data-go]");
      if (!go) return;
      const [docId, from, to] = go.dataset.go.split(":");
      if (docId !== this.docId) return; // a link to the other chapter
      event.preventDefault();
      this.dialog.close();
      this.show(Number(from), Number(to));
    });
    this.replaceBtn.addEventListener("click", () => this.replace());
  }

  open() {
    if (!this.dialog) return;
    const sel = this.view.state.selection.main;
    const picked = this.view.state.sliceDoc(sel.from, sel.to);
    if (picked && !picked.includes("\n") && picked.length <= 200) this.form.elements.query.value = picked;
    this.docs = null; // fetch fresh texts each time
    this.dialog.showModal();
    this.form.elements.query.focus();
    this.form.elements.query.select();
    if (this.form.elements.query.value) this.search(true);
  }

  options() {
    const f = this.form.elements;
    return { regexp: f.regexp.checked, caseSensitive: f.caseSensitive.checked, wholeWord: f.wholeWord.checked,
             replacement: f.replacement.value };
  }

  async search(fetchTexts) {
    const query = this.form.elements.query.value;
    if (!query) {
      this.results.replaceChildren();
      this.summary.textContent = "";
      this.replaceBtn.disabled = true;
      return;
    }
    if (fetchTexts || !this.docs) {
      this.summary.textContent = t("Searching…");
      try {
        await this.settle?.();
        ({ docs: this.docs } = await api("GET", `/api/projects/${this.projectId}/texts`));
      } catch (err) {
        this.summary.textContent = err.message;
        return;
      }
    }
    // Chapters by default; bibliographies, styles and data files when asked for.
    const prose = new Set(["markdown", "latex", "fountain"]);
    const docs = this.form.elements.allFiles.checked ? this.docs : this.docs.filter((d) => prose.has(d.kind));
    try {
      this.found = searchDocs(docs, query, this.options());
    } catch (err) {
      this.summary.textContent = t("This isn't a valid regular expression: {message}", { message: err.message });
      this.results.replaceChildren();
      this.replaceBtn.disabled = true;
      return;
    }
    this.render();
  }

  render() {
    const { groups, total, truncated } = this.found;
    const replacing = this.canEdit;
    this.results.replaceChildren(...groups.map((g) => el("li", { class: "match-group" },
      el("h3", {},
        replacing ? el("input", { type: "checkbox", checked: true, "data-doc-all": g.doc.id,
                                  "aria-label": t("Replace in {path}", { path: g.doc.path }) }) : null,
        el("a", { href: this.docUrl(g.doc.path) }, g.doc.path),
        el("span", { class: "muted" }, g.matches.length === 1 ? t("1 match") : t("{n} matches", { n: g.matches.length }))),
      el("ul", { class: "plain" }, ...g.matches.map((m, i) => el("li", { class: "match-row" },
        replacing ? el("input", { type: "checkbox", checked: true, "data-doc": g.doc.id, "data-i": String(i),
                                  "aria-label": t("Line {line}", { line: m.line }) }) : null,
        el("a", { class: "line-no", href: `${this.docUrl(g.doc.path)}#L${m.line}C${m.col}`,
                  "data-go": `${g.doc.id}:${m.from}:${m.to}`, title: t("Go to line {line}", { line: m.line }) }, String(m.line)),
        el("span", { class: "context" }, m.context.before,
          replacing && this.form.elements.replacement.value !== ""
            ? [el("del", {}, m.context.text), el("ins", {}, m.insert)]
            : el("mark", {}, m.context.text),
          m.context.after)))))));
    const docs = groups.length;
    this.summary.textContent = !total ? t("No matches.")
      : (total === 1 ? t("1 match") : t("{n} matches", { n: total.toLocaleString() }))
        + " · " + (docs === 1 ? t("in 1 document") : t("in {n} documents", { n: docs }))
        + (truncated ? " · " + t("only the first {n} are shown", { n: total.toLocaleString() }) : "");
    this.count();
  }

  selected() {
    const out = [];
    for (const g of this.found?.groups ?? []) {
      const boxes = this.results.querySelectorAll(`[data-doc="${CSS.escape(g.doc.id)}"]`);
      const edits = [];
      for (const box of boxes) {
        if (!box.checked) continue;
        const m = g.matches[Number(box.dataset.i)];
        edits.push({ from: m.from, to: m.to, expect: m.text, insert: m.insert });
      }
      if (edits.length) out.push({ id: g.doc.id, version: g.doc.version, edits });
    }
    return out;
  }

  count() {
    const n = this.selected().reduce((sum, d) => sum + d.edits.length, 0);
    this.replaceBtn.disabled = !n;
    this.replaceBtn.textContent = n ? (n === 1 ? t("Replace 1 match") : t("Replace {n} matches", { n: n.toLocaleString() }))
      : t("Replace");
  }

  async replace() {
    const docs = this.selected();
    if (!docs.length) return;
    this.replaceBtn.disabled = true;
    try {
      const res = await api("POST", `/api/projects/${this.projectId}/replace`, {
        query: this.form.elements.query.value, replacement: this.form.elements.replacement.value, docs });
      const skipped = res.documents.reduce((sum, d) => sum + d.skipped, 0);
      const changed = res.documents.filter((d) => d.replaced).length;
      toast((res.replaced === 1 ? t("Replaced 1 match") : t("Replaced {n} matches", { n: res.replaced }))
        + " " + (changed === 1 ? t("in 1 document") : t("in {n} documents", { n: changed }))
        + (skipped ? ". " + t("{n} had changed since the search and were left as they are.", { n: skipped }) : ".")
        + " " + t("A version from before is in each document's history."));
      await this.search(true);
    } catch (err) {
      toast(err.message, { error: true });
      this.count();
    }
  }

  show(from, to) {
    const len = this.view.state.doc.length;
    const range = EditorSelection.range(Math.min(from, len), Math.min(to, len));
    this.view.dispatch({ selection: range, effects: EditorView.scrollIntoView(range.from, { y: "center" }) });
    this.view.focus();
  }
}

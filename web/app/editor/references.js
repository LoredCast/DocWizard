// The references panel (ARCHITECTURE.md §10.4): what the document cites, the project's
// bibliography files, and your Zotero libraries with their collections, to search and cite
// from quickly. Items with a PDF open in the page finder, which sets the citation's locator.
import { api, el, t, toast } from "../ui/dom.js";
import { applyCitation, fold, libraryField, scoreItem, sourceName } from "./citations.js";
import {
  addBibliography, bibliographyEntries, citationAt, citedKeys, citeChanges, documentLang, formatLocator,
  locatorOf, normalizeEntry, quoteBefore,
} from "./cite-tools.js";
import { openPdfFinder } from "./pdf-finder.js";

const PAGE = 50;

// replaceChildren, leaving out empty slots (it would write "null").
function fill(node, ...children) {
  node.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
}

function debounce(fn, ms) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

// "23" -> "p. 23", "23-25" -> "pp. 23-25"; anything else as typed.
export function locatorFromInput(lang, value) {
  const v = value.trim();
  const m = /^(\d+|[ivxlc]+)(?:\s*[-–]\s*(\d+|[ivxlc]+))?$/i.exec(v);
  return m ? formatLocator(lang, m[1], m[2] ?? "") : v;
}

export class ReferencesPanel {
  constructor({ view, panel, readOnly, canBrowse, onToggle, onCited, projectId }) {
    this.view = view;
    this.projectId = projectId;
    this.panel = panel;
    this.readOnly = readOnly;
    this.canBrowse = canBrowse;
    this.onToggle = onToggle ?? (() => {});
    this.onCited = onCited ?? (() => {});
    this.mode = "doc";
    this.query = "";
    this.collection = "";
    this.sources = null;     // your Zotero libraries, once loaded
    this.expanded = null;    // id of the item showing its details
    this.zItems = [];
    this.zTotal = 0;
    this.seq = 0;
    this.build();
  }

  build() {
    this.sourceSelect = el("select", { "aria-label": t("Show references from") });
    this.searchInput = el("input", { type: "search", placeholder: t("Search author, title, year or key"),
                                     "aria-label": t("Search references") });
    this.collectionSelect = el("select", { "aria-label": t("Collection"), hidden: true });
    this.sourceNote = el("div", { class: "muted refs-note" });
    this.atCursor = el("div", { class: "refs-cursor" });
    this.list = el("ul", { class: "plain refs-list" });
    this.more = el("div", { class: "refs-more" });
    const head = el("div", { class: "pane-head" },
      el("span", {}, t("References")), el("span", { class: "spacer" }),
      el("button", { type: "button", onclick: () => this.hide() }, t("Close")));
    const controls = el("div", { class: "refs-controls" },
      this.sourceSelect, this.searchInput, this.collectionSelect, this.sourceNote);
    if (!this.readOnly && this.projectId) controls.append(this.lookupForm());
    this.panel.replaceChildren(head, controls, el("div", { class: "panel-body" }, this.atCursor, this.list, this.more));
    this.sourceSelect.addEventListener("change", () => {
      this.mode = this.sourceSelect.value;
      this.collection = "";
      this.expanded = null;
      this.render();
    });
    this.collectionSelect.addEventListener("change", () => {
      this.collection = this.collectionSelect.value;
      this.render();
    });
    const onSearch = debounce(() => {
      this.query = this.searchInput.value;
      this.expanded = null;
      this.renderList();
    }, 200);
    this.searchInput.addEventListener("input", onSearch);
    this.searchInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        const first = this.list.querySelector(".ref-item");
        if (first) first.querySelector(".ref-head")?.click();
      }
    });
    this.cursorMoved = debounce(() => { if (this.open) this.renderCursor(); }, 150);
  }

  // Add a reference by DOI, ISBN or arXiv ID (§10.5): the server looks it up, adds it to the
  // document's CSL-JSON file (references.json unless it lists another) and the key is cited here.
  lookupForm() {
    const input = el("input", { name: "id", placeholder: t("DOI, ISBN or arXiv ID"), autocomplete: "off",
                                "aria-label": t("Add a reference by DOI, ISBN or arXiv ID") });
    const button = el("button", { type: "submit" }, t("Add"));
    const form = el("form", { class: "row refs-lookup" }, input, button);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const id = input.value.trim();
      if (!id) return;
      const text = this.text();
      const file = bibliographyEntries(text).map(normalizeEntry).find((e) => /\.json$/i.test(e)) ?? "references.json";
      button.disabled = true;
      button.textContent = t("Looking up…");
      try {
        const data = await api("POST", `/api/projects/${this.projectId}/references`, { id, file });
        const pos = this.view.state.selection.main.head;
        const before = this.view.state.sliceDoc(Math.max(0, pos - 1), pos);
        const key = `${before && !/\s|\[|\(/.test(before) ? " " : ""}[@${data.key}]`;
        applyCitation(this.view, { id: data.key, source: data.file }, pos, pos, key);
        input.value = "";
        toast(data.added ? t("Added @{key} to {file} and cited it.", { key: data.key, file: data.file })
          : t("@{key} was already in {file}; cited it.", { key: data.key, file: data.file }));
        this.onCited();
      } catch (err) {
        toast(err.message, { error: true });
      } finally {
        button.disabled = false;
        button.textContent = t("Add");
      }
    });
    return form;
  }

  get open() { return !this.panel.hidden; }

  get library() { return this.view.state.field(libraryField, false) ?? new Map(); }

  text() { return this.view.state.doc.toString(); }

  async show({ focus = true } = {}) {
    this.panel.hidden = false;
    this.onToggle(true);
    if (focus) this.searchInput.focus();
    await this.loadSources();
    this.render();
  }

  hide() {
    this.panel.hidden = true;
    this.onToggle(false);
    this.view.focus();
  }

  toggle() { return this.open ? this.hide() : this.show(); }

  libraryChanged() {
    if (this.open && this.mode !== "zotero") this.render();
  }

  async loadSources(force = false) {
    if (!this.canBrowse) { this.sources = []; return; }
    if (this.sources && !force) return;
    try {
      this.sources = (await api("GET", "/api/citations/sources")).sources;
    } catch {
      this.sources = this.sources ?? [];
    }
  }

  ownSource(item) {
    const z = item.zotero;
    if (!z || !this.sources) return null;
    return this.sources.find((s) => (z.sourceId ? s.id === z.sourceId : s.name === z.source)) ?? null;
  }

  // -- rendering ----------------------------------------------------------------
  render() {
    const options = [el("option", { value: "doc" }, t("This document")),
                     el("option", { value: "project" }, t("All of this project's references"))];
    for (const s of this.sources ?? []) {
      options.push(el("option", { value: `z:${s.id}` }, t("Zotero: {name}", { name: s.name })));
    }
    this.sourceSelect.replaceChildren(...options);
    if (![...this.sourceSelect.options].some((o) => o.value === this.mode)) this.mode = "doc";
    this.sourceSelect.value = this.mode;
    this.renderCursor();
    this.renderSourceNote();
    if (this.mode.startsWith("z:")) this.loadCollections();
    else this.collectionSelect.hidden = true;
    this.renderList();
  }

  zSource() {
    return this.mode.startsWith("z:") ? this.sources?.find((s) => `z:${s.id}` === this.mode) ?? null : null;
  }

  renderSourceNote() {
    const src = this.zSource();
    if (!src) {
      const entries = bibliographyEntries(this.text());
      this.sourceNote.replaceChildren(entries.length
        ? t("Bibliography: {list}", { list: entries.map((e) => sourceName(e)).join(", ") })
        : t("This document has no bibliography yet. Citing an item adds its file or library."));
      if (!this.sources?.length && this.canBrowse) {
        this.sourceNote.append(" ", el("a", { href: "/settings#zotero" }, t("Connect Zotero")));
      }
      return;
    }
    const state = src.status === "error" ? el("span", { class: "error" }, src.lastError || t("Refreshing failed."))
      : t("{n} items", { n: src.itemCount });
    this.sourceNote.replaceChildren(state, " ",
      el("button", { type: "button", class: "link", onclick: () => this.refreshSource(src) }, t("Refresh")));
  }

  async refreshSource(src) {
    this.sourceNote.textContent = t("Refreshing…");
    try {
      await api("POST", `/api/citations/sources/${src.id}/refresh`);
    } catch (err) {
      toast(err.message, { error: true });
    }
    await this.loadSources(true);
    this.render();
  }

  async loadCollections() {
    const src = this.zSource();
    if (!src) return;
    let cols = [];
    try {
      cols = (await api("GET", `/api/citations/sources/${src.id}/collections`)).collections;
    } catch { /* none */ }
    if (this.zSource() !== src) return;
    const children = new Map();
    for (const c of cols) {
      const key = c.parent ?? "";
      if (!children.has(key)) children.set(key, []);
      children.get(key).push(c);
    }
    const options = [el("option", { value: "" }, t("All items"))];
    const walk = (parent, depth) => {
      for (const c of (children.get(parent) ?? []).sort((a, b) => a.name.localeCompare(b.name))) {
        options.push(el("option", { value: c.key }, `${"  ".repeat(depth)}${c.name}`));
        if (depth < 8) walk(c.key, depth + 1);
      }
    };
    walk("", 0);
    this.collectionSelect.replaceChildren(...options);
    this.collectionSelect.value = this.collection;
    this.collectionSelect.hidden = cols.length === 0;
  }

  renderCursor() {
    const { state } = this.view;
    const text = state.doc.toString();
    const citation = citationAt(text, state.selection.main.head);
    if (!citation || citation.from === state.selection.main.head) {
      this.atCursor.replaceChildren();
      return;
    }
    const library = this.library;
    this.atCursor.replaceChildren(
      el("h3", {}, t("At the cursor")),
      el("ul", { class: "plain" }, citation.cites.map((c) => {
        const item = library.get(c.key);
        const locator = locatorOf(c);
        const src = item && this.ownSource(item);
        return el("li", { class: "refs-cite" },
          el("span", { class: "grow" },
            item ? el("strong", {}, item.label) : el("span", { class: "error" }, t("@{key} is in no bibliography", { key: c.key })),
            locator ? el("span", { class: "muted" }, ` · ${locator}`) : null),
          item && src && item.zotero?.pdf ? el("button", { type: "button", class: "link",
            onclick: () => this.findPage(item, src, citation) }, t("Find page")) : null,
          item ? el("button", { type: "button", class: "link", onclick: () => this.reveal(item) }, t("Details")) : null);
      })));
  }

  reveal(item) {
    this.mode = "project";
    this.query = item.id;
    this.searchInput.value = item.id;
    this.expanded = item.id;
    this.render();
  }

  async renderList() {
    const seq = ++this.seq;
    this.more.replaceChildren();
    if (this.zSource()) {
      await this.renderZotero(seq, 0);
      return;
    }
    const library = this.library;
    const text = this.text();
    const entries = bibliographyEntries(text).map(normalizeEntry);
    const cited = citedKeys(text);
    const citedSet = new Set(cited);
    const q = fold(this.query.trim());
    let items;
    if (this.mode === "doc" && !q) {
      // What the document cites, in order, then what its bibliography offers.
      const rows = cited.map((key) => library.get(key) ?? { id: key, missing: true, authors: [], year: "", title: "" });
      const rest = [...library.values()].filter((i) => !citedSet.has(i.id) && entries.includes(normalizeEntry(i.source ?? "")));
      rest.sort((a, b) => a.label.localeCompare(b.label));
      fill(this.list,
        rows.length ? el("li", { class: "refs-heading" }, t("Cited here ({n})", { n: rows.length })) : null,
        ...rows.map((i) => this.row(i, { cited: true, listed: true })),
        rest.length ? el("li", { class: "refs-heading" }, t("In the bibliography, not cited yet")) : null,
        ...rest.slice(0, 300).map((i) => this.row(i, { cited: false, listed: true })));
      if (!rows.length && !rest.length) {
        this.list.replaceChildren(el("li", { class: "muted" },
          library.size ? t("Nothing cited yet. Search above, or pick a library to browse.")
            : t("No references yet. Add a .bib, .json or .ris file to the project, or connect Zotero in your settings.")));
      }
      return;
    }
    const pool = [...library.values()].filter((i) => this.mode === "project" || entries.includes(normalizeEntry(i.source ?? "")));
    items = pool.map((i) => ({ i, s: q ? scoreItem(q, i) : 1 })).filter((x) => x.s >= 0);
    items.sort((a, b) => b.s - a.s || a.i.label.localeCompare(b.i.label));
    this.list.replaceChildren(...items.slice(0, 200).map(({ i }) => this.row(i, {
      cited: citedSet.has(i.id), listed: entries.includes(normalizeEntry(i.source ?? "")) })));
    if (!items.length) this.list.replaceChildren(el("li", { class: "muted" }, t("No matching references.")));
    else if (items.length > 200) this.more.append(el("p", { class: "muted" }, t("Showing 200 of {n}. Search to narrow it down.", { n: items.length })));
  }

  async renderZotero(seq, offset) {
    const src = this.zSource();
    if (offset === 0) this.list.replaceChildren(el("li", { class: "muted" }, t("Loading…")));
    const params = new URLSearchParams({ q: this.query, limit: String(PAGE), offset: String(offset) });
    if (this.collection) params.set("collection", this.collection);
    let data;
    try {
      data = await api("GET", `/api/citations/sources/${src.id}/items?${params}`);
    } catch (err) {
      if (seq === this.seq) this.list.replaceChildren(el("li", { class: "error" }, err.message));
      return;
    }
    if (seq !== this.seq) return;
    const text = this.text();
    const citedSet = new Set(citedKeys(text));
    const entries = bibliographyEntries(text).map(normalizeEntry);
    const rows = data.items.map((i) => this.row(i, { cited: citedSet.has(i.id), listed: entries.includes(i.source) }));
    if (offset === 0) this.list.replaceChildren(...rows);
    else this.list.append(...rows);
    if (!data.total) {
      this.list.replaceChildren(el("li", { class: "muted" }, src.itemCount ? t("No matching references.")
        : t("This library is empty, or hasn't been fetched yet.")));
    }
    const shown = offset + data.items.length;
    this.more.replaceChildren();
    if (shown < data.total) {
      this.more.append(el("button", { type: "button", onclick: () => this.renderZotero(this.seq, shown) },
        t("Show more ({n} of {total})", { n: shown, total: data.total })));
    }
  }

  row(item, { cited, listed }) {
    const li = el("li", { class: item.missing ? "ref-item missing" : "ref-item", "data-key": item.id });
    const badges = el("span", { class: "ref-badges" },
      item.zotero?.pdf ? el("span", { class: "badge", title: t("Has a PDF") }, "PDF") : null,
      cited ? el("span", { class: "badge", title: t("Cited in this document") }, t("cited")) : null,
      !item.missing && !listed ? el("span", { class: "badge muted", title: t("Citing it adds {source} to the bibliography", { source: sourceName(item.source) }) }, "+") : null);
    const head = el("button", { type: "button", class: "ref-head", "aria-expanded": "false" },
      el("span", { class: "ref-line" },
        el("strong", {}, item.missing ? `@${item.id}` : item.label), badges),
      el("span", { class: "ref-title" }, item.missing ? t("Not in any bibliography of this project") : item.title));
    head.addEventListener("click", () => {
      this.expanded = this.expanded === item.id ? null : item.id;
      for (const other of this.list.querySelectorAll(".ref-item.open")) {
        if (other !== li) {
          other.classList.remove("open");
          other.querySelector(".ref-details")?.remove();
          other.querySelector(".ref-head")?.setAttribute("aria-expanded", "false");
        }
      }
      if (this.expanded === item.id) this.expand(li, item);
      else {
        li.classList.remove("open");
        li.querySelector(".ref-details")?.remove();
        head.setAttribute("aria-expanded", "false");
      }
    });
    li.append(head);
    if (this.expanded === item.id) this.expand(li, item);
    return li;
  }

  expand(li, item) {
    li.classList.add("open");
    li.querySelector(".ref-head").setAttribute("aria-expanded", "true");
    if (item.missing) return;
    const lang = documentLang(this.text());
    const locator = el("input", { class: "ref-locator", placeholder: formatLocator(lang, 23),
                                  "aria-label": t("Page or other locator (optional)") });
    const cite = () => {
      this.cite(item, locatorFromInput(lang, locator.value));
      locator.value = "";
    };
    locator.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); cite(); }
    });
    const files = el("div", { class: "ref-files" });
    const details = el("div", { class: "ref-details" },
      el("p", { class: "muted" }, [item.authors.join(", "), item.year, item.container].filter(Boolean).join(" · ")),
      el("p", {}, el("code", {}, `@${item.id}`), " ",
        el("span", { class: "muted" }, sourceName(item.source))),
      this.readOnly ? null : el("div", { class: "row" }, locator,
        el("button", { type: "button", class: "primary", onclick: cite }, t("Cite"))),
      files);
    li.append(details);
    const src = this.ownSource(item);
    if (src) this.loadDetails(item, src, files);
  }

  async loadDetails(item, src, slot) {
    slot.replaceChildren(el("span", { class: "muted" }, t("Loading attachments…")));
    let data;
    try {
      data = await api("GET", `/api/citations/sources/${src.id}/items/${item.zotero.item}`);
    } catch (err) {
      slot.replaceChildren(el("span", { class: "error" }, err.message));
      return;
    }
    const links = el("p", { class: "row" },
      data.links.web ? el("a", { href: data.links.web, target: "_blank", rel: "noopener" }, t("zotero.org")) : null,
      el("a", { href: data.links.desktop }, t("Show in Zotero")),
      data.fields.DOI ? el("a", { href: `https://doi.org/${data.fields.DOI}`, target: "_blank", rel: "noopener" }, "DOI") : null);
    const atts = data.attachments.map((a) => {
      const isPdf = a.contentType === "application/pdf";
      return el("li", { class: "row" },
        el("span", { class: "grow ellipsis", title: a.filename }, a.title || a.filename || t("Attachment")),
        isPdf && a.file ? el("button", { type: "button", onclick: () => this.findPage({ ...item, page: data.fields.page ?? item.page }, src, null, a) },
          t("Find page")) : null,
        !a.file && a.linkMode === "linked_url" && a.url ? el("a", { href: a.url, target: "_blank", rel: "noopener" }, t("Open link")) : null,
        isPdf && !a.file ? el("a", { href: a.desktop, title: src.connection === "local"
          ? t("Add the WebDAV folder Zotero syncs to in your settings to open PDFs here.") : "" }, t("Open in Zotero")) : null);
    });
    fill(slot, links,
      atts.length ? el("ul", { class: "plain ref-atts" }, atts) : el("p", { class: "muted" }, t("No attachments.")),
      data.attachmentsError ? el("p", { class: "muted" }, t("Zotero couldn't be reached; this list may be old.")) : null);
  }

  // -- actions ------------------------------------------------------------------
  cite(item, locator = "") {
    if (this.readOnly) return;
    const { view } = this;
    const before = view.state.doc.toString();
    const { changes, cursor } = citeChanges(before, view.state.selection.main, item.id, locator);
    if (changes.length) {
      view.dispatch({ changes, selection: { anchor: cursor }, scrollIntoView: true, userEvent: "input" });
    }
    const bib = item.source ? addBibliography(view.state.doc.toString(), item.source) : null;
    if (bib) view.dispatch({ changes: bib, userEvent: "input" });
    if (bib || !this.library.has(item.id)) this.onCited(item);
    toast(locator ? t("Cited {label}, {locator}.", { label: item.label, locator }) : t("Cited {label}.", { label: item.label }));
    this.renderCursor();
    if (window.matchMedia("(max-width: 900px)").matches) this.hide();
    else view.focus();
  }

  async findPage(item, src, citation = null, attachment = null) {
    let att = attachment;
    let page = item.page;
    if (!att) {
      try {
        const data = await api("GET", `/api/citations/sources/${src.id}/items/${item.zotero.item}`);
        att = data.attachments.find((a) => a.contentType === "application/pdf" && a.file);
        page = data.fields.page ?? page;
        if (!att) {
          const desk = data.attachments.find((a) => a.contentType === "application/pdf");
          toast(desk ? t("This PDF can only be opened in Zotero.") : t("This item has no PDF."), { error: !desk });
          if (desk) window.location.href = desk.desktop;
          return;
        }
      } catch (err) {
        toast(err.message, { error: true });
        return;
      }
    }
    const { state } = this.view;
    const text = state.doc.toString();
    const around = citation ?? citationAt(text, state.selection.main.head);
    const sel = state.selection.main;
    // The passage to look for: the quotation before the citation or the cursor, or the selection.
    const quote = around ? quoteBefore(text, around.from)
      : (sel.empty ? quoteBefore(text, sel.head) : state.sliceDoc(sel.from, sel.to)).slice(0, 600);
    openPdfFinder({
      url: `/api/citations/sources/${src.id}/attachments/${att.key}/file`,
      title: `${item.label}: ${item.title}`, item: { ...item, page }, attachment: att, quote,
      lang: documentLang(text),
      onCite: this.readOnly ? null : (locator) => this.cite(item, locator),
    });
  }
}

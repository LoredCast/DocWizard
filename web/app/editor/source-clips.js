// Verified source clips (ARCHITECTURE.md §10.5): the page a citation points to, read from the
// source's PDF, beside the text. Hovering a citation shows that page's passage; "Verify
// quotes" checks every quotation against its PDF; the source reader shows a PDF next to the
// draft, and a passage selected there goes into the text as a quotation with its citation,
// with "Insert quote" or by dragging it.
//
// A source's PDF is a project file named after its key (sources/doe2020.pdf), or the PDF
// attached to it in your Zotero library. Everything is read in the browser.
import { EditorSelection } from "codemirror";
import { loadPdfJs } from "../preview/pdf.js";
import { $, api, el, t, toast } from "../ui/dom.js";
import { citationAt, citedKeys, documentLang, formatLocator, normalizeText, pageLabel, pageOffset, setLocator } from "./cite-tools.js";
import { excerpt, locatorPage, pageIndexFor, quotations, quoteMarkup, verifyQuote } from "./source-check.js";

const HOVER_DELAY = 450;
const MAX_PDF_BYTES = 60 * 1024 * 1024;

const stored = (key) => {
  try { return localStorage.getItem(key); } catch { return null; }
};

// Where each source's PDF is, and its text once read.
export class SourceLibrary {
  constructor({ projectId, files, library }) {
    this.projectId = projectId;
    this.files = files ?? [];
    this.library = library; // () => Map of the project's citable items
    this.sources = null;
    this.found = new Map(); // key -> Promise<{url, title, offsetKey, range} | null>
    this.loaded = new Map(); // url -> Promise<{doc, raw, norm, labels, offset}>
  }

  item(key) {
    return this.library()?.get(key) ?? null;
  }

  locate(key) {
    if (!this.found.has(key)) this.found.set(key, this.#locate(key).catch(() => null));
    return this.found.get(key);
  }

  async #locate(key) {
    const item = this.item(key);
    const title = item ? `${item.label}: ${item.title}` : key;
    const lower = key.toLowerCase();
    const own = this.files.find((f) => f.type !== "document" && f.path.toLowerCase().endsWith(".pdf")
      && f.path.split("/").pop().slice(0, -4).toLowerCase() === lower);
    if (own) {
      const url = `/api/projects/${this.projectId}/assets/${own.path.split("/").map(encodeURIComponent).join("/")}`;
      return { url, title, offsetKey: `dw.pageOffset.${url}`, range: item?.page, where: own.path };
    }
    if (!item?.zotero?.pdf) return null;
    if (this.sources === null) {
      try {
        this.sources = (await api("GET", "/api/citations/sources")).sources;
      } catch {
        this.sources = [];
      }
    }
    const src = this.sources.find((s) => (item.zotero.sourceId ? s.id === item.zotero.sourceId : s.name === item.zotero.source));
    if (!src) return null; // someone else's library
    const data = await api("GET", `/api/citations/sources/${src.id}/items/${item.zotero.item}`);
    const att = data.attachments.find((a) => a.contentType === "application/pdf" && a.file);
    if (!att) return null;
    return { url: `/api/citations/sources/${src.id}/attachments/${att.key}/file`, title,
             offsetKey: `dw.pageOffset.${att.key}`, range: data.fields?.page ?? item.page, where: "Zotero" };
  }

  load(source) {
    if (!this.loaded.has(source.url)) {
      const promise = this.#load(source);
      promise.catch(() => this.loaded.delete(source.url));
      this.loaded.set(source.url, promise);
    }
    return this.loaded.get(source.url);
  }

  async #load(source) {
    const [pdfjs, res] = await Promise.all([loadPdfJs(), fetch(source.url, { credentials: "same-origin" })]);
    if (!res.ok) throw new Error(t("The PDF could not be opened ({status}).", { status: res.status }));
    const data = new Uint8Array(await res.arrayBuffer());
    if (data.length > MAX_PDF_BYTES) throw new Error(t("This PDF is too large to read here."));
    const doc = await pdfjs.getDocument({ data, verbosity: pdfjs.VerbosityLevel?.ERRORS ?? 0 }).promise;
    const labels = await doc.getPageLabels().catch(() => null);
    const saved = stored(source.offsetKey);
    const offset = saved !== null ? Number(saved) : pageOffset(labels, doc.numPages, source.range);
    const raw = [];
    const norm = [];
    for (let i = 1; i <= doc.numPages; i++) {
      const page = await doc.getPage(i);
      const content = await page.getTextContent();
      const text = content.items.filter((x) => typeof x.str === "string").map((x) => x.str + (x.hasEOL ? "\n" : "")).join(" ");
      raw.push(text);
      norm.push(normalizeText(text));
      page.cleanup();
    }
    return { doc, raw, norm, labels, offset };
  }
}

// The clip shown over a citation: the cited page's passage, the quotation marked.
export class SourceClips {
  constructor({ view, sources, onOpen }) {
    this.view = view;
    this.sources = sources;
    this.onOpen = onOpen;
    this.box = el("div", { class: "source-clip", role: "tooltip", hidden: true });
    document.body.append(this.box);
    this.timer = null;
    this.target = null;
    view.contentDOM.addEventListener("mouseover", (event) => {
      const chip = event.target.closest?.(".cm-cite-chip");
      if (!chip || chip === this.target) return;
      this.target = chip;
      clearTimeout(this.timer);
      this.timer = setTimeout(() => this.showFor(chip), HOVER_DELAY);
    });
    view.contentDOM.addEventListener("mouseout", (event) => {
      if (!event.target.closest?.(".cm-cite-chip")) return;
      if (this.box.contains(event.relatedTarget)) return;
      clearTimeout(this.timer);
      this.target = null;
      this.hideSoon();
    });
    this.box.addEventListener("mouseleave", () => this.hideSoon());
    this.box.addEventListener("mouseenter", () => clearTimeout(this.hideTimer));
    document.addEventListener("keydown", (event) => { if (event.key === "Escape") this.hide(); });
    view.scrollDOM.addEventListener("scroll", () => this.hide(), { passive: true });
  }

  hideSoon() {
    clearTimeout(this.hideTimer);
    this.hideTimer = setTimeout(() => this.hide(), 250);
  }

  hide() {
    this.box.hidden = true;
    this.target = null;
  }

  async showFor(chip) {
    let pos;
    try { pos = this.view.posAtDOM(chip); } catch { return; }
    const text = this.view.state.doc.toString();
    const citation = citationAt(text, pos + 1) ?? citationAt(text, pos);
    if (!citation) return;
    const cite = citation.cites.find((c) => !/^(fig|tbl|eq|sec):/.test(c.key));
    if (!cite) return;
    const rect = chip.getBoundingClientRect();
    await this.show({ key: cite.key, locator: locatorOfCite(cite), quote: quoteFor(text, citation.from), rect });
  }

  async show({ key, locator, quote, rect }) {
    const place = () => {
      const width = Math.min(512, window.innerWidth - 32);
      this.box.style.left = `${Math.max(16, Math.min(rect.left, window.innerWidth - width - 16))}px`;
      const below = rect.bottom + 6;
      this.box.style.top = below + 260 < window.innerHeight ? `${below}px` : `${Math.max(16, rect.top - 266)}px`;
    };
    const head = (...extra) => el("div", { class: "clip-head" },
      el("strong", { class: "spacer" }, this.sources.item(key)?.label ?? key, locator ? `, ${locator}` : ""), ...extra);
    this.box.replaceChildren(head(), el("p", { class: "muted" }, t("Looking for the source…")));
    this.box.hidden = false;
    place();
    const source = await this.sources.locate(key);
    if (!source) {
      this.box.replaceChildren(head(), el("p", { class: "muted" },
        t("No PDF for this source. Attach one in Zotero, or add {name} to the project.", { name: `${key}.pdf` })));
      return;
    }
    let pdf;
    try {
      pdf = await this.sources.load(source);
    } catch (err) {
      this.box.replaceChildren(head(), el("p", { class: "error" }, err.message));
      return;
    }
    if (this.box.hidden) return;
    const printed = locatorPage(locator);
    let index = pageIndexFor(printed, pdf.labels, pdf.offset, pdf.doc.numPages);
    let check = null;
    if (quote) {
      check = verifyQuote(pdf.norm, quote, index, pdf.labels, pdf.offset);
      if (!index && check.page) index = check.page;
    }
    const open = el("button", { type: "button", class: "link", onclick: () => { this.hide(); this.onOpen?.(key, index ?? 1); } },
      t("Open the source"));
    if (!index) {
      this.box.replaceChildren(head(open), el("p", { class: "muted" },
        printed ? t("Page {page} isn't in this PDF.", { page: printed }) : t("The citation names no page.")));
      return;
    }
    const clip = excerpt(pdf.raw[index - 1] ?? "", check && check.page === index ? quote : "");
    this.box.replaceChildren(
      head(el("span", { class: "muted" }, t("PDF page {n}", { n: index })), open),
      check ? el("p", { class: `quote-check verdict ${verdictClass(check.status)}` }, verdictText(check, printed)) : null,
      el("blockquote", {}, clip.before, clip.match ? el("mark", {}, clip.match) : null, clip.after));
  }
}

function locatorOfCite(cite) {
  const m = /^,\s*(.+)$/.exec(cite.suffix ?? "");
  return m ? m[1].trim() : "";
}

function quoteFor(text, citeFrom) {
  return quotations(text.slice(Math.max(0, citeFrom - 1200), citeFrom + 400))
    .find((q) => q.citeFrom + Math.max(0, citeFrom - 1200) === citeFrom)?.quote ?? "";
}

function verdictClass(status) {
  return { verified: "ok", "other-page": "warn", partial: "warn", "not-found": "fail" }[status] ?? "info";
}

function verdictText(check, printed) {
  if (check.status === "verified") return t("The quotation is on this page.");
  if (check.status === "other-page") return t("The quotation is on p. {found}, not p. {cited}.", { found: check.printed, cited: printed ?? "?" });
  if (check.status === "partial") return t("Only part of the quotation matches (p. {found}).", { found: check.printed });
  return t("The quotation isn't in this PDF.");
}

// "Verify quotes": every quotation with a citation, checked against its source's PDF.
export class QuoteCheck {
  constructor({ view, sources, readOnly, onOpen }) {
    this.view = view;
    this.sources = sources;
    this.readOnly = readOnly;
    this.onOpen = onOpen;
    this.dialog = $("#quotes-dialog");
    this.list = $("#quotes-list");
    this.state = $("#quotes-state");
    $("#quotes-again")?.addEventListener("click", () => this.run());
  }

  open() {
    if (!this.dialog) return;
    this.dialog.showModal();
    this.run();
  }

  async run() {
    const text = this.view.state.doc.toString();
    const lang = documentLang(text);
    const found = quotations(text);
    this.list.replaceChildren();
    if (!found.length) {
      this.state.textContent = t("No quotations with a citation in this document.");
      return;
    }
    this.state.textContent = t("Checking {n} quotations…", { n: found.length });
    const counts = { verified: 0, problems: 0, unchecked: 0 };
    for (const q of found) {
      const row = el("li", {});
      this.list.append(row);
      const short = q.quote.length > 140 ? `${q.quote.slice(0, 140)}…` : q.quote;
      const jump = el("button", { type: "button", class: "link", onclick: () => {
        this.dialog.close();
        this.view.dispatch({ selection: EditorSelection.range(q.from, q.to), scrollIntoView: true });
        this.view.focus();
      } }, `“${short}”`);
      const verdict = el("span", { class: "verdict info" }, t("Checking…"));
      const detail = el("ul", {});
      row.append(el("div", { class: "check-head" }, el("span", { class: "spacer" }, jump, " ",
        el("span", { class: "muted" }, `@${q.key}${q.locator ? `, ${q.locator}` : ""}`)), verdict), detail);
      const source = await this.sources.locate(q.key);
      if (!source) {
        verdict.className = "verdict info";
        verdict.textContent = t("No PDF");
        counts.unchecked++;
        continue;
      }
      let pdf;
      try {
        pdf = await this.sources.load(source);
      } catch (err) {
        verdict.textContent = t("Not read");
        detail.append(el("li", { class: "error" }, err.message));
        counts.unchecked++;
        continue;
      }
      const printed = locatorPage(q.locator);
      const index = pageIndexFor(printed, pdf.labels, pdf.offset, pdf.doc.numPages);
      const check = verifyQuote(pdf.norm, q.quote, index, pdf.labels, pdf.offset);
      verdict.className = `verdict ${verdictClass(check.status)}`;
      verdict.textContent = { verified: t("Verified"), "other-page": t("Other page"), partial: t("Partly found"),
                              "not-found": t("Not found") }[check.status];
      if (check.status === "verified") counts.verified++;
      else counts.problems++;
      if (check.status !== "verified") detail.append(el("li", {}, verdictText(check, printed)));
      if (check.snippet) {
        detail.append(el("li", { class: "muted" }, `…${check.snippet}…`, " ",
          el("button", { type: "button", class: "link", onclick: () => { this.dialog.close(); this.onOpen?.(q.key, check.page); } },
            t("Open the source"))));
      }
      if (check.status === "other-page" && !this.readOnly) {
        const locator = formatLocator(lang, check.printed);
        detail.append(el("li", {}, el("button", { type: "button", onclick: (event) => {
          const now = this.view.state.doc.toString();
          const current = citationAt(now, q.citeFrom + 1);
          const cite = current?.cites.find((c) => c.key === q.key);
          if (!cite) return;
          this.view.dispatch({ changes: setLocator(cite, locator), userEvent: "input.cite" });
          event.target.replaceWith(t("Changed to {locator}.", { locator }));
        } }, t("Cite {locator} instead", { locator }))));
      }
    }
    this.state.textContent = [
      counts.verified === 1 ? t("1 verified") : t("{n} verified", { n: counts.verified }),
      counts.problems === 1 ? t("1 to look at") : t("{n} to look at", { n: counts.problems }),
      counts.unchecked ? (counts.unchecked === 1 ? t("1 without a PDF") : t("{n} without a PDF", { n: counts.unchecked })) : null,
    ].filter(Boolean).join(" · ");
  }
}

// The source reader: a PDF beside the draft. Selected passages go into the text as quotations.
export class SourceReader {
  constructor({ view, sources, panel, readOnly, onToggle }) {
    this.view = view;
    this.sources = sources;
    this.panel = panel;
    this.readOnly = readOnly;
    this.onToggle = onToggle ?? (() => {});
    this.key = null;
    this.pdf = null;
    this.pages = el("div", { class: "source-pages" });
    this.pick = el("select", { "aria-label": t("Source") });
    this.info = el("span", { class: "spacer" });
    this.insertBtn = el("button", { type: "button", class: "primary", disabled: true }, t("Insert quote"));
    this.observer = null;
    panel.replaceChildren(
      el("div", { class: "pane-head" }, el("span", {}, t("Source reader")), el("span", { class: "spacer" }),
        el("button", { type: "button", onclick: () => this.hide() }, t("Close"))),
      el("div", { class: "source-pick" }, this.pick),
      this.pages,
      el("div", { class: "source-actions" }, this.info, readOnly ? null : this.insertBtn));
    this.pick.addEventListener("change", () => this.load(this.pick.value, 1));
    this.insertBtn.addEventListener("click", () => this.insert());
    document.addEventListener("selectionchange", () => this.selectionChanged());
    this.pages.addEventListener("dragstart", (event) => {
      const passage = this.selected();
      if (!passage || this.readOnly) return;
      event.dataTransfer.setData("text/plain", this.markup(passage));
      event.dataTransfer.effectAllowed = "copy";
    });
  }

  get open() { return !this.panel.hidden; }

  async show(key = null, page = 1) {
    this.panel.hidden = false;
    this.onToggle(true);
    const text = this.view.state.doc.toString();
    const here = citationAt(text, this.view.state.selection.main.head);
    const keys = citedKeys(text).filter((k) => !/^(fig|tbl|eq|sec):/.test(k));
    this.pick.replaceChildren(el("option", { value: "" }, t("Choose a cited source")),
      ...keys.map((k) => el("option", { value: k }, this.sources.item(k)?.label ?? k)));
    const wanted = key ?? here?.cites.find((c) => keys.includes(c.key))?.key ?? null;
    if (wanted) {
      const startAt = key ? page : (() => {
        const cite = here.cites.find((c) => c.key === wanted);
        return { printed: locatorPage(locatorOfCite(cite)) };
      })();
      this.pick.value = wanted;
      await this.load(wanted, startAt);
    } else if (!this.key) {
      this.pages.replaceChildren(el("p", { class: "muted pad" }, t("Choose a source, or open this from a citation.")));
    }
  }

  hide() {
    this.panel.hidden = true;
    this.onToggle(false);
    this.observer?.disconnect();
    this.view.focus();
  }

  toggle() { return this.open ? this.hide() : this.show(); }

  async load(key, page) {
    if (!key) return;
    this.key = key;
    this.pages.replaceChildren(el("p", { class: "muted pad" }, t("Opening the PDF…")));
    const source = await this.sources.locate(key);
    if (!source) {
      this.pages.replaceChildren(el("p", { class: "muted pad" },
        t("No PDF for this source. Attach one in Zotero, or add {name} to the project.", { name: `${key}.pdf` })));
      return;
    }
    try {
      this.pdf = await this.sources.load(source);
    } catch (err) {
      this.pages.replaceChildren(el("p", { class: "error pad" }, err.message));
      return;
    }
    const { doc, labels, offset } = this.pdf;
    const start = typeof page === "object"
      ? pageIndexFor(page.printed, labels, offset, doc.numPages) ?? 1 : Math.max(1, Math.min(page, doc.numPages));
    // Every page gets a placeholder; pages are drawn as they scroll into view.
    this.observer?.disconnect();
    this.observer = new IntersectionObserver((entries) => {
      for (const e of entries) if (e.isIntersecting) this.draw(e.target);
    }, { root: this.pages, rootMargin: "400px 0px" });
    const width = Math.max(240, this.pages.clientWidth - 16);
    const first = await doc.getPage(1);
    const base = first.getViewport({ scale: 1 });
    const scale = width / base.width;
    const boxes = [];
    for (let i = 1; i <= doc.numPages; i++) {
      const box = el("div", { class: "source-page", "data-page": String(i) },
        el("span", { class: "page-label" }, t("p. {page}", { page: pageLabel(i, labels, offset) })));
      box.style.width = `${Math.floor(base.width * scale)}px`;
      box.style.height = `${Math.floor(base.height * scale)}px`;
      boxes.push(box);
      this.observer.observe(box);
    }
    this.scale = scale;
    this.pages.replaceChildren(...boxes);
    boxes[start - 1]?.scrollIntoView({ block: "start" });
    this.info.textContent = `${source.where} · ${t("{n} pages", { n: doc.numPages })}`;
  }

  async draw(box) {
    if (box.dataset.drawn) return;
    box.dataset.drawn = "1";
    const pdfjs = await loadPdfJs();
    const page = await this.pdf.doc.getPage(Number(box.dataset.page));
    const viewport = page.getViewport({ scale: this.scale });
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const canvas = el("canvas", {});
    canvas.width = Math.floor(viewport.width * dpr);
    canvas.height = Math.floor(viewport.height * dpr);
    const layer = el("div", { class: "textLayer" });
    layer.style.setProperty("--scale-factor", String(viewport.scale));
    box.prepend(canvas, layer);
    await page.render({ canvasContext: canvas.getContext("2d", { alpha: false }), viewport,
                        transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : null }).promise.catch(() => {});
    const textLayer = new pdfjs.TextLayer({ textContentSource: await page.getTextContent(), container: layer, viewport });
    await textLayer.render().catch(() => {});
  }

  selected() {
    const sel = document.getSelection();
    if (!sel || sel.isCollapsed || !this.pages.contains(sel.anchorNode)) return null;
    const text = sel.toString().trim();
    if (!text) return null;
    const node = sel.anchorNode.nodeType === 1 ? sel.anchorNode : sel.anchorNode.parentElement;
    const index = Number(node?.closest(".source-page")?.dataset.page ?? 0);
    return { text, index };
  }

  markup(passage) {
    const { labels, offset } = this.pdf;
    const lang = documentLang(this.view.state.doc.toString());
    const locator = passage.index ? formatLocator(lang, pageLabel(passage.index, labels, offset)) : "";
    return quoteMarkup(passage.text, this.key, locator, lang);
  }

  selectionChanged() {
    if (!this.open) return;
    const passage = this.selected();
    this.insertBtn.disabled = !passage;
    if (passage) {
      this.info.textContent = t("Selected on p. {page}. Insert it, or drag it into the text.",
                                { page: pageLabel(passage.index, this.pdf.labels, this.pdf.offset) });
    }
  }

  insert() {
    const passage = this.selected();
    if (!passage) {
      toast(t("Select a passage in the PDF first."));
      return;
    }
    const insert = this.markup(passage);
    const at = this.view.state.selection.main;
    this.view.dispatch({ changes: { from: at.from, to: at.to, insert }, selection: { anchor: at.from + insert.length },
                         userEvent: "input.paste", scrollIntoView: true });
    this.view.focus();
  }
}

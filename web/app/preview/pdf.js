// PDF preview with the vendored PDF.js (ARCHITECTURE.md §6.4). Pages are laid out at once at
// their final size and drawn when they come near the visible part, so long documents stay light;
// a new render replaces the pages only once the ones in view are drawn, at the same place.
//
// - Each drawn page has PDF.js's text layer over it, so its text can be selected and copied.
// - The PDF bar (optional, given as `bar`) zooms (−, Fit width / Fit page / 50–400 %, +), shows the
//   page in view and jumps to one, and finds text in the whole PDF with every match marked.
// - Double-clicking (double-tapping) a spot asks for its source: onPick gets the page, the point
//   in PDF points from the page's top left, and the words around it in reading order.
// - The editor's cursor is followed paragraph by paragraph: showParagraph() finds the passage's
//   words in the pages' text and marks its lines; showBoxes() marks boxes SyncTeX gave.
//
// This module has no imports, so its pure functions can be tested in Node.

let pdfjsPromise = null;

export async function loadPdfJs() {
  if (!pdfjsPromise) {
    const modUrl = new URL("../../vendor/pdfjs/pdf.min.mjs", import.meta.url).href;
    const workerUrl = new URL("../../vendor/pdfjs/pdf.worker.min.mjs", import.meta.url).href;
    pdfjsPromise = import(modUrl).then((lib) => {
      lib.GlobalWorkerOptions.workerSrc = workerUrl;
      return lib;
    });
  }
  return pdfjsPromise;
}

const GAP = 12;
const PAD = 12;
const WORDS_AROUND = 12;
const WORD = /[\p{L}\p{N}]+/gu;
const CSS_PER_PT = 96 / 72; // 100 % is a PDF point at a CSS pixel's 1/96 inch, as in PDF viewers
export const ZOOM_STEPS = [0.5, 0.67, 0.75, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3, 4];
const ZOOM_KEY = "dw.pdfZoom";

const reducedMotion = () => globalThis.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
const scrollBehavior = () => (reducedMotion() ? "auto" : "smooth");

export class PdfPreview {
  constructor(container, { onError, onLoad, onPick, hint, scrollRatio, label, bar = null, labels = {} } = {}) {
    this.label = label ?? "PDF";
    this.container = container;
    this.startRatio = scrollRatio ?? 0; // where the first render opens, as a share of the scroll height
    this.hint = hint ?? "";
    this.onError = onError ?? null;
    this.onLoad = onLoad ?? null;
    this.onPick = onPick ?? null;
    this.bar = bar;
    this.labels = { page: "Page {n}", matches: "{i} of {n}", noMatches: "No matches", ...labels };
    this.viewer = null;
    this.pages = null;
    this.url = null;
    this.data = null;
    this.doc = null;
    this.sizes = [];
    this.visible = true;
    this.seq = 0;
    this.pendingRender = false;
    this.lastWidth = 0;
    this.lastHeight = 0;
    this.resizeTimer = null;
    this.observer = null;
    this.tasks = new Map();
    this.textLayers = new Map();
    this.zoom = readZoom(); // "fit" (the width), "page" (the whole page) or a scale (1 is 100 %)
    this.marks = { find: [], para: [] }; // [{page, x, y, w, h, current?}] in PDF points from the top left
    this.find = { query: "", hits: [], index: -1 };
    this.texts = null;
    this.currentPage = 0;
  }

  ensureViewer() {
    if (this.viewer) return;
    this.viewer = document.createElement("div");
    this.viewer.className = "pdf-viewer";
    // Scrollable, so reachable with the keyboard (arrow keys and Page Up/Down scroll it).
    this.viewer.tabIndex = 0;
    this.viewer.setAttribute("role", "region");
    this.viewer.setAttribute("aria-label", this.label);
    this.viewer.hidden = !this.visible;
    if (this.onPick && this.hint) this.viewer.title = this.hint;
    this.pages = document.createElement("div");
    this.pages.className = "pdf-pages";
    this.viewer.append(this.pages);
    this.container.append(this.viewer);
    this.viewer.addEventListener("dblclick", (event) => this.handlePick(event));
    this.viewer.addEventListener("scroll", () => this.trackPage(), { passive: true });
    this.viewer.addEventListener("keydown", (event) => this.handleKey(event));
    // Ctrl + wheel (and a trackpad's pinch, which browsers report as one) zooms the PDF, not the page.
    let wheel = 0;
    this.viewer.addEventListener("wheel", (event) => {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      wheel += event.deltaY;
      if (Math.abs(wheel) < 40) return;
      this.stepZoom(wheel < 0 ? 1 : -1);
      wheel = 0;
    }, { passive: false });
    if (typeof ResizeObserver === "function") {
      new ResizeObserver(() => {
        if (!this.visible || !this.doc) return;
        const w = this.viewer.clientWidth;
        const h = this.viewer.clientHeight;
        if (w <= 0) return;
        const fitsHeight = this.zoom === "page" && Math.abs(h - this.lastHeight) > 16;
        if (this.pendingRender || (typeof this.zoom === "string" && (Math.abs(w - this.lastWidth) > 16 || fitsHeight))) {
          clearTimeout(this.resizeTimer);
          this.resizeTimer = setTimeout(() => this.layout(this.doc, this.seq, { anchor: this.anchor() }), 120);
        }
      }).observe(this.viewer);
    }
    this.wireBar();
  }

  show(url) {
    if (!url || url === this.url) return;
    this.url = url;
    this.data = null;
    this.ensureViewer();
    this.load();
  }

  // A PDF made in the browser (§6.4): its bytes, not a URL.
  showData(bytes) {
    this.url = null;
    this.data = bytes;
    this.ensureViewer();
    this.load();
  }

  reload() {
    if (!this.url && !this.data) return;
    this.ensureViewer();
    this.load();
  }

  async load() {
    const seq = ++this.seq;
    const url = this.url;
    const data = this.data;
    try {
      const [pdfjs, res] = await Promise.all([loadPdfJs(), data ? null : fetch(url, { credentials: "same-origin" })]);
      if (seq !== this.seq) return;
      if (res && !res.ok) throw new Error(`HTTP ${res.status}`);
      // PDF.js takes the bytes it is given, so it gets a copy of a PDF kept for a reload.
      const bytes = data ? data.slice() : new Uint8Array(await res.arrayBuffer());
      if (seq !== this.seq) return;
      const doc = await pdfjs.getDocument({ data: bytes, verbosity: pdfjs.VerbosityLevel?.ERRORS ?? 0 }).promise;
      if (seq !== this.seq) {
        doc.destroy();
        return;
      }
      this.pdfjs = pdfjs;
      await this.layout(doc, seq);
    } catch {
      if (seq !== this.seq) return;
      this.onError?.(url);
    }
  }

  // The scale a page is drawn at, from the zoom and the viewer's size.
  scaleFor(base) {
    const width = Math.max(120, this.viewer.clientWidth - 2 * PAD);
    if (this.zoom === "fit") return Math.max(240, width) / base.width;
    if (this.zoom === "page") {
      const height = Math.max(120, this.viewer.clientHeight - 2 * PAD);
      return Math.min(width / base.width, height / base.height);
    }
    return this.zoom * CSS_PER_PT;
  }

  // Lays out every page of *doc* at the zoom, draws the ones that will be in view and swaps them
  // in: a new render at the same relative scroll position, a new zoom at the same spot (*anchor*).
  async layout(doc, seq, { anchor = null } = {}) {
    if (!this.visible || this.viewer.clientWidth <= 0) {
      this.pendingRender = true;
      this.nextDoc = doc;
      return;
    }
    this.pendingRender = false;
    this.lastWidth = this.viewer.clientWidth;
    this.lastHeight = this.viewer.clientHeight;
    const sizes = [];
    for (let i = 1; i <= doc.numPages; i++) {
      const page = await doc.getPage(i);
      if (seq !== this.seq) return;
      const base = page.getViewport({ scale: 1 });
      const scale = this.scaleFor(base);
      sizes.push({ page, scale, width: Math.floor(base.width * scale), height: Math.floor(base.height * scale) });
    }
    const maxScroll = Math.max(0, this.viewer.scrollHeight - this.viewer.clientHeight);
    const ratio = this.doc ? (maxScroll > 0 ? this.viewer.scrollTop / maxScroll : 0) : this.startRatio;

    const frag = document.createDocumentFragment();
    const sheets = sizes.map((s, i) => {
      const sheet = document.createElement("div");
      sheet.className = "pdf-sheet";
      sheet.dataset.page = String(i + 1);
      sheet.style.width = `${s.width}px`;
      sheet.style.height = `${s.height}px`;
      sheet.style.setProperty("--scale-factor", String(s.scale));
      frag.append(sheet);
      return sheet;
    });
    // Where the viewer will be once these pages are in, and the pages that will show there.
    const tops = [];
    let y = PAD;
    for (const s of sizes) {
      tops.push(y);
      y += s.height + GAP;
    }
    const total = y - GAP + PAD;
    let top = ratio * Math.max(0, total - this.viewer.clientHeight);
    let left = null;
    if (anchor && sizes[anchor.page - 1]) {
      const s = sizes[anchor.page - 1];
      top = tops[anchor.page - 1] + anchor.y * s.scale - anchor.dy;
      const widest = Math.max(...sizes.map((z) => z.width));
      const pagesWidth = Math.max(this.viewer.clientWidth - 2 * PAD, widest);
      left = (pagesWidth - s.width) / 2 + PAD + anchor.x * s.scale - anchor.dx;
    }
    top = Math.max(0, Math.min(top, total - this.viewer.clientHeight));
    const bottom = top + this.viewer.clientHeight;
    const first = [];
    sizes.forEach((s, i) => {
      if (tops[i] + s.height >= top - 200 && tops[i] <= bottom + 200) first.push(i);
    });
    await Promise.all(first.map((i) => this.draw(sheets[i], sizes[i], seq)));
    if (seq !== this.seq) return;

    this.observer?.disconnect();
    for (const task of this.tasks.values()) task.cancel?.();
    this.tasks.clear();
    for (const layer of this.textLayers.values()) layer.cancel?.();
    this.textLayers.clear();
    const old = this.doc;
    if (old !== doc) {
      this.headingDoc = null;
      this.marks.para = [];
      this.marks.find = [];
    }
    this.doc = doc;
    this.sizes = sizes;
    this.pages.replaceChildren(frag);
    this.viewer.scrollTop = top;
    if (left !== null) this.viewer.scrollLeft = Math.max(0, left);
    if (old && old !== doc) old.destroy();
    for (const i of first) this.paintMarks(sheets[i]);

    if (typeof IntersectionObserver === "function") {
      this.observer = new IntersectionObserver((entries) => {
        for (const entry of entries) {
          const n = Number(entry.target.dataset.page);
          if (entry.isIntersecting) this.draw(entry.target, this.sizes[n - 1], this.seq);
          else if (this.sizes.length > 8) this.forget(entry.target);
        }
      }, { root: this.viewer, rootMargin: "150% 0px" });
      for (const sheet of sheets) this.observer.observe(sheet);
    } else {
      sheets.forEach((sheet, i) => this.draw(sheet, sizes[i], seq));
    }
    this.trackPage(true);
    this.updateBar();
    if (old !== doc) {
      if (this.find.query) this.runFind(this.find.query, { keep: true });
      this.onLoad?.();
    }
  }

  async draw(sheet, size, seq) {
    if (!size || sheet.dataset.drawn === String(seq) || sheet.querySelector("canvas")) return;
    sheet.dataset.drawn = String(seq);
    const dpr = Math.min(window.devicePixelRatio || 1, 2.5);
    const viewport = size.page.getViewport({ scale: size.scale });
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.floor(viewport.width * dpr));
    canvas.height = Math.max(1, Math.floor(viewport.height * dpr));
    canvas.setAttribute("aria-hidden", "true");
    const ctx = canvas.getContext("2d", { alpha: false });
    const task = size.page.render({ canvasContext: ctx, viewport, transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : null });
    this.tasks.set(sheet, task);
    try {
      await task.promise;
    } catch {
      delete sheet.dataset.drawn;
      return; // cancelled, or the document was replaced
    } finally {
      if (this.tasks.get(sheet) === task) this.tasks.delete(sheet);
    }
    if (sheet.dataset.drawn !== String(seq)) return;
    sheet.replaceChildren(canvas);
    sheet.setAttribute("role", "img");
    sheet.setAttribute("aria-label", this.labels.page.replace("{n}", sheet.dataset.page));
    this.paintMarks(sheet);
    this.drawText(sheet, size, viewport, seq);
  }

  // The page's text over the picture, transparent, so it can be selected, copied and found.
  async drawText(sheet, size, viewport, seq) {
    const TextLayer = this.pdfjs?.TextLayer;
    if (!TextLayer) return;
    let content;
    try {
      content = await this.pageText(size.page);
    } catch {
      return;
    }
    if (sheet.dataset.drawn !== String(seq) || sheet.querySelector(".textLayer")) return;
    const div = document.createElement("div");
    div.className = "textLayer";
    sheet.append(div);
    sheet.removeAttribute("role"); // the page's text is there for screen readers now
    const layer = new TextLayer({ textContentSource: content, container: div, viewport });
    this.textLayers.set(sheet, layer);
    try {
      await layer.render();
      const end = document.createElement("div");
      end.className = "endOfContent";
      div.append(end);
    } catch {
      div.remove();
    } finally {
      if (this.textLayers.get(sheet) === layer) this.textLayers.delete(sheet);
    }
  }

  forget(sheet) {
    this.tasks.get(sheet)?.cancel?.();
    this.tasks.delete(sheet);
    this.textLayers.get(sheet)?.cancel?.();
    this.textLayers.delete(sheet);
    delete sheet.dataset.drawn;
    sheet.replaceChildren();
  }

  // -- Text of the pages ---------------------------------------------------------
  // Read once per rendered document, for headings, finding and following the cursor.
  textOf(n) {
    const size = this.sizes[n - 1];
    return size ? this.pageText(size.page) : Promise.resolve({ items: [] });
  }

  // A page's text, read once (pages of a document not swapped in yet too).
  pageText(page) {
    this.texts ??= new WeakMap();
    let text = this.texts.get(page);
    if (!text) {
      text = page.getTextContent();
      this.texts.set(page, text);
      text.catch(() => this.texts.delete(page));
    }
    return text;
  }

  async allText() {
    const doc = this.doc;
    const out = [];
    for (let i = 1; i <= this.sizes.length; i++) {
      out.push(await this.textOf(i));
      if (doc !== this.doc) throw new Error("replaced");
    }
    return out;
  }

  // The pages' text as one model (pageModel for each), kept with the document.
  async model() {
    const doc = this.doc;
    if (this.modelDoc !== doc) {
      this.modelDoc = doc;
      this.modelPromise = this.allText().then((contents) => {
        const pages = contents.map((c, i) => pageModel(c, this.sizes[i]?.page.view ?? [0, 0, 0, 0]));
        return { pages, tokens: tokensOf(pages) };
      });
      this.modelPromise.catch(() => { if (this.modelDoc === doc) this.modelDoc = null; });
    }
    return this.modelPromise;
  }

  async handlePick(event) {
    const sheet = event.target.closest?.(".pdf-sheet");
    if (!sheet || !this.doc || !this.onPick) return;
    event.preventDefault();
    window.getSelection?.()?.removeAllRanges();
    const n = Number(sheet.dataset.page);
    const size = this.sizes[n - 1];
    if (!size) return;
    const rect = sheet.getBoundingClientRect();
    const cssX = event.clientX - rect.left;
    const cssY = event.clientY - rect.top;
    this.mark(sheet, cssX, cssY);
    const viewport = size.page.getViewport({ scale: size.scale });
    const [px, py] = viewport.convertToPdfPoint(cssX, cssY);
    const view = size.page.view; // [x0, y0, x1, y1], origin bottom left
    let words = [];
    let at = 0;
    try {
      ({ words, at } = wordsAround(await this.textOf(n), px, py));
    } catch { /* the point alone still works for LaTeX */ }
    this.onPick({ page: n, pages: this.sizes.length, x: px - view[0], y: view[3] - py, words, at, url: this.url });
  }

  // -- Following the cursor -------------------------------------------------------
  // Scrolls to the *nth* heading with this title, found in the text of the pages, or to the top for
  // none (§6.4). The headings are read once per rendered document.
  async scrollToHeading(title, nth = 0) {
    if (!this.doc || !this.viewer || !this.visible) return false;
    const doc = this.doc;
    if (!title) {
      this.viewer.scrollTo({ top: 0, behavior: scrollBehavior() });
      return true;
    }
    const hit = await this.headingAt(title, nth);
    if (doc !== this.doc || !hit) return false;
    this.scrollToPoint(hit.page, 0, pageTop(hit, this.sizes[hit.page - 1]), { margin: 24 });
    return true;
  }

  async headingAt(title, nth = 0) {
    if (this.headingDoc !== this.doc) {
      this.headingDoc = this.doc;
      this.headings = this.allText().then((contents) => {
        const body = bodySize(contents);
        return contents.flatMap((content, i) => headingsOf(content, body).map((h) => ({ ...h, page: i + 1 })));
      }).catch(() => []);
    }
    const headings = await this.headings;
    const want = plainText(title);
    const hits = headings.filter((h) => sameHeading(h.text, want));
    return want ? hits[Math.min(nth, hits.length - 1)] ?? null : null;
  }

  // Marks the paragraph whose words these are (*at* is the word at the cursor) and brings it into
  // view if it isn't. *hint* is where in the document it lies (0 to 1), to choose between passages
  // that read the same. Returns whether it was found.
  async showParagraph({ words, at = 0, hint = 0.5 }) {
    if (!this.doc || !this.viewer || !this.visible) return false;
    const doc = this.doc;
    let model;
    try {
      model = await this.model();
    } catch {
      return false;
    }
    if (doc !== this.doc) return false;
    const found = matchPassage(model.tokens, words, at, hint);
    if (!found) {
      this.setMarks("para", []);
      return false;
    }
    const rects = rangeRects(model.pages, model.tokens, found.from, found.to);
    this.setMarks("para", rects);
    this.reveal(rects);
    return true;
  }

  // Marks boxes in PDF points from the pages' top left ([{page, x, y, w, h}], from SyncTeX).
  showBoxes(boxes) {
    if (!this.doc || !boxes?.length) return false;
    this.setMarks("para", boxes);
    this.reveal(boxes);
    return true;
  }

  clearParagraph() {
    this.setMarks("para", []);
  }

  // Scrolls so the first of *rects* shows, if none of it does.
  reveal(rects) {
    const r = rects[0];
    const size = r && this.sizes[r.page - 1];
    const sheet = r && this.pages.children[r.page - 1];
    if (!size || !sheet) return;
    const vr = this.viewer.getBoundingClientRect();
    const sr = sheet.getBoundingClientRect();
    const top = sr.top + r.y * size.scale;
    const last = rects.filter((q) => q.page === r.page).at(-1);
    const bottom = sr.top + (last.y + last.h) * size.scale;
    if (top >= vr.top + 8 && Math.min(bottom, top + 120) <= vr.bottom - 8) return;
    this.scrollToPoint(r.page, r.x, r.y, { margin: Math.min(this.viewer.clientHeight / 3, 120) });
  }

  // Scrolls so the point (PDF points from the page's top left) is *margin* below the viewer's top.
  scrollToPoint(page, x, y, { margin = 24, behavior = scrollBehavior() } = {}) {
    const sheet = this.pages.children[page - 1];
    const size = this.sizes[page - 1];
    if (!sheet || !size) return;
    const vr = this.viewer.getBoundingClientRect();
    const sr = sheet.getBoundingClientRect();
    const top = sr.top - vr.top + this.viewer.scrollTop + y * size.scale - margin;
    const opts = { top: Math.max(0, top), behavior };
    if (this.viewer.scrollWidth > this.viewer.clientWidth) {
      const px = sr.left - vr.left + this.viewer.scrollLeft + x * size.scale;
      if (px < this.viewer.scrollLeft || px > this.viewer.scrollLeft + this.viewer.clientWidth - 40) {
        opts.left = Math.max(0, px - 24);
      }
    }
    this.viewer.scrollTo(opts);
  }

  // -- Marks: found text and the paragraph at the cursor ------------------------
  setMarks(kind, rects) {
    this.marks[kind] = rects;
    if (!this.pages) return;
    for (const sheet of this.pages.children) this.paintMarks(sheet);
  }

  paintMarks(sheet) {
    const n = Number(sheet.dataset.page);
    const size = this.sizes[n - 1];
    sheet.querySelector(".sheet-marks")?.remove();
    if (!size || !sheet.dataset.drawn) return;
    const all = [...this.marks.para.filter((r) => r.page === n).map((r) => ({ ...r, kind: "para" })),
                 ...this.marks.find.filter((r) => r.page === n).map((r) => ({ ...r, kind: "find" }))];
    if (!all.length) return;
    const layer = document.createElement("div");
    layer.className = "sheet-marks";
    layer.setAttribute("aria-hidden", "true");
    for (const r of all) {
      const box = document.createElement("span");
      box.className = `sheet-mark ${r.kind}${r.current ? " current" : ""}`;
      box.style.left = `${(r.x * size.scale).toFixed(1)}px`;
      box.style.top = `${(r.y * size.scale).toFixed(1)}px`;
      box.style.width = `${Math.max(2, r.w * size.scale).toFixed(1)}px`;
      box.style.height = `${Math.max(2, r.h * size.scale).toFixed(1)}px`;
      layer.append(box);
    }
    // Under the text layer, so the marked text can still be selected.
    const text = sheet.querySelector(".textLayer");
    if (text) text.before(layer);
    else sheet.append(layer);
  }

  // -- Find in the PDF ---------------------------------------------------------------
  async runFind(query, { keep = false, step = 0 } = {}) {
    const q = String(query ?? "");
    if (!this.doc) {
      this.find = { query: q, hits: [], index: -1 };
      this.updateFind();
      return;
    }
    const doc = this.doc;
    if (q !== this.find.query || keep || !this.find.hits.length) {
      const old = this.find;
      let model;
      try {
        model = await this.model();
      } catch {
        return;
      }
      if (doc !== this.doc || (this.findInput && this.findInput.value !== q && !keep)) return;
      const hits = findText(model.pages, q);
      // Keep the place: the first match from the page in view on, or the same one as before.
      let index = -1;
      if (hits.length) {
        index = keep && old.index >= 0 ? Math.min(old.index, hits.length - 1)
          : Math.max(0, hits.findIndex((h) => h.page >= (this.currentPage || 1)));
      }
      this.find = { query: q, hits, index };
    } else if (this.find.hits.length) {
      const n = this.find.hits.length;
      this.find.index = (this.find.index + step + n) % n;
    }
    const { hits, index } = this.find;
    this.setMarks("find", hits.flatMap((h, i) => h.rects.map((r) => ({ ...r, current: i === index }))));
    if (index >= 0 && !keep) {
      const h = hits[index];
      this.reveal(h.rects);
    }
    this.updateFind();
  }

  closeFind() {
    this.find = { query: "", hits: [], index: -1 };
    this.setMarks("find", []);
    if (this.findRow) this.findRow.hidden = true;
    if (this.findInput) this.findInput.value = "";
    this.updateFind();
  }

  openFind() {
    if (!this.findRow) return false;
    this.findRow.hidden = false;
    this.findInput.focus();
    this.findInput.select();
    return true;
  }

  updateFind() {
    if (!this.findCount) return;
    const { query, hits, index } = this.find;
    this.findCount.textContent = !query.trim() ? ""
      : hits.length ? this.labels.matches.replace("{i}", String(index + 1)).replace("{n}", String(hits.length))
        : this.labels.noMatches;
    this.findCount.classList.toggle("error", Boolean(query.trim()) && !hits.length);
  }

  // -- Zoom and pages ------------------------------------------------------------------
  // Where the viewer is: the page at its top and the point of it there, to zoom around.
  anchor() {
    if (!this.viewer || !this.sizes.length) return null;
    const vr = this.viewer.getBoundingClientRect();
    const sheets = [...this.pages.children];
    const at = sheets.find((s) => s.getBoundingClientRect().bottom > vr.top + 1) ?? sheets[0];
    const n = Number(at.dataset.page);
    const size = this.sizes[n - 1];
    const sr = at.getBoundingClientRect();
    const dy = Math.max(0, sr.top - vr.top);
    return { page: n, y: Math.max(0, vr.top - sr.top) / size.scale, dy, x: Math.max(0, vr.left - sr.left) / size.scale,
             dx: Math.max(0, sr.left - vr.left) };
  }

  setZoom(zoom) {
    const next = zoom === "fit" || zoom === "page" ? zoom : clampZoom(Number(zoom));
    if (!Number.isFinite(next) && typeof next !== "string") return;
    const anchor = this.anchor();
    this.zoom = next;
    try { localStorage.setItem(ZOOM_KEY, String(next)); } catch { /* private window */ }
    this.updateBar();
    if (this.doc) this.layout(this.doc, this.seq, { anchor });
  }

  // The scale shown now (1 is 100 %), also when the page fits the viewer.
  effectiveZoom() {
    if (typeof this.zoom === "number") return this.zoom;
    const s = this.sizes[Math.max(0, (this.currentPage || 1) - 1)];
    return s ? s.scale / CSS_PER_PT : 1;
  }

  stepZoom(direction) {
    this.setZoom(nextZoom(this.effectiveZoom(), direction));
  }

  goToPage(n) {
    const page = Math.max(1, Math.min(this.sizes.length, Math.round(Number(n)) || 1));
    this.scrollToPoint(page, 0, 0, { margin: PAD / 2, behavior: "auto" });
    this.currentPage = page;
    this.updateBar();
  }

  trackPage(force = false) {
    if (this.pageFrame && !force) return;
    const run = () => {
      this.pageFrame = 0;
      if (!this.viewer || !this.sizes.length) return;
      const vr = this.viewer.getBoundingClientRect();
      const line = vr.top + Math.min(vr.height * 0.3, 160);
      let n = 1;
      for (const sheet of this.pages.children) {
        if (sheet.getBoundingClientRect().top <= line) n = Number(sheet.dataset.page);
        else break;
      }
      if (n !== this.currentPage) {
        this.currentPage = n;
        this.updateBar();
      }
    };
    if (force) run();
    else this.pageFrame = requestAnimationFrame(run);
  }

  handleKey(event) {
    if (event.target.closest?.(".pdf-bar")) return;
    const mod = event.ctrlKey || event.metaKey;
    if (mod && (event.key === "f" || event.key === "F") && this.openFind()) {
      event.preventDefault();
    } else if (!mod && !event.altKey && (event.key === "+" || event.key === "=")) {
      event.preventDefault();
      this.stepZoom(1);
    } else if (!mod && !event.altKey && event.key === "-") {
      event.preventDefault();
      this.stepZoom(-1);
    } else if (!mod && !event.altKey && event.key === "0") {
      event.preventDefault();
      this.setZoom("fit");
    } else if (event.key === "Escape" && this.find.query) {
      this.closeFind();
    }
  }

  // -- The PDF bar ---------------------------------------------------------------------
  wireBar() {
    const bar = this.bar;
    if (!bar || bar.dataset.wired) return;
    bar.dataset.wired = "1";
    const q = (sel) => bar.querySelector(sel);
    this.zoomSelect = q("[data-pdf-zoom]");
    this.pageInput = q("[data-pdf-page]");
    this.pageCount = q("[data-pdf-pages]");
    this.findRow = q("[data-pdf-find-row]");
    this.findInput = q("[data-pdf-find]");
    this.findCount = q("[data-pdf-find-count]");
    q("[data-pdf-zoom-out]")?.addEventListener("click", () => this.stepZoom(-1));
    q("[data-pdf-zoom-in]")?.addEventListener("click", () => this.stepZoom(1));
    this.zoomSelect?.addEventListener("change", () => {
      const v = this.zoomSelect.value;
      this.setZoom(v === "fit" || v === "page" ? v : Number(v));
    });
    this.pageInput?.addEventListener("change", () => this.goToPage(this.pageInput.value));
    this.pageInput?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        this.goToPage(this.pageInput.value);
        this.viewer?.focus({ preventScroll: true });
      }
    });
    q("[data-pdf-open-find]")?.addEventListener("click", () => {
      if (this.findRow.hidden) this.openFind();
      else this.closeFind();
    });
    let findTimer = null;
    this.findInput?.addEventListener("input", () => {
      clearTimeout(findTimer);
      findTimer = setTimeout(() => this.runFind(this.findInput.value), 150);
    });
    this.findInput?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        clearTimeout(findTimer);
        if (this.findInput.value !== this.find.query) this.runFind(this.findInput.value);
        else this.runFind(this.findInput.value, { step: event.shiftKey ? -1 : 1 });
      } else if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        this.closeFind();
        this.viewer?.focus({ preventScroll: true });
      }
    });
    q("[data-pdf-find-prev]")?.addEventListener("click", () => this.runFind(this.findInput.value, { step: -1 }));
    q("[data-pdf-find-next]")?.addEventListener("click", () => this.runFind(this.findInput.value, { step: 1 }));
    q("[data-pdf-find-close]")?.addEventListener("click", () => this.closeFind());
    this.updateBar();
  }

  updateBar() {
    if (!this.bar) return;
    const n = this.sizes.length;
    if (this.pageInput) {
      this.pageInput.max = String(Math.max(1, n));
      if (document.activeElement !== this.pageInput) this.pageInput.value = n ? String(this.currentPage || 1) : "";
      this.pageInput.disabled = !n;
    }
    if (this.pageCount) this.pageCount.textContent = n ? String(n) : "–";
    if (this.zoomSelect) {
      const select = this.zoomSelect;
      const value = typeof this.zoom === "number" ? String(this.zoom) : this.zoom;
      let custom = select.querySelector("option[data-custom]");
      const listed = [...select.options].some((o) => !o.dataset.custom && o.value === value);
      if (typeof this.zoom === "number" && !listed) {
        if (!custom) {
          custom = document.createElement("option");
          custom.dataset.custom = "1";
          select.append(custom);
        }
        custom.value = value;
        custom.textContent = `${Math.round(this.zoom * 100)} %`;
      } else {
        custom?.remove();
      }
      select.value = value;
      // Fit width and Fit page say how large that is now.
      for (const option of select.options) {
        if (option.value === "fit" || option.value === "page") {
          option.dataset.label ??= option.textContent;
          option.textContent = option.value === this.zoom && n
            ? `${option.dataset.label} (${Math.round(this.effectiveZoom() * 100)} %)` : option.dataset.label;
        }
      }
    }
  }

  scrollRatio() {
    if (!this.viewer) return 0;
    const max = Math.max(0, this.viewer.scrollHeight - this.viewer.clientHeight);
    return max > 0 ? this.viewer.scrollTop / max : 0;
  }

  mark(sheet, x, y) {
    const dot = document.createElement("span");
    dot.className = "pdf-pick";
    dot.style.left = `${x}px`;
    dot.style.top = `${y}px`;
    sheet.append(dot);
    setTimeout(() => dot.remove(), 900);
  }

  setVisible(visible) {
    this.visible = visible;
    if (this.bar) this.bar.hidden = !visible;
    if (!this.viewer) return;
    this.viewer.hidden = !visible;
    if (visible && this.pendingRender) {
      const doc = this.nextDoc ?? this.doc;
      this.nextDoc = null;
      if (doc) this.layout(doc, this.seq);
    }
  }
}

function readZoom() {
  let raw = null;
  try { raw = localStorage.getItem(ZOOM_KEY); } catch { /* private window */ }
  if (raw === "page") return "page";
  const n = Number(raw);
  return raw && Number.isFinite(n) ? clampZoom(n) : "fit";
}

export function clampZoom(z) {
  return Math.min(ZOOM_STEPS.at(-1), Math.max(ZOOM_STEPS[0], Math.round(z * 100) / 100));
}

// The next zoom step up (direction 1) or down (-1) from *current*.
export function nextZoom(current, direction) {
  if (direction > 0) return ZOOM_STEPS.find((s) => s > current + 0.005) ?? ZOOM_STEPS.at(-1);
  return [...ZOOM_STEPS].reverse().find((s) => s < current - 0.005) ?? ZOOM_STEPS[0];
}

// The top of a heading (from headingsOf) in PDF points from its page's top.
function pageTop(hit, size) {
  const view = size?.page.view ?? [0, 0, 0, 0];
  return view[3] - hit.top;
}

// The words of the page's text around a point (PDF user space), in reading order, and which of
// them is under the point. Lines that end in a hyphen join the next line's first word.
export function wordsAround(content, px, py) {
  let text = "";
  let target = -1;
  let best = Infinity;
  for (const item of content.items) {
    if (typeof item.str !== "string") continue;
    const [a, b, c, d, e, f] = item.transform;
    const height = Math.hypot(c, d) || Math.hypot(a, b) || 10;
    const x0 = e;
    const x1 = e + (item.width || 0);
    const yTop = f + height * 0.8;
    const yBottom = f - height * 0.25;
    // Distance from the point to the item's box: zero inside it.
    const dx = px < x0 ? x0 - px : px > x1 ? px - x1 : 0;
    const dy = py < yBottom ? yBottom - py : py > yTop ? py - yTop : 0;
    const dist = dy * 3 + dx;
    const start = text.length;
    if (dist < best && item.str.trim()) {
      best = dist;
      const frac = item.width > 0 ? Math.min(1, Math.max(0, (px - x0) / item.width)) : 0;
      target = start + Math.min(item.str.length - 1, Math.floor(frac * item.str.length));
    }
    if (item.hasEOL && item.str.endsWith("-")) text += item.str.slice(0, -1);
    else text += item.str + (item.hasEOL ? " " : "");
  }
  const all = [...text.matchAll(WORD)].map((m) => ({ word: m[0], start: m.index, end: m.index + m[0].length }));
  if (!all.length) return { words: [], at: 0 };
  let at = all.findIndex((w) => target < w.end);
  if (at < 0) at = all.length - 1;
  const from = Math.max(0, at - WORDS_AROUND);
  const to = Math.min(all.length, at + WORDS_AROUND + 1);
  return { words: all.slice(from, to).map((w) => w.word), at: at - from };
}

// -- The pages' text as a model, for finding and following ---------------------------

// One page's text: the string as it reads (lines joined with a space, a hyphen at a line's end
// joining the word), the items it came from with their place in it, and the page's box.
export function pageModel(content, view = [0, 0, 0, 0]) {
  let text = "";
  const spans = [];
  for (const item of content.items ?? []) {
    if (typeof item.str !== "string") continue;
    const [a, b, c, d, e, f] = item.transform;
    const height = Math.hypot(c, d) || Math.hypot(a, b) || 10;
    const hyphen = item.hasEOL && item.str.endsWith("-");
    const str = hyphen ? item.str.slice(0, -1) : item.str;
    if (str.length) {
      spans.push({ start: text.length, end: text.length + str.length, len: item.str.length, str: item.str, x: e, y: f,
                   width: item.width || 0, height });
    }
    text += str;
    if (item.hasEOL && !hyphen) text += " ";
  }
  return { text, spans, view };
}

// Lower case without accents, one character for one (so offsets stay those of the text): "Ü" is "u".
export function foldChars(text) {
  let out = "";
  for (const ch of text) {
    const base = ch.normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase();
    // Ligatures and the like (ﬁ is fi) keep their first letter, so the text keeps its length.
    const one = base.length ? base : ch.toLowerCase();
    out += ch.length === 2 ? (one + " ").slice(0, 2) : one[0] ?? " ";
  }
  return out;
}

// Roughly how wide a character is set, relative to others, in proportional fonts.
function charWeight(ch) {
  if (/[iljtfr.,;:'!|ı ]/.test(ch)) return 0.55;
  if (/[mwMW]/.test(ch)) return 1.5;
  if (/[A-Z0-9]/.test(ch)) return 1.2;
  return 1;
}

// The share of the width of *str* its first *n* characters take.
export function charShare(str, n) {
  if (!str || n <= 0) return 0;
  if (n >= str.length) return 1;
  let before = 0;
  let all = 0;
  for (let i = 0; i < str.length; i++) {
    const w = charWeight(str[i]);
    if (i < n) before += w;
    all += w;
  }
  return all > 0 ? before / all : n / str.length;
}

// Rectangles (PDF points from the page's top left) covering text offsets from..to of a page.
export function textRects(page, from, to) {
  const rects = [];
  const top = page.view[3];
  const left = page.view[0];
  for (const s of page.spans) {
    if (s.end <= from || s.start >= to) continue;
    const a = Math.max(from, s.start) - s.start;
    const b = Math.min(to, s.end) - s.start;
    const x0 = s.x + s.width * charShare(s.str, a);
    const x1 = s.x + s.width * charShare(s.str, b);
    const rect = { x: x0 - left, y: top - (s.y + s.height * 0.85), w: Math.max(1, x1 - x0), h: s.height * 1.15 };
    // Pieces of one line become one rectangle.
    const prev = rects.at(-1);
    if (prev && Math.abs(prev.y - rect.y) < s.height * 0.3 && rect.x - (prev.x + prev.w) < s.height * 1.5
        && rect.x >= prev.x - 1) {
      const right = Math.max(prev.x + prev.w, rect.x + rect.w);
      prev.y = Math.min(prev.y, rect.y);
      prev.h = Math.max(prev.h, rect.h);
      prev.w = right - prev.x;
    } else {
      rects.push(rect);
    }
  }
  return rects;
}

// Every place *query* occurs, ignoring case, accents and how spaces run: [{page, from, to, rects}].
export function findText(pages, query, max = 2000) {
  const q = foldChars(String(query).trim()).replace(/\s+/g, " ");
  if (!q) return [];
  const hits = [];
  pages.forEach((page, i) => {
    if (hits.length >= max) return;
    const folded = foldChars(page.text);
    // Runs of spaces count as one: the folded page text with its spaces squeezed, and where each
    // of its characters came from.
    let squeezed = "";
    const from = [];
    for (let k = 0; k < folded.length; k++) {
      const ch = /\s/.test(folded[k]) ? " " : folded[k];
      if (ch === " " && squeezed.endsWith(" ")) continue;
      squeezed += ch;
      from.push(k);
    }
    let at = squeezed.indexOf(q);
    while (at >= 0 && hits.length < max) {
      const start = from[at];
      const end = from[at + q.length - 1] + 1;
      hits.push({ page: i + 1, from: start, to: end, rects: textRects(page, start, end).map((r) => ({ ...r, page: i + 1 })) });
      at = squeezed.indexOf(q, at + Math.max(1, q.length));
    }
  });
  return hits;
}

// The words of all pages in reading order: [{w, page, start, end}] (page counted from 0).
export function tokensOf(pages) {
  const tokens = [];
  pages.forEach((page, p) => {
    const folded = foldChars(page.text);
    for (const m of folded.matchAll(WORD)) tokens.push({ w: m[0], page: p, start: m.index, end: m.index + m[0].length });
  });
  return tokens;
}

// The run of tokens a passage of the source reads as: *words* are its words in order, *at* the one
// at the cursor, *hint* where in the document it lies (0 to 1). Words may be missing or added on
// either side (rendered citations, footnote marks, numbers), so each side is followed with a
// lookahead rather than compared word for word. Returns {from, to, score} (token indices) or null.
export function matchPassage(tokens, words, at = 0, hint = 0.5) {
  const q = words.map((w) => foldChars(String(w))).filter((w) => /^[\p{L}\p{N}]+$/u.test(w));
  if (!q.length || !tokens.length) return null;
  at = Math.max(0, Math.min(at, q.length - 1));
  const where = new Map();
  tokens.forEach((t, i) => {
    let list = where.get(t.w);
    if (!list) where.set(t.w, list = []);
    list.push(i);
  });
  const LOOK = 6;
  const extend = (i, j) => {
    let score = 1;
    let lo = i;
    let hi = i;
    for (const step of [-1, 1]) {
      let pos = i;
      let misses = 0;
      for (let qj = j + step; qj >= 0 && qj < q.length && misses < 5; qj += step) {
        let found = -1;
        for (let look = 1; look <= LOOK; look++) {
          const p = pos + step * look;
          if (p < 0 || p >= tokens.length) break;
          if (tokens[p].w === q[qj]) { found = p; break; }
        }
        if (found < 0) {
          misses += 1;
        } else {
          score += 1;
          pos = found;
          if (step < 0) lo = found;
          else hi = found;
        }
      }
    }
    return { score, lo, hi };
  };
  const order = [...q.keys()].sort((a, b) => Math.abs(a - at) - Math.abs(b - at));
  let best = null;
  let tried = 0;
  for (const j of order) {
    if (q[j].length < 3 && q.length > 2) continue;
    const spots = where.get(q[j]);
    if (!spots || spots.length > 400) continue;
    tried += 1;
    for (const i of spots) {
      const { score, lo, hi } = extend(i, j);
      const rank = [score, -Math.abs(j - at), -Math.abs(i / Math.max(1, tokens.length - 1) - hint)];
      if (!best || rank[0] > best.rank[0] || (rank[0] === best.rank[0]
          && (rank[1] > best.rank[1] || (rank[1] === best.rank[1] && rank[2] > best.rank[2])))) {
        best = { rank, lo, hi };
      }
    }
    // A confident match around the cursor's word itself, or enough words tried.
    if (best && (best.rank[0] >= Math.min(q.length, 8) || tried >= 6)) break;
  }
  const need = q.length <= 2 ? q.length : Math.min(3, q.length);
  if (!best || best.rank[0] < need) return null;
  return { from: best.lo, to: best.hi, score: best.rank[0] };
}

// The rectangles of tokens from..to, on every page they run over: [{page, x, y, w, h}].
export function rangeRects(pages, tokens, from, to) {
  const out = [];
  let i = from;
  while (i <= to) {
    const p = tokens[i].page;
    let j = i;
    while (j + 1 <= to && tokens[j + 1].page === p) j += 1;
    for (const r of textRects(pages[p], tokens[i].start, tokens[j].end)) out.push({ ...r, page: p + 1 });
    i = j + 1;
  }
  return out;
}

// The words of the paragraph around *pos* in a Markdown or LaTeX source, with markup that doesn't
// print as words left out, and which of them is at *pos*: {words, at, from, to} (the paragraph's
// offsets), or null outside of text (front matter, code, a blank line).
export function paragraphAt(text, pos, { latex = false, maxWords = 120 } = {}) {
  pos = Math.max(0, Math.min(pos, text.length));
  // The front matter isn't text.
  if (!latex && text.startsWith("---")) {
    const m = /^---[ \t]*\r?\n[\s\S]*?\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|$)/.exec(text);
    if (m && pos < m[0].length) return null;
  }
  const lineStart = (p) => text.lastIndexOf("\n", p - 1) + 1;
  const lineEnd = (p) => { const e = text.indexOf("\n", p); return e < 0 ? text.length : e; };
  const blank = (s, e) => !text.slice(s, e).trim();
  let from = lineStart(pos);
  let to = lineEnd(pos);
  if (blank(from, to)) return null;
  // A heading is a paragraph of its own.
  const heading = (s, e) => (latex ? /^\s*\\(?:part|chapter|(?:sub)*section|paragraph)\*?\{/ : /^#{1,6}\s/)
    .test(text.slice(s, e));
  const fence = (s, e) => /^\s*(```|~~~|:::)/.test(text.slice(s, e));
  if (!heading(from, to)) {
    while (from > 0) {
      const ps = lineStart(from - 1);
      const pe = from - 1;
      if (blank(ps, pe) || heading(ps, pe) || fence(ps, pe)) break;
      from = ps;
    }
    while (to < text.length) {
      const ns = to + 1;
      const ne = lineEnd(ns);
      if (blank(ns, ne) || heading(ns, ne) || fence(ns, ne)) break;
      to = ne;
    }
  }
  let para = text.slice(from, to);
  const blankOut = (re) => { para = para.replace(re, (m) => " ".repeat(m.length)); };
  if (latex) {
    blankOut(/(?<!\\)%.*$/gm); // comments
    blankOut(/\\(?:cite[a-z]*|ref|eqref|autoref|cref|label|includegraphics|url|href)\*?(?:\[[^\]]*\])*\{[^}]*\}/g);
    blankOut(/\$\$[\s\S]*?\$\$|\$[^$\n]*\$|\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]/g);
    blankOut(/\\[a-zA-Z@]+\*?/g); // other commands: their arguments are text
    blankOut(/[{}]/g);
  } else {
    blankOut(/<!--[\s\S]*?-->/g);
    blankOut(/\^\[(?:[^[\]]|\[[^\]]*\])*\]/g); // inline notes print at the foot of the page
    blankOut(/\[\^[^\]]+\]/g);
    blankOut(/\[[^\]]*@[^\]]*\]/g); // [see @doe, p. 4]
    blankOut(/(?<![\w.])-?@[\w:.#$%&\-+?<>~/]+/g);
    blankOut(/\{[#.][^}]*\}/g); // attributes
    blankOut(/\]\([^)]*\)/g); // link addresses
    blankOut(/\$\$[\s\S]*?\$\$|\$[^$\n]+\$/g); // math
    blankOut(/<\/?[a-zA-Z][^>]*>/g);
    blankOut(/`[^`]*`/g);
  }
  const folded = foldChars(para);
  const all = [...folded.matchAll(WORD)].map((m) => ({ w: m[0], start: m.index, end: m.index + m[0].length }));
  if (!all.length) return null;
  const rel = pos - from;
  let at = all.findIndex((w) => rel < w.end);
  if (at < 0) at = all.length - 1;
  const lo = Math.max(0, Math.min(at - Math.floor(maxWords / 2), all.length - maxWords));
  const slice = all.slice(lo, lo + maxWords);
  return { words: slice.map((w) => w.w), at: at - lo, from, to };
}

// Text compared without case, accents, punctuation or spacing: "Über die Stadt" is "uber die stadt".
export function plainText(s) {
  return String(s).normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, " ").trim();
}

// The headings of a page's text: lines set larger than the body text (*body*, in points), with a
// heading that wraps joined into one. Each is its plain text and the top of its first line in PDF
// user space. Running heads and page numbers are smaller than the body; the table of contents
// lists headings at body size, so neither counts.
export function headingsOf(content, body) {
  const lines = [];
  for (const item of content.items) {
    if (typeof item.str !== "string" || !item.str.trim()) continue;
    const [a, b, c, d, e, f] = item.transform;
    const size = Math.hypot(c, d) || Math.hypot(a, b) || 0;
    let line = lines.find((l) => Math.abs(l.y - f) < Math.max(1, size * 0.3));
    if (!line) lines.push(line = { y: f, x: e, size, parts: [] });
    line.size = Math.max(line.size, size);
    line.x = Math.min(line.x, e);
    line.parts.push({ x: e, str: item.str });
  }
  lines.sort((p, q) => q.y - p.y);
  const out = [];
  let last = null;
  for (const line of lines) {
    if (line.size < body * 1.12) { last = null; continue; }
    const text = line.parts.sort((p, q) => p.x - q.x).map((p) => p.str).join(" ");
    if (last && Math.abs(last.size - line.size) < 0.5 && last.bottom - line.y < line.size * 1.8) {
      last.raw += ` ${text}`;
      last.bottom = line.y;
    } else {
      last = { raw: text, size: line.size, top: line.y + line.size, bottom: line.y };
      out.push(last);
    }
  }
  return out.filter((h) => h.raw.length <= 300).map((h) => ({ text: plainText(h.raw), top: h.top, size: h.size }));
}

// The size of the body text: the size most characters are set in.
export function bodySize(contents) {
  const chars = new Map();
  for (const content of contents) {
    for (const item of content.items) {
      if (typeof item.str !== "string") continue;
      const [a, b, c, d] = item.transform;
      const size = Math.round((Math.hypot(c, d) || Math.hypot(a, b) || 0) * 2) / 2;
      chars.set(size, (chars.get(size) ?? 0) + item.str.trim().length);
    }
  }
  let best = 0;
  let size = 10;
  for (const [s, n] of chars) if (n > best) { best = n; size = s; }
  return size;
}

// Whether a heading line is the heading *want* (plain text): the same, or with its number in front
// ("2.1 Method" for "Method").
export function sameHeading(line, want) {
  return line === want || line.replace(/^(?:(?:\d+|[ivxlc]{1,4}|[a-z])\s+)+/, "") === want;
}

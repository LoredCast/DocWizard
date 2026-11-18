// PDF preview with the vendored PDF.js (ARCHITECTURE.md §6.4). Pages are laid out at once at
// their final size and drawn when they come near the visible part, so long documents stay light;
// a new render replaces the pages only once the ones in view are drawn, at the same place.
// Double-clicking (double-tapping) a spot asks for its source: onPick gets the page, the point in
// PDF points from the page's top left, and the words around it in reading order.

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

export class PdfPreview {
  constructor(container, { onError, onLoad, onPick, hint, scrollRatio } = {}) {
    this.container = container;
    this.startRatio = scrollRatio ?? 0; // where the first render opens, as a share of the scroll height
    this.hint = hint ?? "";
    this.onError = onError ?? null;
    this.onLoad = onLoad ?? null;
    this.onPick = onPick ?? null;
    this.viewer = null;
    this.pages = null;
    this.url = null;
    this.doc = null;
    this.sizes = [];
    this.visible = true;
    this.seq = 0;
    this.pendingRender = false;
    this.lastWidth = 0;
    this.resizeTimer = null;
    this.observer = null;
    this.tasks = new Map();
  }

  ensureViewer() {
    if (this.viewer) return;
    this.viewer = document.createElement("div");
    this.viewer.className = "pdf-viewer";
    this.viewer.hidden = !this.visible;
    if (this.onPick && this.hint) this.viewer.title = this.hint;
    this.pages = document.createElement("div");
    this.pages.className = "pdf-pages";
    this.viewer.append(this.pages);
    this.container.append(this.viewer);
    this.viewer.addEventListener("dblclick", (event) => this.handlePick(event));
    if (typeof ResizeObserver === "function") {
      new ResizeObserver(() => {
        if (!this.visible || !this.doc) return;
        const w = this.viewer.clientWidth;
        if (w <= 0) return;
        if (this.pendingRender || Math.abs(w - this.lastWidth) > 16) {
          clearTimeout(this.resizeTimer);
          this.resizeTimer = setTimeout(() => this.layout(this.doc, this.seq), 120);
        }
      }).observe(this.viewer);
    }
  }

  show(url) {
    if (!url || url === this.url) return;
    this.url = url;
    this.ensureViewer();
    this.load();
  }

  reload() {
    if (!this.url) return;
    this.ensureViewer();
    this.load();
  }

  async load() {
    const seq = ++this.seq;
    const url = this.url;
    try {
      const [pdfjs, res] = await Promise.all([loadPdfJs(), fetch(url, { credentials: "same-origin" })]);
      if (seq !== this.seq) return;
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const buf = await res.arrayBuffer();
      if (seq !== this.seq) return;
      const doc = await pdfjs.getDocument({ data: new Uint8Array(buf), verbosity: pdfjs.VerbosityLevel?.ERRORS ?? 0 }).promise;
      if (seq !== this.seq) {
        doc.destroy();
        return;
      }
      await this.layout(doc, seq);
    } catch {
      if (seq !== this.seq) return;
      this.onError?.(url);
    }
  }

  // Lays out every page of *doc* at the viewer's width, draws the ones that will be in view and
  // swaps them in at the same relative scroll position.
  async layout(doc, seq) {
    if (!this.visible || this.viewer.clientWidth <= 0) {
      this.pendingRender = true;
      this.nextDoc = doc;
      return;
    }
    this.pendingRender = false;
    const width = Math.max(240, this.viewer.clientWidth - 2 * PAD);
    this.lastWidth = this.viewer.clientWidth;
    const sizes = [];
    for (let i = 1; i <= doc.numPages; i++) {
      const page = await doc.getPage(i);
      if (seq !== this.seq) return;
      const base = page.getViewport({ scale: 1 });
      const scale = width / base.width;
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
      frag.append(sheet);
      return sheet;
    });
    // Where the viewer will be once these pages are in, and the pages that will show there.
    const total = sizes.reduce((sum, s) => sum + s.height + GAP, PAD * 2 - GAP);
    const top = ratio * Math.max(0, total - this.viewer.clientHeight);
    const bottom = top + this.viewer.clientHeight;
    let y = PAD;
    const first = [];
    sizes.forEach((s, i) => {
      if (y + s.height >= top - 200 && y <= bottom + 200) first.push(i);
      y += s.height + GAP;
    });
    await Promise.all(first.map((i) => this.draw(sheets[i], sizes[i], seq)));
    if (seq !== this.seq) return;

    this.observer?.disconnect();
    for (const task of this.tasks.values()) task.cancel?.();
    this.tasks.clear();
    const old = this.doc;
    this.headingDoc = null;
    this.doc = doc;
    this.sizes = sizes;
    this.pages.replaceChildren(frag);
    this.viewer.scrollTop = top;
    if (old && old !== doc) old.destroy();

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
    this.onLoad?.();
  }

  async draw(sheet, size, seq) {
    if (!size || sheet.dataset.drawn === String(seq) || sheet.querySelector("canvas")) return;
    sheet.dataset.drawn = String(seq);
    const dpr = Math.min(window.devicePixelRatio || 1, 2.5);
    const viewport = size.page.getViewport({ scale: size.scale });
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.floor(viewport.width * dpr));
    canvas.height = Math.max(1, Math.floor(viewport.height * dpr));
    canvas.setAttribute("aria-label", `Page ${sheet.dataset.page}`);
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
    if (sheet.dataset.drawn === String(seq)) sheet.replaceChildren(canvas);
  }

  forget(sheet) {
    this.tasks.get(sheet)?.cancel?.();
    this.tasks.delete(sheet);
    delete sheet.dataset.drawn;
    sheet.replaceChildren();
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
      ({ words, at } = wordsAround(await size.page.getTextContent(), px, py));
    } catch { /* the point alone still works for LaTeX */ }
    this.onPick({ page: n, pages: this.sizes.length, x: px - view[0], y: view[3] - py, words, at, url: this.url });
  }

  // Follows the editor's cursor (§6.4): scrolls to the *nth* heading with this title, found in the
  // text of the pages, or to the top for none. The headings are read once per rendered document.
  async scrollToHeading(title, nth = 0) {
    if (!this.doc || !this.viewer || !this.visible) return false;
    const doc = this.doc;
    if (!title) {
      this.viewer.scrollTo({ top: 0, behavior: "smooth" });
      return true;
    }
    if (this.headingDoc !== doc) {
      this.headingDoc = doc;
      this.headings = (async () => {
        const contents = [];
        for (const size of this.sizes) contents.push(await size.page.getTextContent());
        const body = bodySize(contents);
        return contents.flatMap((content, i) => headingsOf(content, body).map((h) => ({ ...h, page: i + 1 })));
      })().catch(() => []);
    }
    const headings = await this.headings;
    if (doc !== this.doc) return false;
    const want = plainText(title);
    const hits = headings.filter((h) => sameHeading(h.text, want));
    const hit = hits[Math.min(nth, hits.length - 1)];
    if (!want || !hit) return false;
    const sheet = this.pages.children[hit.page - 1];
    const size = this.sizes[hit.page - 1];
    if (!sheet || !size) return false;
    const view = size.page.view;
    const y = sheet.getBoundingClientRect().top - this.viewer.getBoundingClientRect().top + this.viewer.scrollTop
      + (view[3] - hit.top) * size.scale;
    this.viewer.scrollTo({ top: Math.max(0, y - 24), behavior: "smooth" });
    return true;
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
    if (!this.viewer) return;
    this.viewer.hidden = !visible;
    if (visible && this.pendingRender) {
      const doc = this.nextDoc ?? this.doc;
      this.nextDoc = null;
      if (doc) this.layout(doc, this.seq);
    }
  }
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

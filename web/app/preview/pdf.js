// PDF preview: uses the browser's built-in viewer on desktop when supported,
// and lazy-loaded PDF.js on mobile browsers / browsers without inline PDF support (ARCHITECTURE.md §6.4).

let pdfjsPromise = null;

function usePdfJs() {
  if (navigator.pdfViewerEnabled === false) return true;
  if (window.matchMedia?.("(pointer: coarse)").matches) return true;
  return /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent);
}

async function loadPdfJs() {
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

export class PdfPreview {
  constructor(container, { onError, onLoad } = {}) {
    this.container = container;
    this.onError = onError ?? null;
    this.onLoad = onLoad ?? null;
    this.frame = null;
    this.viewer = null;
    this.pages = null;
    this.url = null;
    this.page = 1;
    this.visible = true;
    this.renderSeq = 0;
    this.docTask = null;
    this.pendingRender = false;
    this.lastWidth = 0;
    this.resizeTimer = null;
    this.ro = null;
  }

  ensureViewer() {
    if (this.viewer) return;
    this.viewer = document.createElement("div");
    this.viewer.className = "pdf-viewer";
    this.viewer.hidden = !this.visible;
    this.pages = document.createElement("div");
    this.pages.className = "pdf-pages";
    this.viewer.append(this.pages);
    this.container.append(this.viewer);
    if (typeof ResizeObserver === "function") {
      this.ro = new ResizeObserver(() => {
        if (!this.visible || !this.url) return;
        const w = this.viewer.clientWidth;
        if (w <= 0) return;
        if (this.pendingRender || Math.abs(w - this.lastWidth) > 16) {
          clearTimeout(this.resizeTimer);
          this.resizeTimer = setTimeout(() => this.renderPdf(), 120);
        }
      });
      this.ro.observe(this.viewer);
    }
  }

  show(url) {
    if (!url || url === this.url) return;
    this.url = url;
    if (!usePdfJs()) {
      // A new frame per render: the native viewer can't be told to reload in place.
      const frame = document.createElement("iframe");
      frame.title = "PDF preview";
      frame.className = "pdf-frame";
      frame.hidden = !this.visible;
      frame.src = `${url}#page=${this.page}`;
      if (this.frame) this.frame.replaceWith(frame);
      else this.container.append(frame);
      this.frame = frame;
      this.onLoad?.();
      return;
    }
    this.ensureViewer();
    this.renderPdf();
  }

  reload() {
    if (!this.url) return;
    if (usePdfJs()) {
      this.ensureViewer();
      this.renderPdf();
    } else {
      const prev = this.url;
      this.url = null;
      this.show(prev);
    }
  }

  async renderPdf() {
    if (!this.url || !this.viewer) return;
    if (!this.visible || this.viewer.clientWidth <= 0) {
      this.pendingRender = true;
      return;
    }
    this.pendingRender = false;
    const seq = ++this.renderSeq;
    const url = this.url;
    const availWidth = Math.max(240, this.viewer.clientWidth - 24);
    this.lastWidth = this.viewer.clientWidth;
    const maxScroll = Math.max(0, this.viewer.scrollHeight - this.viewer.clientHeight);
    const prevRatio = maxScroll > 0 ? this.viewer.scrollTop / maxScroll : 0;

    if (this.docTask) {
      try { await this.docTask.destroy(); } catch { /* ignore */ }
      this.docTask = null;
    }

    let doc = null;
    try {
      const [pdfjs, res] = await Promise.all([loadPdfJs(), fetch(url, { credentials: "same-origin" })]);
      if (seq !== this.renderSeq) return;
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const buf = await res.arrayBuffer();
      if (seq !== this.renderSeq) return;
      const task = pdfjs.getDocument({ data: new Uint8Array(buf), verbosity: pdfjs.VerbosityLevel?.ERRORS ?? 0 });
      this.docTask = task;
      doc = await task.promise;
      if (seq !== this.renderSeq) {
        await doc.destroy();
        return;
      }

      const dpr = Math.min(window.devicePixelRatio || 1, 2.5);
      const frag = document.createDocumentFragment();
      for (let i = 1; i <= doc.numPages; i++) {
        if (seq !== this.renderSeq) return;
        const page = await doc.getPage(i);
        const base = page.getViewport({ scale: 1 });
        const cssScale = availWidth / base.width;
        const viewport = page.getViewport({ scale: cssScale });
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.floor(viewport.width * dpr));
        canvas.height = Math.max(1, Math.floor(viewport.height * dpr));
        canvas.style.width = `${Math.floor(viewport.width)}px`;
        canvas.style.height = `${Math.floor(viewport.height)}px`;
        canvas.setAttribute("aria-label", `Page ${i} of ${doc.numPages}`);
        const ctx = canvas.getContext("2d", { alpha: false });
        const transform = dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : null;
        await page.render({ canvasContext: ctx, viewport, transform }).promise;
        frag.append(canvas);
      }
      if (seq !== this.renderSeq) return;
      this.pages.replaceChildren(frag);
      const newMax = Math.max(0, this.viewer.scrollHeight - this.viewer.clientHeight);
      this.viewer.scrollTop = prevRatio * newMax;
      this.onLoad?.();
    } catch {
      if (seq !== this.renderSeq) return;
      this.onError?.(url);
    } finally {
      if (doc && this.docTask === null) {
        try { await doc.destroy(); } catch { /* ignore */ }
      }
    }
  }

  setVisible(visible) {
    this.visible = visible;
    if (this.frame) this.frame.hidden = !visible;
    if (this.viewer) {
      this.viewer.hidden = !visible;
      if (visible && this.pendingRender && this.url) this.renderPdf();
    }
  }
}

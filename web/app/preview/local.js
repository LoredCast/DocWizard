// PDFs made in this browser (ARCHITECTURE.md §6.4): the render kit comes from the server (and is
// kept in IndexedDB for offline use), and workers/render.js turns it and the text being typed into
// a PDF with pandoc and Typst compiled to WebAssembly.
import { getKit, putKit } from "../store/idb.js";

const KIT_AGE = 30_000; // how often an open editor asks whether the kit changed

export class LocalRenderer {
  constructor({ projectId, docId, offline = false }) {
    this.projectId = projectId;
    this.docId = docId;
    this.offline = offline;
    this.key = `${projectId}:${docId}`;
    this.kit = null;
    this.etag = null;
    this.checked = 0;
    this.worker = null;
    this.next = 0;
    this.pending = new Map();
  }

  start() {
    if (this.worker) return;
    this.worker = new Worker(new URL("../workers/render.js", import.meta.url), { type: "module" });
    this.worker.onmessage = (event) => {
      const { id, pdf, error, superseded, ms } = event.data ?? {};
      const p = this.pending.get(id);
      if (!p) return;
      this.pending.delete(id);
      if (superseded) p.resolve(null);
      else if (error) p.reject(new Error(error));
      else p.resolve({ pdf, ms });
    };
    this.worker.onerror = (event) => {
      for (const p of this.pending.values()) p.reject(new Error(event.message || "render worker failed"));
      this.pending.clear();
    };
    this.worker.postMessage({ type: "warm" });
  }

  // The kit: asked for again when older than KIT_AGE (an unchanged one answers 304), and the
  // kept copy when the server can't be reached.
  async loadKit({ fresh = false } = {}) {
    if (!this.kit) {
      const kept = await getKit(this.key);
      if (kept) ({ kit: this.kit, etag: this.etag } = kept);
    }
    if (this.offline || (!fresh && this.kit && Date.now() - this.checked < KIT_AGE)) {
      if (!this.kit) throw new Error("not available offline");
      return this.kit;
    }
    try {
      const res = await fetch(`/api/projects/${this.projectId}/render-kit?doc=${encodeURIComponent(this.docId)}`, {
        credentials: "same-origin", headers: this.etag && this.kit ? { "If-None-Match": this.etag } : {},
      });
      this.checked = Date.now();
      if (res.status === 200) {
        this.kit = await res.json();
        this.etag = res.headers.get("etag");
        putKit(this.key, this.etag, this.kit);
      } else if (res.status !== 304) {
        const body = await res.json().catch(() => null);
        const err = new Error(body?.error?.message ?? `HTTP ${res.status}`);
        err.status = res.status;
        if (!this.kit || res.status < 500) throw err;
      }
    } catch (err) {
      if (err.status || !this.kit) throw err;
      // Offline: the kept kit.
    }
    return this.kit;
  }

  // The PDF for *text*, or null when a newer render replaced this one.
  async render(text) {
    this.start();
    const kit = await this.loadKit();
    const id = ++this.next;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.worker.postMessage({ id, kit, text });
    });
  }

  stop() {
    this.worker?.terminate();
    this.worker = null;
    for (const p of this.pending.values()) p.resolve(null);
    this.pending.clear();
  }
}

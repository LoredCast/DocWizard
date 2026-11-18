// HTML preview in a sandboxed iframe on the preview origin (ARCHITECTURE.md §6.4).
// Navigation uses location.replace(), which works across origins and adds no history
// entries; a change of heading only changes the fragment, so the page doesn't reload.

const LOAD_TIMEOUT_MS = 10000;

export class HtmlPreview {
  constructor(iframe, { onError, onLoad } = {}) {
    this.iframe = iframe;
    this.onError = onError ?? null;
    this.onLoad = onLoad ?? null;
    this.url = null;
    this.dark = false;
    this.heading = "";
    this.current = "";
    this.loaded = false;
    this.timer = null;

    this.iframe.addEventListener("load", () => {
      if (!this.current) return;
      clearTimeout(this.timer);
      this.timer = null;
      this.loaded = true;
      this.onLoad?.();
    });

    this.iframe.addEventListener("error", () => {
      if (!this.current) return;
      this.fail();
    });

    document.addEventListener("securitypolicyviolation", (event) => {
      const directive = event.effectiveDirective || event.violatedDirective;
      if (directive === "frame-src" && this.current) this.fail();
    });
  }

  armTimeout() {
    clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      if (!this.loaded && this.current) this.fail();
    }, LOAD_TIMEOUT_MS);
  }

  fail() {
    clearTimeout(this.timer);
    this.timer = null;
    this.loaded = false;
    this.onError?.(this.address());
  }

  address() {
    if (!this.url) return "";
    const u = new URL(this.url, location.href);
    if (this.dark) u.searchParams.set("paper", "dark");
    u.hash = this.heading ? encodeURIComponent(this.heading) : "";
    return u.href;
  }

  navigate() {
    const next = this.address();
    if (!next || next === this.current) return;
    const prevBase = this.current ? this.current.split("#", 1)[0] : "";
    const nextBase = next.split("#", 1)[0];
    const hashOnly = prevBase && prevBase === nextBase && this.loaded;
    this.current = next;
    if (!hashOnly) {
      this.loaded = false;
      this.armTimeout();
    }
    if (!prevBase) {
      this.iframe.src = next;
      return;
    }
    try {
      this.iframe.contentWindow.location.replace(next);
    } catch {
      this.iframe.src = next;
    }
  }

  show(url) {
    this.url = url;
    this.navigate();
  }

  reload() {
    this.current = "";
    this.loaded = false;
    this.navigate();
  }

  setDark(dark) {
    this.dark = dark;
    this.navigate();
  }

  scrollTo(headingId) {
    if (headingId === this.heading) return;
    this.heading = headingId;
    this.navigate();
  }
}

// Pandoc's auto_identifiers: lowercase, drop footnotes and formatting, keep letters, digits,
// "_", "-", "."; spaces become hyphens; strip everything up to the first letter.
export function headingId(title) {
  const explicit = /\{[^}]*#([^\s}]+)[^}]*\}\s*$/.exec(title);
  if (explicit) return explicit[1];
  const id = title
    .replace(/\^\[[^\]]*\]|\[\^[^\]]*\]/g, "")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/[*_`~]/g, "")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}_\-.\s]/gu, "")
    .trim()
    .replace(/\s+/g, "-")
    .replace(/^[^\p{L}]+/u, "");
  return id || "section";
}

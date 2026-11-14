// HTML preview in a sandboxed iframe on the preview origin (ARCHITECTURE.md §6.4).
// Navigation uses location.replace(), which works across origins and adds no history
// entries; a change of heading only changes the fragment, so the page doesn't reload.

export class HtmlPreview {
  constructor(iframe) {
    this.iframe = iframe;
    this.url = null;
    this.dark = false;
    this.heading = "";
    this.current = "";
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
    this.current = next;
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

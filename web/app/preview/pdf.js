// PDF preview with the browser's built-in viewer (ARCHITECTURE.md §6.4).
// The viewer doesn't run in a sandboxed frame, so PDFs get their own frame, served
// from the app origin with a CSP that only allows this page to embed them.

export class PdfPreview {
  constructor(container) {
    this.container = container;
    this.frame = null;
    this.url = null;
    this.page = 1;
  }

  show(url) {
    if (!url || url === this.url) return;
    this.url = url;
    // A new frame per render: the viewer can't be told to reload in place.
    const frame = document.createElement("iframe");
    frame.title = "PDF preview";
    frame.className = "pdf-frame";
    frame.src = `${url}#page=${this.page}`;
    if (this.frame) this.frame.replaceWith(frame);
    else this.container.append(frame);
    this.frame = frame;
  }

  setVisible(visible) {
    if (this.frame) this.frame.hidden = !visible;
  }
}

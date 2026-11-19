// "Check before submitting" (ARCHITECTURE.md §9.11): word and page limits, cross-references,
// citations, open comments and suggestions, and what a double-blind export removes.
import { $, api, el, t } from "../ui/dom.js";

const VERDICT = {
  ok: () => t("OK"),
  warn: () => t("Check"),
  fail: () => t("Fix"),
  info: () => t("Note"),
};

export class Preflight {
  constructor({ projectId, docId, docUrl, onAnonymize }) {
    this.projectId = projectId;
    this.docId = docId;
    this.docUrl = docUrl;
    this.dialog = $("#preflight-dialog");
    this.list = $("#preflight-list");
    this.state = $("#preflight-state");
    this.pagesBtn = $("#preflight-pages");
    if (!this.dialog) return;
    this.pagesBtn?.addEventListener("click", () => this.run(true));
    $("#preflight-again")?.addEventListener("click", () => this.run(this.counted));
    $("#preflight-anonymize")?.addEventListener("click", () => { this.dialog.close(); onAnonymize?.(); });
    this.counted = false;
  }

  open() {
    if (!this.dialog) return;
    this.dialog.showModal();
    this.run(false);
  }

  async run(pages) {
    this.counted = pages;
    this.state.textContent = pages ? t("Making the PDF to count its pages…") : t("Checking…");
    this.pagesBtn.disabled = true;
    let report;
    try {
      report = await api("GET", `/api/projects/${this.projectId}/preflight?doc=${encodeURIComponent(this.docId)}${pages ? "&pages=1" : ""}`);
    } catch (err) {
      this.state.textContent = err.message;
      this.pagesBtn.disabled = false;
      return;
    }
    this.pagesBtn.disabled = false;
    this.pagesBtn.hidden = report.pages !== null && report.pages !== undefined;
    const problems = report.checks.filter((c) => c.status === "fail" || c.status === "warn").length;
    this.state.textContent = (report.pdfError ? `${report.pdfError} ` : "")
      + (problems ? (problems === 1 ? t("1 thing to look at.") : t("{n} things to look at.", { n: problems }))
        : t("Everything checked is fine."));
    this.list.replaceChildren(...report.checks.map((c) => el("li", { class: `check-${c.id}` },
      el("div", { class: "check-head" },
        el("strong", {}, c.title),
        el("span", { class: "spacer muted" }, c.summary),
        el("span", { class: `verdict ${c.status}` }, VERDICT[c.status]?.() ?? c.status)),
      c.items.length ? el("ul", {}, ...c.items.map((i) => el("li", {},
        i.path ? el("a", { href: this.docUrl(i.path) + (i.line ? `#L${i.line}` : "") },
          i.line ? `${i.path}:${i.line}` : i.path) : null,
        i.path ? " " : null, i.text)),
        c.more ? el("li", { class: "muted" }, t("and {n} more", { n: c.more })) : null) : null)));
  }
}

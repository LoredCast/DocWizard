// Find the page of a citation in its PDF (ARCHITECTURE.md §10.4): open an attachment, search
// its text for the quoted passage, and cite the printed page that holds it.
import { loadPdfJs } from "../preview/pdf.js";
import { el, t } from "../ui/dom.js";
import { findInPages, formatLocator, normalizeText, pageLabel, pageOffset } from "./cite-tools.js";

const stored = (key) => {
  try { return localStorage.getItem(key); } catch { return null; }
};
const store = (key, value) => {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch { /* private window */ }
};

function highlightItem(norm, q) {
  if (!norm || !q) return false;
  if (q.length >= 3 && norm.includes(q)) return true;
  if (norm.length >= 6 && q.includes(norm)) return true;
  return norm.length >= 16 && (q.includes(norm.slice(0, 12)) || q.includes(norm.slice(-12)));
}

// Opens a dialog over the editor. *onCite(locator)* puts the locator into the document;
// without it (a read-only editor) the dialog only shows pages.
export async function openPdfFinder({ url, title, item, attachment, quote = "", lang = "", onCite = null }) {
  const offsetKey = `dw.pageOffset.${attachment?.key ?? url}`;
  const search = el("input", { type: "search", class: "grow", placeholder: t("Search the PDF for a passage or a word"),
                               "aria-label": t("Search the PDF"), value: quote });
  const status = el("span", { class: "muted", role: "status" });
  const hitsList = el("ol", { class: "plain pdf-hits" });
  const canvas = el("canvas", { class: "pdf-page-canvas" });
  const marks = el("div", { class: "pdf-marks", "aria-hidden": "true" });
  const pageBox = el("div", { class: "pdf-page" }, canvas, marks);
  const stage = el("div", { class: "pdf-stage" }, pageBox);
  const pageInput = el("input", { type: "number", min: 1, value: 1, class: "pdf-page-input", "aria-label": t("PDF page") });
  const pageCount = el("span", { class: "muted" });
  const printedInput = el("input", { class: "pdf-printed", "aria-label": t("Printed page number of this page"),
                                     title: t("The page number printed on this page. Change it if it's wrong; the other pages follow.") });
  const toInput = el("input", { class: "pdf-printed", placeholder: t("to"), "aria-label": t("Last page of the passage") });
  const locatorPreview = el("code", { class: "pdf-locator" });
  const citeButton = el("button", { type: "button", class: "primary" }, t("Cite this page"));
  const prev = el("button", { type: "button", "aria-label": t("Previous page") }, "‹");
  const next = el("button", { type: "button", "aria-label": t("Next page") }, "›");
  const openLinks = el("span", { class: "row pdf-links" },
    el("a", { href: url, target: "_blank", rel: "noopener" }, t("Open in a new tab")),
    attachment?.desktop ? el("a", { href: attachment.desktop, class: "pdf-desktop" }, t("Open in Zotero")) : null);

  const dialog = el("dialog", { class: "pdf-finder", "aria-label": t("Find the page in {title}", { title }) },
    el("div", { class: "pdf-finder-head" },
      el("strong", { class: "grow ellipsis" }, title),
      openLinks,
      el("button", { type: "button", "data-close-dialog": true }, t("Close"))),
    el("form", { class: "row pdf-search" }, search, el("button", { type: "submit" }, t("Find")), status),
    el("div", { class: "pdf-finder-body" },
      el("div", { class: "pdf-hits-wrap" }, hitsList),
      stage),
    el("div", { class: "pdf-finder-foot" },
      el("span", { class: "row" }, prev, el("label", { class: "row" }, t("PDF page"), pageInput), pageCount, next),
      el("span", { class: "row" }, el("label", { class: "row" }, t("printed page"), printedInput), "–", toInput),
      el("span", { class: "grow" }),
      onCite ? el("span", { class: "row" }, locatorPreview, citeButton) : null));
  document.body.append(dialog);
  dialog.addEventListener("close", () => {
    cancelled = true;
    doc?.destroy().catch(() => {});
    dialog.remove();
  });
  dialog.showModal();
  search.focus();

  let cancelled = false;
  let doc = null;
  let labels = null;
  let offset = null;
  let current = 1;
  let texts = [];        // normalized text per page
  let items = [];        // per page: [{str, norm, transform, width}]
  let hits = [];
  let query = "";
  let renderTask = null;

  status.textContent = t("Opening the PDF…");
  try {
    const [pdfjs, res] = await Promise.all([loadPdfJs(), fetch(url, { credentials: "same-origin" })]);
    if (!res.ok) {
      let message = t("The PDF could not be opened ({status}).", { status: res.status });
      try { message = (await res.json()).error?.message ?? message; } catch { /* not JSON */ }
      throw new Error(message);
    }
    const data = new Uint8Array(await res.arrayBuffer());
    if (cancelled) return;
    doc = await pdfjs.getDocument({ data, verbosity: pdfjs.VerbosityLevel?.ERRORS ?? 0 }).promise;
    labels = await doc.getPageLabels().catch(() => null);
    const saved = stored(offsetKey);
    offset = saved !== null ? Number(saved) : pageOffset(labels, doc.numPages, item?.page);
    pageInput.max = String(doc.numPages);
    pageCount.textContent = t("of {n}", { n: doc.numPages });
    await show(1);
    await readText(pdfjs);
  } catch (err) {
    status.textContent = "";
    stage.replaceChildren(el("p", { class: "error pad" }, err.message || t("The PDF could not be opened.")));
    return;
  }

  async function readText() {
    texts = new Array(doc.numPages).fill("");
    items = new Array(doc.numPages).fill(null);
    let chars = 0;
    for (let i = 1; i <= doc.numPages && !cancelled; i++) {
      if (i % 10 === 1 && doc.numPages > 10) status.textContent = t("Reading page {i} of {n}…", { i, n: doc.numPages });
      const page = await doc.getPage(i);
      const content = await page.getTextContent();
      const parts = [];
      items[i - 1] = content.items.filter((x) => typeof x.str === "string").map((x) => {
        parts.push(x.str + (x.hasEOL ? "\n" : ""));
        return { str: x.str, norm: normalizeText(x.str), transform: x.transform, width: x.width };
      });
      texts[i - 1] = normalizeText(parts.join(" "));
      chars += texts[i - 1].length;
      page.cleanup();
    }
    if (cancelled) return;
    if (chars < 20 * doc.numPages && chars < 200) {
      status.textContent = t("This PDF has no text to search (it may be a scan). Page through it instead.");
      return;
    }
    status.textContent = "";
    if (search.value.trim()) find();
  }

  function find() {
    query = normalizeText(search.value);
    hits = findInPages(texts, search.value);
    hitsList.replaceChildren(...hits.map((h, n) => el("li", {},
      el("button", { type: "button", class: "pdf-hit", onclick: () => { show(h.page); select(n); } },
        el("span", { class: "pdf-hit-page" }, t("p. {page}", { page: pageLabel(h.page, labels, offset) })),
        el("span", { class: "muted" }, `…${h.snippet}…`)))));
    if (!query) status.textContent = "";
    else if (!hits.length) status.textContent = t("Not found. Try fewer or other words.");
    else {
      const partial = hits[0].partial ? ` ${t("(only part of the passage matched)")}` : "";
      status.textContent = (hits.length === 1 ? t("1 match") : t("{n} matches", { n: hits.length })) + partial;
      show(hits[0].page);
      select(0);
    }
  }

  function select(n) {
    hitsList.querySelectorAll(".pdf-hit").forEach((b, i) => b.classList.toggle("active", i === n));
  }

  async function show(index) {
    if (!doc) return;
    current = Math.max(1, Math.min(doc.numPages, index));
    pageInput.value = String(current);
    printedInput.value = pageLabel(current, labels, offset);
    toInput.value = "";
    updateLocator();
    const page = await doc.getPage(current);
    if (cancelled) return;
    const width = Math.max(240, stage.clientWidth - 24);
    const base = page.getViewport({ scale: 1 });
    const viewport = page.getViewport({ scale: Math.min(2.5, width / base.width) });
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(viewport.width * dpr);
    canvas.height = Math.floor(viewport.height * dpr);
    canvas.style.width = `${Math.floor(viewport.width)}px`;
    canvas.style.height = `${Math.floor(viewport.height)}px`;
    pageBox.style.width = canvas.style.width;
    pageBox.style.height = canvas.style.height;
    renderTask?.cancel();
    renderTask = page.render({ canvasContext: canvas.getContext("2d", { alpha: false }), viewport,
                               transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : null });
    try { await renderTask.promise; } catch { return; }
    const pdfjs = await loadPdfJs();
    const pageItems = items[current - 1] ?? [];
    const onHitPage = hits.some((h) => h.page === current || h.page === current - 1);
    marks.replaceChildren(...(onHitPage ? pageItems : []).filter((x) => highlightItem(x.norm, query)).map((x) => {
      const tx = pdfjs.Util.transform(viewport.transform, x.transform);
      const h = Math.hypot(tx[2], tx[3]);
      return el("span", { class: "pdf-mark",
        style: `left:${tx[4]}px;top:${tx[5] - h}px;width:${x.width * viewport.scale}px;height:${h * 1.15}px` });
    }));
    stage.scrollTop = 0;
    const first = marks.firstElementChild;
    if (first) stage.scrollTop = Math.max(0, parseFloat(first.style.top) - 80);
  }

  function updateLocator() {
    const from = printedInput.value.trim() || String(current);
    const to = toInput.value.trim();
    locatorPreview.textContent = formatLocator(lang, from, to);
  }

  dialog.querySelector(".pdf-search").addEventListener("submit", (event) => {
    event.preventDefault();
    find();
  });
  prev.addEventListener("click", () => show(current - 1));
  next.addEventListener("click", () => show(current + 1));
  pageInput.addEventListener("change", () => show(Number(pageInput.value) || 1));
  printedInput.addEventListener("change", () => {
    const n = Number(printedInput.value.trim());
    if (Number.isInteger(n)) {
      // Numbers count on from here: remember how this PDF's pages map to printed ones.
      offset = n - current;
      store(offsetKey, String(offset));
      hitsList.querySelectorAll(".pdf-hit-page").forEach((node, i) => {
        node.textContent = t("p. {page}", { page: pageLabel(hits[i].page, labels, offset) });
      });
    }
    updateLocator();
  });
  printedInput.addEventListener("input", updateLocator);
  toInput.addEventListener("input", updateLocator);
  dialog.addEventListener("keydown", (event) => {
    if (event.target.closest("input")) return;
    if (event.key === "ArrowRight" || event.key === "PageDown") { event.preventDefault(); show(current + 1); }
    if (event.key === "ArrowLeft" || event.key === "PageUp") { event.preventDefault(); show(current - 1); }
  });
  citeButton.addEventListener("click", () => {
    onCite?.(locatorPreview.textContent);
    dialog.close();
  });
}

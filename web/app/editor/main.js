// The editor page: wires the editor, collaboration, preview, export, layout options and versions.
import { EditorSelection, EditorState, EditorView, redo, undo } from "codemirror";
import { DocSession } from "../collab/peer.js";
import { PresenceSender, PresenceView, peersField } from "../collab/presence.js";
import { Socket } from "../collab/socket.js";
import { HtmlPreview, headingId } from "../preview/html.js";
import { PdfPreview } from "../preview/pdf.js";
import { getCitations, getDoc, getDocByUrl, prune, putCitations, putLayout } from "../store/idb.js";
import { $, api, confirmDialog, el, t, toast, wireCommon } from "../ui/dom.js";
import { AnchorLoader, anchorsField, conflictGutter } from "./anchors.js";
import { setCiteHooks, setLibrary } from "./citations.js";
import {
  activeFormatting, cycleHeading, insertCitation, insertFootnote, insertLink,
  toggleBold, toggleBulletList, toggleCode, toggleInlineMath, toggleItalic,
  toggleOrderedList, toggleQuote, toggleStrike, toggleTaskList,
} from "./commands.js";
import { CommentsPanel } from "./comments.js";
import { FieldsPanel, LayoutFields, fieldsExtension } from "./fields.js";
import { ReferencesPanel } from "./references.js";
import { baseExtensions } from "./setup.js";

wireCommon();

// Online, the page embeds a snapshot. Offline, the service worker serves the editor
// shell without one, and the document opens from this browser's copy (§6.5).
let snap = JSON.parse($("#snapshot").textContent);
const offlineRecord = snap ? null : await getDocByUrl(location.pathname);
if (!snap) {
  if (!offlineRecord?.meta) {
    $(".mode-switch")?.setAttribute("hidden", "");
    $("#split").replaceChildren(el("section", { class: "pad" },
      el("h2", {}, t("Not available offline")),
      el("p", {}, t("This document hasn't been opened in this browser recently. Open it again when you're online.")),
      el("p", {}, el("a", { href: "/" }, t("Documents available offline")))));
    await new Promise(() => {}); // nothing else on this page can work
  }
  snap = { ...offlineRecord.meta, text: offlineRecord.baseText, version: offlineRecord.syncedVersion,
           clientID: offlineRecord.clientID };
  document.title = `${snap.doc.path} · ${snap.project.name} · DocWizard`;
  $("#crumb-project").textContent = snap.project.name;
  $("#crumb-project").href = `/p/${snap.project.id}`;
  $("#crumb-doc").textContent = snap.doc.path;
  $("#doc-kind").textContent = snap.doc.kind === "markdown" ? "Markdown"
    : snap.doc.kind.charAt(0).toUpperCase() + snap.doc.kind.slice(1);
  for (const node of document.querySelectorAll("[data-layout-name]")) node.textContent = snap.layout?.name ?? "";
}
const docId = snap.doc.id;
const projectId = snap.project.id;
const kind = snap.doc.kind;
const readOnly = !snap.can.edit;
const isMarkdown = kind === "markdown";

const split = $("#split");

// -- Preferences ---------------------------------------------------------
const root = document.documentElement;
root.style.setProperty("--editor-font", snap.prefs.font === "mono" ? "var(--mono)" : "var(--font)");
root.style.setProperty("--editor-size", `${snap.prefs.size}px`);

const stored = (key, fallback) => {
  try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
};
const store = (key, value) => {
  try { localStorage.setItem(key, value); } catch { /* private window */ }
};

// -- Editor and collaboration ---------------------------------------------
const socket = new Socket();
const base = baseExtensions({ kind, readOnly });
let library = new Map();
let references = null; // the references panel, created below
let fieldsPanel = null; // the fields panel, created below
// Clicking commented text opens its thread.
const markClicks = EditorView.domEventHandlers({
  click(event) {
    const mark = event.target.closest?.("[data-thread]");
    if (mark) comments.focusThread(Number(mark.dataset.thread));
    return false;
  },
});
let layoutOptions = { ...snap.layoutOptions };
// The front matter fields the layout prints (§9.4): autocomplete and hover help in the
// front matter, and the Fields panel below.
const layoutFields = isMarkdown ? new LayoutFields(snap.fields, {
  layoutName: snap.layout?.name ?? "",
  titlePage: () => layoutOptions.titlepage ?? snap.fields?.titlePage ?? false,
}) : null;

const session = new DocSession({
  socket, docId, clientID: snap.clientID, text: snap.text, version: snap.version,
  makeState: (text, version) => EditorState.create({ doc: text, extensions: [
    base, peersField, anchorsField, conflictGutter((id) => openConflicts(id)), markClicks,
    layoutFields ? fieldsExtension(layoutFields) : [], session.extension(version)] }),
  onStatus: showStatus,
  onEvent: handleEvent,
  onUpdates: () => { presence.flush(); comments.flush(); conflictLoader.flush(); },
});
const presenceSender = new PresenceSender(session, socket, docId);
const presence = new PresenceView(session, showPeers);

const view = new EditorView({
  state: session.makeState(snap.text, snap.version),
  parent: $("#editor"),
  dispatchTransactions(trs, v) {
    v.update(trs);
    if (trs.some((tr) => tr.docChanged)) docChanged();
    if (trs.some((tr) => tr.selection || tr.docChanged)) {
      presenceSender.schedule();
      updateFormatBar();
    }
    if (trs.some((tr) => tr.selection)) cursorMoved();
    if (trs.some((tr) => tr.selection || tr.docChanged)) references?.cursorMoved();
    if (trs.some((tr) => tr.docChanged)) fieldsPanel?.docChanged();
  },
});
const { text: _text, version: _version, clientID: _clientID, ...meta } = snap;
session.keep = { url: location.pathname, meta, pinned: Boolean(snap.doc.pinned) };
session.start(view);
view.focus();

// -- Formatting bar and mobile actions menu -------------------------------
const formatBar = $("#format-bar");
const FORMAT_CMDS = {
  undo, redo,
  bold: toggleBold, italic: toggleItalic, strike: toggleStrike, code: toggleCode,
  heading: cycleHeading, quote: toggleQuote, bullet: toggleBulletList,
  ordered: toggleOrderedList, task: toggleTaskList,
  link: insertLink, math: toggleInlineMath, cite: insertCitation, footnote: insertFootnote,
  refs: () => references?.toggle(),
};
if (formatBar) {
  formatBar.hidden = readOnly || !isMarkdown;
  formatBar.addEventListener("mousedown", (event) => {
    if (event.target.closest("[data-cmd]")) event.preventDefault();
  });
  formatBar.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-cmd]");
    if (!btn || readOnly || !isMarkdown) return;
    const cmd = FORMAT_CMDS[btn.dataset.cmd];
    if (cmd) {
      cmd(view);
      view.focus();
      updateFormatBar();
    }
  });
}

function updateFormatBar() {
  if (!formatBar || formatBar.hidden) return;
  const active = activeFormatting(view.state);
  for (const btn of formatBar.querySelectorAll("[data-cmd]")) {
    const cmd = btn.dataset.cmd;
    if (cmd in active) {
      const on = Boolean(active[cmd]);
      if (btn.hasAttribute("aria-pressed")) btn.setAttribute("aria-pressed", String(on));
      if (cmd === "heading") btn.textContent = active.heading ? `H${active.heading}` : "H";
    }
  }
}
updateFormatBar();

const editorMore = $("#editor-more");
const moreBtn = $("#more-actions");
if (editorMore && moreBtn) {
  const closeMore = () => {
    editorMore.classList.remove("open");
    moreBtn.setAttribute("aria-expanded", "false");
  };
  moreBtn.addEventListener("click", (event) => {
    event.stopPropagation();
    const open = editorMore.classList.toggle("open");
    moreBtn.setAttribute("aria-expanded", String(open));
  });
  $("#toolbar-buttons")?.addEventListener("click", (event) => {
    if (event.target.closest("button")) closeMore();
  });
  document.addEventListener("click", (event) => {
    if (editorMore.classList.contains("open") && !editorMore.contains(event.target)) closeMore();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeMore();
  });
}

if (!offlineRecord) {
  getDoc(docId).then((record) => {
    if (record && record.clientID !== undefined && session.restore(record)) {
      toast("Unsaved edits from this browser are being merged in.");
      session.reportStatus();
    }
    session.persistSoon();
    prune();
  });
  if (snap.layout) putLayout(snap.layout);
}

function showStatus({ state, waiting }) {
  // The offline editor has no CSRF token or server data: once everything it held is
  // on the server, the real page takes over.
  if (offlineRecord && state === "saved") {
    location.reload();
    return;
  }
  const label = {
    saved: t("Saved"),
    saving: t("Saving…"),
    reconnecting: t("Reconnecting…"),
    offline: waiting ? (waiting === 1 ? t("Offline · 1 change waiting") : t("Offline · {n} changes waiting", { n: waiting })) : t("Offline"),
  }[state];
  $("#save-state").textContent = label;
  const menuStatus = $("#menu-save-state");
  if (menuStatus) menuStatus.textContent = label;
}

function showPeers(peers) {
  const slot = $("#presence");
  slot.replaceChildren(...peers.map((p, i) => el("span", { class: "peer" },
    el("span", { class: "dot", style: `--peer: var(--peer-${p.color})`, "aria-hidden": "true" }),
    el("span", { class: "name" }, `${p.name}${i < peers.length - 1 ? "," : ""}`))));
  slot.title = peers.length ? t("Also here: {names}", { names: peers.map((p) => p.name).join(", ") }) : "";
}

socket.onError((msg) => toast(msg.message ?? t("The connection reported an error."), { error: true }));

function handleEvent(msg) {
  switch (msg.t) {
    case "opened":
      presence.replaceAll(msg.version, msg.peers ?? []);
      if (location.hash.startsWith("#comment-")) openLinkedThread();
      else comments.load();
      loadConflicts();
      presenceSender.reset();
      presenceSender.send(true);
      if (msg.role && msg.role !== snap.role && ["viewer", "commenter"].includes(msg.role) && !readOnly) {
        toast("Your access changed to read-only. Reload the page.", { error: true });
      }
      requestPreview(0);
      break;
    case "reset":
      presence.restore();
      if (library.size) view.dispatch({ effects: setLibrary.of(library) });
      docChanged();
      break;
    case "merged":
      if (msg.conflicts) {
        toast(msg.conflicts === 1 ? t("Offline edits were merged. 1 passage needs a look.") : t("Offline edits were merged. {n} passages need a look.", { n: msg.conflicts }));
      }
      loadConflicts();
      break;
    case "conflicts":
      loadConflicts();
      break;
    case "comments":
      comments.load();
      break;
    case "notifications":
      if (msg.users?.includes(snap.user?.id)) comments.load();
      break;
    case "rendered":
      showRendered(msg);
      break;
    case "presence":
      presence.receive(msg.version, msg.peer);
      break;
    case "error":
      toast(msg.message ?? t("Something went wrong."), { error: true });
      break;
    default:
  }
}

// -- Word count and outline (worker) -----------------------------------------
let outline = [];
let statsTimer = null;
let statsId = 0;
const stats = new Worker(new URL("../workers/stats.js", import.meta.url), { type: "module" });
stats.onmessage = (event) => {
  if (event.data.id !== statsId) return;
  outline = event.data.outline;
  const locale = document.documentElement.lang || undefined;
  const n = event.data.words.toLocaleString(locale);
  const notes = event.data.notes && isMarkdown ? t(" · {n} in notes", { n: event.data.notes.toLocaleString(locale) }) : "";
  $("#word-count").textContent = t("{n} words", { n }) + notes;
};
function countWords() {
  statsId += 1;
  stats.postMessage({ id: statsId, text: view.state.doc.toString() });
}
countWords();

function docChanged() {
  clearTimeout(statsTimer);
  statsTimer = setTimeout(countWords, 400);
  requestPreview();
}

// -- Citations ---------------------------------------------------------
let citationsEtag = "";
async function loadCitations() {
  if (!isMarkdown) return;
  if (!citationsEtag) {
    const cached = await getCitations(projectId);
    if (cached) {
      citationsEtag = cached.etag;
      library = new Map(cached.items.map((item) => [item.id, item]));
      view.dispatch({ effects: setLibrary.of(library) });
    }
  }
  try {
    const res = await fetch(`/api/projects/${projectId}/citations`, {
      credentials: "same-origin", headers: citationsEtag ? { "If-None-Match": citationsEtag } : {},
    });
    if (res.status === 304 || !res.ok) return;
    citationsEtag = res.headers.get("etag") ?? "";
    const data = await res.json();
    putCitations(projectId, citationsEtag, data.items);
    library = new Map(data.items.map((item) => [item.id, item]));
    view.dispatch({ effects: setLibrary.of(library) });
    references?.libraryChanged();
  } catch {
    /* offline: keep the current library */
  }
}
loadCitations();

// Citing from a library the document didn't use yet: once the front matter reaches the
// server, the project's citation list has the new items (and their chips their labels).
let citeReload = [];
function reloadCitationsSoon() {
  citeReload.forEach(clearTimeout);
  citeReload = [1500, 5000].map((ms) => setTimeout(loadCitations, ms));
}
const canBrowse = !snap.user?.guest && !offlineRecord;
setCiteHooks({
  remoteSearch: canBrowse ? async (q) => (await api("GET", `/api/citations/search?q=${encodeURIComponent(q)}`)).items : null,
  afterCite: reloadCitationsSoon,
});
// Bibliography files edited elsewhere show up on focus.
window.addEventListener("focus", loadCitations);

// -- Preview -------------------------------------------------------------
const pane = $("#preview-pane");
const frame = $("#preview");
const previewError = $("#preview-error");
const narrow = window.matchMedia("(max-width: 900px)");
// LaTeX documents preview the project's root as a PDF built with LaTeX; Markdown uses the
// layout's PDF engine (§9.5).
const isLatex = kind === "latex";
const pdfTarget = isLatex ? "pdf-latex" : (snap.layout.defaultPdf === "pdf-latex" ? "pdf-latex" : "pdf-typst");
let target = isLatex || stored("dw.previewTarget", "html") === "pdf" ? pdfTarget : "html";
let previewOn = stored("dw.preview", "on") === "on";
let previewTimer = null;
let shown = { html: -1, "pdf-typst": -1, "pdf-latex": -1 };
const previewFailed = { html: false, pdf: false };

function showPreviewError(which, url) {
  previewFailed[which] = true;
  if ((which === "html") !== (target === "html")) return;
  const isHtml = which === "html";
  previewError.replaceChildren(
    el("p", { class: "error" }, isHtml ? t("HTML preview could not be loaded.") : t("PDF preview could not be loaded.")),
    el("div", { class: "row" },
      el("button", { type: "button", onclick: () => {
        clearPreviewError(which);
        if (isHtml) html.reload();
        else pdf.reload();
      } }, t("Retry")),
      url ? el("a", { class: "button", href: url, target: "_blank", rel: "noopener noreferrer" },
        isHtml ? t("Open preview") : t("Open PDF")) : null));
  previewError.hidden = false;
  frame.hidden = true;
  pdf.setVisible(false);
}

function clearPreviewError(which) {
  previewFailed[which] = false;
  if ((which === "html") !== (target === "html")) return;
  previewError.hidden = true;
  previewError.replaceChildren();
  frame.hidden = target !== "html";
  pdf.setVisible(target !== "html");
}

const html = new HtmlPreview(frame, {
  onLoad: () => clearPreviewError("html"),
  onError: (url) => showPreviewError("html", url),
});
const pdf = new PdfPreview(pane, {
  onLoad: () => clearPreviewError("pdf"),
  onError: (url) => showPreviewError("pdf", url),
});
if (isLatex) {
  for (const radio of document.querySelectorAll('input[name="preview-target"]')) radio.closest("label").hidden = true;
}

$(`input[name="preview-target"][value="${target === "html" ? "html" : "pdf"}"]`).checked = true;
const darkPaper = $("#dark-paper");
darkPaper.checked = stored("dw.darkPaper", "off") === "on";
html.setDark(darkPaper.checked);
pane.classList.toggle("dark-paper", darkPaper.checked);

function previewVisible() {
  return narrow.matches ? split.classList.contains("show-preview") : previewOn;
}

function applyPreviewLayout() {
  const button = $("#toggle-preview");
  const modeEdit = $("#mode-edit");
  const modePreview = $("#mode-preview");
  const showingMobile = split.classList.contains("show-preview");
  if (narrow.matches) {
    split.classList.remove("no-preview");
    button.textContent = showingMobile ? t("Edit") : t("Preview");
    button.setAttribute("aria-pressed", String(showingMobile));
  } else {
    split.classList.remove("show-preview");
    split.classList.toggle("no-preview", !previewOn);
    button.textContent = t("Preview");
    button.setAttribute("aria-pressed", String(previewOn));
  }
  if (modeEdit && modePreview) {
    modeEdit.setAttribute("aria-selected", String(!showingMobile));
    modePreview.setAttribute("aria-selected", String(showingMobile));
  }
  const activeKey = target === "html" ? "html" : "pdf";
  const failed = previewFailed[activeKey];
  previewError.hidden = !failed;
  frame.hidden = target !== "html" || failed;
  pdf.setVisible(target !== "html" && !failed);
}
applyPreviewLayout();
narrow.addEventListener("change", () => {
  applyPreviewLayout();
  requestPreview(0);
  if (previewVisible()) cursorMoved();
});

function setMobilePreview(show) {
  split.classList.toggle("show-preview", show);
  applyPreviewLayout();
  if (show) {
    requestPreview(0);
    cursorMoved();
  } else {
    view.focus();
  }
}

$("#mode-edit")?.addEventListener("click", () => setMobilePreview(false));
$("#mode-preview")?.addEventListener("click", () => setMobilePreview(true));

$("#toggle-preview").addEventListener("click", () => {
  if (narrow.matches) {
    setMobilePreview(!split.classList.contains("show-preview"));
    return;
  }
  previewOn = !previewOn;
  store("dw.preview", previewOn ? "on" : "off");
  applyPreviewLayout();
  requestPreview(0);
  if (previewVisible()) cursorMoved();
});

for (const radio of document.querySelectorAll('input[name="preview-target"]')) {
  radio.addEventListener("change", () => {
    target = radio.value === "pdf" ? pdfTarget : "html";
    store("dw.previewTarget", radio.value);
    applyPreviewLayout();
    requestPreview(0);
  });
}

darkPaper.addEventListener("change", () => {
  store("dw.darkPaper", darkPaper.checked ? "on" : "off");
  pane.classList.toggle("dark-paper", darkPaper.checked);
  html.setDark(darkPaper.checked);
});

function requestPreview(delay = target === "html" ? 300 : 1000) {
  clearTimeout(previewTimer);
  if (!previewVisible()) return;
  previewTimer = setTimeout(() => {
    const sent = socket.send({ t: "render", doc: docId, version: session.version(), target,
                               layout: snap.layout.id, options: layoutOptions });
    if (sent) $("#preview-state").textContent = t("Rendering…");
  }, delay);
}

function showRendered(msg) {
  const diag = $("#diagnostic");
  const errors = (msg.diagnostics ?? []).filter((d) => d.severity === "error");
  if (errors.length) {
    const d = errors[0];
    const elsewhere = d.file && !snap.doc.path.endsWith(d.file);
    diag.textContent = d.line ? (elsewhere ? t("{file}, line {line}: {message}", { file: d.file, line: d.line, message: d.message }) : t("Line {line}: {message}", { line: d.line, message: d.message })) : d.message;
    diag.className = "diagnostic-error";
    diag.title = errors.map((e) => e.message).join("\n");
  } else {
    diag.textContent = "";
    diag.className = "";
    diag.title = "";
  }
  if (msg.target !== target) return;
  if (!msg.url) {
    $("#preview-state").textContent = t("Preview failed");
    return;
  }
  if ((msg.version ?? 0) < shown[msg.target]) return;
  shown[msg.target] = msg.version ?? 0;
  $("#preview-state").textContent = t("Preview");
  if (msg.target === "html") {
    clearPreviewError("html");
    html.show(msg.url);
  } else {
    clearPreviewError("pdf");
    pdf.show(msg.url);
  }
}

// One-way scroll sync: the heading above the cursor (§6.4).
let lastHeading = null;
function cursorMoved() {
  if (!isMarkdown || target !== "html" || !outline.length || !previewVisible()) return;
  const line = view.state.doc.lineAt(view.state.selection.main.head).number;
  let current = null;
  for (const h of outline) {
    if (h.line > line) break;
    current = h;
  }
  const id = current ? headingId(current.title) : "";
  if (id === lastHeading) return;
  lastHeading = id;
  html.scrollTo(id);
}

// -- Export ---------------------------------------------------------------
const exportDialog = $("#export-dialog");
$("#open-export").addEventListener("click", () => {
  $("#export-status").textContent = "";
  exportDialog.showModal();
});
$("#export-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const status = $("#export-status");
  const button = event.submitter ?? $("#export-form button[type=submit]");
  const targetName = new FormData(event.target).get("target");
  status.textContent = t("Exporting…");
  button.disabled = true;
  try {
    const { job } = await api("POST", `/api/projects/${projectId}/export`,
                              { target: targetName, doc: docId, options: layoutOptions });
    for (let i = 0; i < 600; i++) {
      await new Promise((r) => setTimeout(r, i < 10 ? 500 : 1500));
      const s = await api("GET", `/api/jobs/${job}`);
      if (s.status === "queued" || s.status === "running") continue;
      status.replaceChildren();
      if (s.url) {
        status.append(el("a", { href: s.url, download: "" }, t("Download")), " ");
      } else {
        status.append("The export failed. ");
      }
      const message = s.diagnostics?.find((d) => d.severity === "error")?.message;
      if (message) status.append(el("span", { class: "error" }, message));
      return;
    }
    status.textContent = t("The export is taking too long.");
  } catch (err) {
    status.textContent = err.message;
  } finally {
    button.disabled = false;
  }
});

// -- Layout options -------------------------------------------------------
const optionsDialog = $("#options-dialog");
function optionField(name, spec, value) {
  const label = spec.label ?? name;
  const current = value ?? spec.default;
  if (spec.type === "boolean") {
    return el("label", { class: "choice" },
      el("input", { type: "checkbox", name, checked: Boolean(current) }), label);
  }
  if (spec.type === "enum") {
    return el("label", {}, label,
      el("select", { name }, spec.values.map((v) =>
        el("option", { value: v, selected: v === current }, spec.labels?.[v] ?? (v === "" ? t("None") : v)))));
  }
  if (spec.type === "number") {
    return el("label", {}, label,
      el("input", { type: "number", name, min: spec.min, max: spec.max, step: spec.step ?? "any",
                    value: current ?? "" }));
  }
  return el("label", {}, label, el("input", { type: "text", name, value: current ?? "", maxlength: 200 }));
}

$("#open-options").addEventListener("click", () => {
  const fields = $("#options-fields");
  const vars = snap.layout.variables ?? {};
  fields.replaceChildren(...Object.entries(vars).map(([name, spec]) => optionField(name, spec, layoutOptions[name])));
  if (!Object.keys(vars).length) fields.append(el("p", { class: "muted" }, t("This layout has no options.")));
  optionsDialog.showModal();
});

$("#options-form").addEventListener("submit", async (event) => {
  if (event.submitter?.value !== "save") return;
  event.preventDefault();
  const next = {};
  for (const [name, spec] of Object.entries(snap.layout.variables ?? {})) {
    const input = event.target.elements[name];
    if (!input) continue;
    if (spec.type === "boolean") next[name] = input.checked;
    else if (spec.type === "number") { if (input.value !== "") next[name] = Number(input.value); }
    else next[name] = input.value;
  }
  try {
    await api("PATCH", `/api/projects/${projectId}`, { layoutOptions: next });
    layoutOptions = next;
    optionsDialog.close();
    shown = { html: -1, "pdf-typst": -1, "pdf-latex": -1 };
    requestPreview(0);
  } catch (err) {
    toast(err.message, { error: true });
  }
});

// -- Versions and history (§8.5) ---------------------------------------------
const versionsDialog = $("#versions-dialog");
const fmt = new Intl.DateTimeFormat(document.documentElement.lang || undefined, { dateStyle: "medium", timeStyle: "short" });
const parseDate = (iso) => new Date(iso.endsWith("Z") ? iso : `${iso}Z`);
const versionsList = $("#versions-list");
const compareView = $("#versions-compare");

function versionRow(v, title, detail) {
  return el("li", { class: "row" },
    el("span", { class: "spacer" }, el("strong", {}, title), " ", el("span", { class: "muted" }, detail)),
    el("button", { type: "button", onclick: () => compareVersion(v, title) }, t("Compare")),
    readOnly ? null : el("button", { type: "button", onclick: () => restoreVersion(v, title) }, t("Restore")));
}

async function loadVersions() {
  compareView.hidden = true;
  versionsList.hidden = false;
  $("#version-form").hidden = readOnly;
  versionsList.replaceChildren(el("li", { class: "muted" }, t("Loading…")));
  try {
    const { versions, history = [] } = await api("GET", `/api/documents/${docId}/versions`);
    const items = [el("li", { class: "list-head" }, t("Named versions"))];
    items.push(...versions.map((v) => versionRow(v, v.label,
      `${fmt.format(parseDate(v.created_at))}${v.author ? ` · ${v.author}` : ""}`)));
    if (!versions.length) items.push(el("li", { class: "muted" }, t("No named versions yet.")));
    items.push(el("li", { class: "list-head" }, t("History")));
    items.push(...history.map((v, i) => versionRow(v, i === 0 ? t("Latest autosave") : fmt.format(parseDate(v.created_at)),
      v.authors.length ? t("Edited by {names}", { names: v.authors.join(", ") }) : "")));
    if (!history.length) items.push(el("li", { class: "muted" }, t("The history starts with the next edits.")));
    versionsList.replaceChildren(...items);
  } catch (err) {
    versionsList.replaceChildren(el("li", { class: "error" }, err.message));
  }
}

async function compareVersion(v, title) {
  versionsList.hidden = true;
  $("#version-form").hidden = true;
  compareView.hidden = false;
  const body = el("div", { class: "diff", role: "region", "aria-label": t("Changes") }, el("p", { class: "muted" }, t("Loading…")));
  compareView.replaceChildren(
    el("p", {}, t("Changes from “{title}” to the current text", { title })),
    el("p", { class: "muted", id: "diff-stats" }),
    body,
    el("div", { class: "buttons" },
      el("button", { type: "button", onclick: loadVersions }, t("Back")),
      readOnly ? null : el("button", { type: "button", onclick: () => restoreVersion(v, title) }, t("Restore this version"))));
  try {
    const diff = await api("GET", `/api/documents/${docId}/versions/${v.id}/compare`);
    $("#diff-stats").textContent = diff.added || diff.removed
      ? t("{added} words added, {removed} removed since then", { added: diff.added, removed: diff.removed })
      : t("No changes since then.");
    body.replaceChildren(...diff.segments.map((s) => {
      if (s.op === "skip") return el("span", { class: "diff-skip" }, s.lines === 1 ? t("… 1 unchanged line …") : t("… {n} unchanged lines …", { n: s.lines }));
      if (s.op === "ins") return el("ins", {}, s.text);
      if (s.op === "del") return el("del", {}, s.text);
      return el("span", {}, s.text);
    }));
  } catch (err) {
    body.replaceChildren(el("p", { class: "error" }, err.message));
  }
}

async function restoreVersion(v, title) {
  if (!await confirmDialog(t("Restore “{label}”? The current text is kept as a version first.", { label: title }), t("Restore"))) return;
  try {
    await api("POST", `/api/documents/${docId}/versions/${v.id}/restore`);
    toast(t("Restored “{label}”.", { label: title }));
    loadVersions();
  } catch (err) {
    toast(err.message, { error: true });
  }
}
$("#open-versions").addEventListener("click", () => {
  versionsDialog.showModal();
  loadVersions();
});
$("#version-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const label = event.target.elements.label.value.trim();
  if (!label) return;
  try {
    await api("POST", `/api/documents/${docId}/versions`, { label });
    event.target.reset();
    loadVersions();
  } catch (err) {
    toast(err.message, { error: true });
  }
});

// -- Comments ---------------------------------------------------------------
const sidePanel = $("#side-panel");
const comments = new CommentsPanel({
  session, docId, panel: sidePanel, list: $("#comments-list"),
  members: snap.members ?? [], isUser: !snap.user?.guest && !offlineRecord,
  onCount: (n, unread) => {
    const button = $("#open-comments");
    button.textContent = n ? t("Comments ({n})", { n }) : t("Comments");
    button.classList.toggle("has-unread", unread > 0);
    button.title = unread ? (unread === 1 ? t("1 thread with news for you") : t("{n} threads with news for you", { n: unread })) : "";
    if (unread && comments?.open) comments.markRead();
  },
});
// A link to a comment (from a notification) opens its thread.
async function openLinkedThread() {
  const m = /^#comment-(\d+)$/.exec(location.hash);
  if (!m) return;
  await comments.load();
  comments.focusThread(Number(m[1]));
}
sidePanel.addEventListener("toggle", () => {
  split.classList.toggle("with-panel", !sidePanel.hidden || Boolean(references?.open) || Boolean(fieldsPanel?.open));
  $("#open-comments").setAttribute("aria-pressed", String(!sidePanel.hidden));
});
$("#open-comments").addEventListener("click", () => {
  if (!comments.open && references?.open) references.hide();
  if (!comments.open && fieldsPanel?.open) fieldsPanel.hide();
  if (comments.open) comments.hide();
  else comments.show();
});
$("#close-panel").addEventListener("click", () => comments.hide());
$("#new-comment").hidden = !snap.can.comment;
$("#new-comment").addEventListener("click", () => comments.compose());

// -- References (§10.4) ------------------------------------------------------
const refsPanel = $("#refs-panel");
references = isMarkdown && refsPanel ? new ReferencesPanel({
  view, panel: refsPanel, readOnly, canBrowse,
  onToggle: (open) => {
    if (open && comments.open) comments.hide();
    if (open && fieldsPanel?.open) fieldsPanel.hide();
    split.classList.toggle("with-panel", open || comments.open || Boolean(fieldsPanel?.open));
    split.classList.toggle("refs-open", open);
    $("#open-refs")?.setAttribute("aria-pressed", String(open));
    formatBar?.querySelector('[data-cmd="refs"]')?.setAttribute("aria-pressed", String(open));
  },
  onCited: reloadCitationsSoon,
}) : null;
$("#open-refs")?.addEventListener("click", () => references?.toggle());

// -- Fields (§9.4) -------------------------------------------------------------
const fieldsEl = $("#fields-panel");
fieldsPanel = layoutFields && fieldsEl ? new FieldsPanel({
  view, panel: fieldsEl, fields: layoutFields, readOnly,
  hasOptions: Object.keys(snap.layout?.variables ?? {}).length > 0,
  openOptions: () => $("#open-options").click(),
  onToggle: (open) => {
    if (open && comments.open) comments.hide();
    if (open && references?.open) references.hide();
    split.classList.toggle("with-panel", open || comments.open || Boolean(references?.open));
    split.classList.toggle("fields-open", open);
    $("#open-fields")?.setAttribute("aria-pressed", String(open));
  },
}) : null;
$("#open-fields")?.addEventListener("click", () => fieldsPanel?.toggle());

// -- Conflicts (§12.3) ------------------------------------------------------
let conflicts = [];
const conflictLoader = new AnchorLoader(session, "conflict", async () => {
  const data = await api("GET", `/api/documents/${docId}/conflicts`);
  conflicts = data.conflicts;
  showConflictCount();
  return { version: data.version, items: conflicts };
});
const loadConflicts = () => conflictLoader.load();

function showConflictCount() {
  const slot = $("#conflict-count");
  slot.replaceChildren();
  if (conflicts.length) {
    slot.append(el("button", { type: "button", class: "link danger-text", onclick: () => openConflicts() },
      `${conflicts.length} ${conflicts.length === 1 ? "conflict" : "conflicts"}`));
  }
  const note = $("#export-conflicts");
  note.textContent = conflicts.length
    ? `${conflicts.length} ${conflicts.length === 1 ? "conflict is" : "conflicts are"} not resolved yet.` : "";
}

function conflictRange(id) {
  return view.state.field(anchorsField).conflict.find((a) => a.id === id);
}

function openConflicts(focusId = null) {
  if (!conflicts.length) return;
  const dialog = el("dialog", { "aria-labelledby": "conflicts-title", class: "wide" });
  const render = () => {
    if (!conflicts.length) { dialog.close(); return; }
    const items = conflicts.map((c) => {
      const range = conflictRange(c.id);
      const ours = range ? view.state.sliceDoc(range.from, range.to) : c.ours_text;
      return el("li", { class: c.id === focusId ? "conflict active" : "conflict" },
        el("p", { class: "muted" }, c.version_label ?? c.source),
        el("div", { class: "compare" },
          el("div", {}, el("p", { class: "muted" }, t("Current text")), el("pre", { class: "alt" }, ours || t("(empty)"))),
          el("div", {}, el("p", { class: "muted" }, t("Their text")), el("pre", { class: "alt" }, c.theirs_text || t("(deleted)")))),
        readOnly ? null : el("div", { class: "buttons" },
          el("button", { type: "button", onclick: () => resolveConflict(c, "ours") }, t("Keep current text")),
          el("button", { type: "button", onclick: () => resolveConflict(c, "theirs") }, t("Use their text")),
          el("button", { type: "button", onclick: () => resolveConflict(c, "edit") }, t("Edit by hand"))));
    });
    dialog.replaceChildren(el("div", { class: "form" },
      el("h2", { id: "conflicts-title" }, t("Conflicts")),
      el("p", { class: "muted" }, t("Both sides changed these passages. The document kept the current text; the whole other version is saved under Versions.")),
      el("ul", { class: "plain conflicts" }, items),
      el("div", { class: "buttons" }, el("button", { type: "button", "data-close-dialog": true }, t("Close")))));
  };

  async function resolveConflict(c, how) {
    const range = conflictRange(c.id);
    if (how === "edit") {
      dialog.close();
      if (range) {
        view.dispatch({ selection: EditorSelection.range(range.from, range.to), scrollIntoView: true });
        view.focus();
      }
      toast("Edit the passage, then choose Keep current text to close the conflict.");
      return;
    }
    if (how === "theirs" && range) {
      view.dispatch({ changes: { from: range.from, to: range.to, insert: c.theirs_text } });
    }
    try {
      await api("PATCH", `/api/documents/${docId}/conflicts/${c.id}`, {});
      conflicts = conflicts.filter((x) => x.id !== c.id);
      showConflictCount();
      render();
    } catch (err) {
      toast(err.message, { error: true });
    }
  }

  render();
  dialog.addEventListener("close", () => { dialog.remove(); loadConflicts(); });
  document.body.append(dialog);
  dialog.showModal();
}

// -- Share (§14.1) -------------------------------------------------------------
const shareButton = $("#open-share");
if (shareButton) {
  const dialog = $("#share-dialog");
  const list = $("#share-links");
  const roleSelect = $("#share-form").elements.role;
  async function loadLinks() {
    list.replaceChildren(el("li", { class: "muted" }, t("Loading…")));
    try {
      const data = await api("GET", `/api/projects/${projectId}/shares`);
      roleSelect.replaceChildren(...data.roles.map((r) => el("option", { value: r }, r[0].toUpperCase() + r.slice(1))));
      list.replaceChildren(...data.links.map((l) => el("li", {},
        el("span", { class: "spacer" }, l.label || t("Link"), " ",
          el("span", { class: "muted" }, `(${[l.role, l.has_password && "password", l.expired ? "expired" : l.expires_at && "expires",
                                               l.guests && `${l.guests} ${l.guests === 1 ? "guest" : "guests"}`].filter(Boolean).join(", ")})`)),
        el("button", { type: "button", class: "link", onclick: async () => {
          await api("DELETE", `/api/projects/${projectId}/shares/${l.id}`);
          loadLinks();
        } }, t("Revoke")))));
      if (!data.links.length) list.append(el("li", { class: "muted" }, t("No share links yet.")));
      $("#share-guest-note").hidden = data.guests;
    } catch (err) {
      list.replaceChildren(el("li", { class: "error" }, err.message));
    }
  }
  shareButton.addEventListener("click", () => {
    $("#share-result").replaceChildren();
    dialog.showModal();
    loadLinks();
  });
  $("#share-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const f = event.target.elements;
    try {
      const { url } = await api("POST", `/api/projects/${projectId}/shares`, {
        role: f.role.value, expires_days: f.expires_days.value, password: f.password.value, label: f.label.value });
      const input = el("input", { readonly: true, value: url, "aria-label": t("Share link"), "data-select-on-focus": true });
      $("#share-result").replaceChildren(input,
        el("button", { type: "button", onclick: () => { navigator.clipboard?.writeText(url); toast(t("Link copied.")); } }, t("Copy link")));
      input.focus();
      event.target.reset();
      loadLinks();
    } catch (err) {
      toast(err.message, { error: true });
    }
  });
}

// -- Publish (§14.4) -----------------------------------------------------------
const publishButton = $("#open-publish");
if (publishButton) {
  const dialog = $("#publish-dialog");
  const form = $("#publish-form");
  const f = form.elements;
  const current = $("#publish-current");
  let busy = false;

  function show(pub) {
    $("#publish-remove").hidden = !pub;
    $("#publish-again").hidden = !pub;
    $("#publish-submit").textContent = pub ? t("Save") : t("Publish");
    if (!pub) {
      current.replaceChildren(el("span", { class: "muted" }, t("This document isn't published.")));
    } else if (pub.status === "error") {
      current.replaceChildren(el("span", { class: "error" }, t("Publishing failed: {error}", { error: pub.lastError || t("unknown error") })));
    } else {
      const state = pub.outdated ? (pub.autoUpdate ? t("updating soon") : t("has unpublished changes")) : t("up to date");
      current.replaceChildren(t("Published at"), " ", el("a", { href: pub.url, target: "_blank", rel: "noopener noreferrer" }, pub.url),
        el("span", { class: "muted" }, ` (${state})`), " ",
        el("button", { type: "button", class: "link", onclick: () => { navigator.clipboard?.writeText(pub.url); toast(t("Link copied.")); } }, t("Copy link")));
    }
    if (pub) {
      f.visibility.value = pub.visibility;
      f.slug.value = pub.visibility === "public" ? pub.slug : "";
      f.autoUpdate.checked = pub.autoUpdate;
      f.withPdf.checked = pub.withPdf;
    }
    syncVisibility();
  }
  function syncVisibility() {
    const unlisted = f.visibility.value === "unlisted";
    $("#publish-slug-label").hidden = unlisted;
    f.slug.required = !unlisted;
  }
  async function call(method, url, body) {
    if (busy) return;
    busy = true;
    current.replaceChildren(el("span", { class: "muted" }, method === "DELETE" ? t("Unpublishing…") : t("Publishing…")));
    try {
      show((await api(method, url, body)).publication);
    } catch (err) {
      current.replaceChildren(el("span", { class: "error" }, err.message));
    } finally {
      busy = false;
    }
  }
  const base = `/api/documents/${docId}/publication`;
  form.addEventListener("change", syncVisibility);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    call("PUT", base, { visibility: f.visibility.value, slug: f.slug.value.trim().toLowerCase(),
                        autoUpdate: f.autoUpdate.checked, withPdf: f.withPdf.checked });
  });
  $("#publish-again").addEventListener("click", () => call("POST", `${base}/run`));
  $("#publish-remove").addEventListener("click", async () => {
    if (await confirmDialog(t("Unpublish this document? Its page disappears from the web."), t("Unpublish"))) {
      call("DELETE", base);
    }
  });
  publishButton.addEventListener("click", async () => {
    dialog.showModal();
    current.replaceChildren(el("span", { class: "muted" }, t("Loading…")));
    try {
      const { publication, baseUrl } = await api("GET", base);
      $("#publish-base").textContent = baseUrl;
      show(publication);
    } catch (err) {
      current.replaceChildren(el("span", { class: "error" }, err.message));
    }
  });
}

// -- Keyboard -------------------------------------------------------------
document.addEventListener("keydown", (event) => {
  const mod = event.metaKey || event.ctrlKey;
  if (mod && event.key === "s") {
    event.preventDefault();
    toast(session.status().state === "saved" ? t("Everything is saved.") : t("Saving automatically…"));
  } else if (mod && event.key === "e" && !event.shiftKey) {
    event.preventDefault();
    $("#open-export").click();
  } else if (mod && event.altKey && event.code === "KeyR" && references) {
    event.preventDefault();
    references.toggle();
  } else if (mod && event.altKey && (event.key === "m" || event.code === "KeyM")) {
    event.preventDefault();
    if (snap.can.comment) comments.compose();
  }
});

// Last, once everything above exists: the offline editor starts at the synced version
// and replays this browser's edits on top as unconfirmed changes, which are sent (or
// merged, after a long time) once the server is back.
if (offlineRecord) {
  session.resume(offlineRecord);
  session.reportStatus();
}

// The editor page: wires the editor, collaboration, preview, export, layout options and versions.
import { EditorState, EditorView } from "codemirror";
import { DocSession } from "../collab/peer.js";
import { PresenceSender, PresenceView, peersField } from "../collab/presence.js";
import { Socket } from "../collab/socket.js";
import { HtmlPreview, headingId } from "../preview/html.js";
import { PdfPreview } from "../preview/pdf.js";
import { getDoc, prune } from "../store/idb.js";
import { $, api, confirmDialog, el, toast, wireCommon } from "../ui/dom.js";
import { setLibrary } from "./citations.js";
import { baseExtensions } from "./setup.js";

const snap = JSON.parse($("#snapshot").textContent);
const docId = snap.doc.id;
const projectId = snap.project.id;
const kind = snap.doc.kind;
const readOnly = !["owner", "editor"].includes(snap.role);
const isMarkdown = kind === "markdown";

wireCommon();

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
let layoutOptions = { ...snap.layoutOptions };

const session = new DocSession({
  socket, docId, clientID: snap.clientID, text: snap.text, version: snap.version,
  makeState: (text, version) => EditorState.create({ doc: text, extensions: [base, peersField,
                                                                              session.extension(version)] }),
  onStatus: showStatus,
  onEvent: handleEvent,
  onUpdates: () => presence.flush(),
});
const presenceSender = new PresenceSender(session, socket, docId);
const presence = new PresenceView(session, showPeers);

const view = new EditorView({
  state: session.makeState(snap.text, snap.version),
  parent: $("#editor"),
  dispatchTransactions(trs, v) {
    v.update(trs);
    if (trs.some((tr) => tr.docChanged)) docChanged();
    if (trs.some((tr) => tr.selection || tr.docChanged)) presenceSender.schedule();
    if (trs.some((tr) => tr.selection)) cursorMoved();
  },
});
session.start(view);
view.focus();

getDoc(docId).then((record) => {
  if (record && record.clientID !== undefined && session.restore(record)) {
    toast("Unsaved edits from this browser are being merged in.");
    session.reportStatus();
  }
  prune();
});

function showStatus({ state, waiting }) {
  const label = {
    saved: "Saved",
    saving: "Saving…",
    reconnecting: "Reconnecting…",
    offline: waiting ? `Offline · ${waiting} ${waiting === 1 ? "change" : "changes"} waiting` : "Offline",
  }[state];
  $("#save-state").textContent = label;
}

function showPeers(peers) {
  const slot = $("#presence");
  slot.replaceChildren(...peers.map((p, i) => el("span", { class: "peer" },
    el("span", { class: "dot", style: `--peer: var(--peer-${p.color})`, "aria-hidden": "true" }),
    el("span", { class: "name" }, `${p.name}${i < peers.length - 1 ? "," : ""}`))));
  slot.title = peers.length ? `Also here: ${peers.map((p) => p.name).join(", ")}` : "";
}

socket.onError((msg) => toast(msg.message ?? "The connection reported an error.", { error: true }));

function handleEvent(msg) {
  switch (msg.t) {
    case "opened":
      presence.replaceAll(msg.version, msg.peers ?? []);
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
        toast(`Offline edits were merged. ${msg.conflicts} ${msg.conflicts === 1 ? "passage needs" : "passages need"} a look.`);
      }
      loadConflicts();
      break;
    case "conflicts":
      loadConflicts();
      break;
    case "rendered":
      showRendered(msg);
      break;
    case "presence":
      presence.receive(msg.version, msg.peer);
      break;
    case "error":
      toast(msg.message ?? "Something went wrong.", { error: true });
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
  const n = event.data.words.toLocaleString();
  const notes = event.data.notes ? ` · ${event.data.notes.toLocaleString()} in notes` : "";
  $("#word-count").textContent = isMarkdown ? `${n} words${notes}` : `${n} words`;
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
  try {
    const res = await fetch(`/api/projects/${projectId}/citations`, {
      credentials: "same-origin", headers: citationsEtag ? { "If-None-Match": citationsEtag } : {},
    });
    if (res.status === 304 || !res.ok) return;
    citationsEtag = res.headers.get("etag") ?? "";
    const data = await res.json();
    library = new Map(data.items.map((item) => [item.id, item]));
    view.dispatch({ effects: setLibrary.of(library) });
  } catch {
    /* offline: keep the current library */
  }
}
loadCitations();
// Bibliography files edited elsewhere show up on focus.
window.addEventListener("focus", loadCitations);

// -- Preview -------------------------------------------------------------
const split = $("#split");
const pane = $("#preview-pane");
const frame = $("#preview");
const html = new HtmlPreview(frame);
const pdf = new PdfPreview(pane);
const narrow = window.matchMedia("(max-width: 900px)");
let target = stored("dw.previewTarget", "html") === "pdf" ? "pdf-typst" : "html";
let previewOn = stored("dw.preview", "on") === "on";
let previewTimer = null;
let shown = { html: -1, "pdf-typst": -1 };

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
  if (narrow.matches) {
    split.classList.remove("no-preview");
    button.textContent = split.classList.contains("show-preview") ? "Edit" : "Preview";
    button.setAttribute("aria-pressed", String(split.classList.contains("show-preview")));
  } else {
    split.classList.remove("show-preview");
    split.classList.toggle("no-preview", !previewOn);
    button.textContent = "Preview";
    button.setAttribute("aria-pressed", String(previewOn));
  }
  frame.hidden = target !== "html";
  pdf.setVisible(target !== "html");
}
applyPreviewLayout();
narrow.addEventListener("change", () => { applyPreviewLayout(); requestPreview(0); });

$("#toggle-preview").addEventListener("click", () => {
  if (narrow.matches) {
    split.classList.toggle("show-preview");
  } else {
    previewOn = !previewOn;
    store("dw.preview", previewOn ? "on" : "off");
  }
  applyPreviewLayout();
  requestPreview(0);
});

for (const radio of document.querySelectorAll('input[name="preview-target"]')) {
  radio.addEventListener("change", () => {
    target = radio.value === "pdf" ? "pdf-typst" : "html";
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
    if (sent) $("#preview-state").textContent = "Rendering…";
  }, delay);
}

function showRendered(msg) {
  const diag = $("#diagnostic");
  const errors = (msg.diagnostics ?? []).filter((d) => d.severity === "error");
  if (errors.length) {
    const d = errors[0];
    diag.textContent = d.line ? `Line ${d.line}: ${d.message}` : d.message;
    diag.className = "diagnostic-error";
    diag.title = errors.map((e) => e.message).join("\n");
  } else {
    diag.textContent = "";
    diag.className = "";
    diag.title = "";
  }
  if (msg.target !== target) return;
  if (!msg.url) {
    $("#preview-state").textContent = "Preview failed";
    return;
  }
  if ((msg.version ?? 0) < shown[msg.target]) return;
  shown[msg.target] = msg.version ?? 0;
  $("#preview-state").textContent = "Preview";
  if (msg.target === "html") html.show(msg.url);
  else pdf.show(msg.url);
}

// One-way scroll sync: the heading above the cursor (§6.4).
let lastHeading = null;
function cursorMoved() {
  if (!isMarkdown || target !== "html" || !outline.length) return;
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
  status.textContent = "Exporting…";
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
        status.append(el("a", { href: s.url, download: "" }, "Download"), " ");
      } else {
        status.append("The export failed. ");
      }
      const message = s.diagnostics?.find((d) => d.severity === "error")?.message;
      if (message) status.append(el("span", { class: "error" }, message));
      return;
    }
    status.textContent = "The export is taking too long.";
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
        el("option", { value: v, selected: v === current }, v === "" ? "None" : v))));
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
  if (!Object.keys(vars).length) fields.append(el("p", { class: "muted" }, "This layout has no options."));
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
    shown = { html: -1, "pdf-typst": -1 };
    requestPreview(0);
  } catch (err) {
    toast(err.message, { error: true });
  }
});

// -- Versions -------------------------------------------------------------
const versionsDialog = $("#versions-dialog");
const fmt = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
async function loadVersions() {
  const list = $("#versions-list");
  list.replaceChildren(el("li", { class: "muted" }, "Loading…"));
  try {
    const { versions } = await api("GET", `/api/documents/${docId}/versions`);
    list.replaceChildren(...versions.map((v) => el("li", { class: "row" },
      el("span", { class: "spacer" }, el("strong", {}, v.label), " ",
        el("span", { class: "muted" }, `${fmt.format(new Date(v.created_at))}${v.author ? ` · ${v.author}` : ""}`)),
      readOnly ? null : el("button", { type: "button", onclick: () => restoreVersion(v) }, "Restore"))));
    if (!versions.length) list.append(el("li", { class: "muted" }, "No named versions yet."));
  } catch (err) {
    list.replaceChildren(el("li", { class: "error" }, err.message));
  }
}
async function restoreVersion(v) {
  if (!await confirmDialog(`Restore “${v.label}”? The current text is kept as a version first.`, "Restore")) return;
  try {
    await api("POST", `/api/documents/${docId}/versions/${v.id}/restore`);
    toast(`Restored “${v.label}”.`);
    loadVersions();
  } catch (err) {
    toast(err.message, { error: true });
  }
}
$("#open-versions").addEventListener("click", () => {
  versionsDialog.showModal();
  loadVersions();
});
if (readOnly) $("#version-form").hidden = true;
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

// -- Conflicts ------------------------------------------------------------
let conflicts = [];
async function loadConflicts() {
  try {
    ({ conflicts } = await api("GET", `/api/documents/${docId}/conflicts`));
  } catch {
    return;
  }
  const slot = $("#conflict-count");
  slot.replaceChildren();
  if (conflicts.length) {
    slot.append(el("button", { type: "button", class: "link", onclick: openConflicts },
      `${conflicts.length} ${conflicts.length === 1 ? "conflict" : "conflicts"}`));
  }
}
loadConflicts();

function openConflicts() {
  const items = conflicts.map((c) => el("li", {},
    el("p", { class: "muted" }, `${c.version_label ?? c.source}`),
    el("p", {}, "Their text: ", el("q", {}, c.theirs_text || "(deleted)")),
    el("div", { class: "buttons" },
      el("button", { type: "button", onclick: () => navigator.clipboard?.writeText(c.theirs_text) }, "Copy their text"),
      readOnly ? null : el("button", { type: "button", onclick: () => resolveConflict(c, dialog) }, "Mark resolved"))));
  const dialog = el("dialog", { "aria-label": "Conflicts" },
    el("div", { class: "form" },
      el("h2", {}, "Conflicts"),
      el("p", { class: "muted" }, "Both sides changed these passages. The document kept its current text; the other side's text is below, and their whole version is saved under Versions."),
      el("ul", { class: "plain" }, items),
      el("div", { class: "buttons" }, el("button", { type: "button", "data-close-dialog": true }, "Close"))));
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
}

async function resolveConflict(c, dialog) {
  try {
    await api("PATCH", `/api/documents/${docId}/conflicts/${c.id}`, {});
    dialog.close();
    await loadConflicts();
    if (conflicts.length) openConflicts();
  } catch (err) {
    toast(err.message, { error: true });
  }
}

// -- Keyboard -------------------------------------------------------------
document.addEventListener("keydown", (event) => {
  const mod = event.metaKey || event.ctrlKey;
  if (mod && event.key === "s") {
    event.preventDefault();
    toast(session.status().state === "saved" ? "Everything is saved." : "Saving automatically…");
  } else if (mod && event.key === "e" && !event.shiftKey) {
    event.preventDefault();
    $("#open-export").click();
  }
});

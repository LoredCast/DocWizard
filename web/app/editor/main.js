// The editor page: wires the editor, collaboration, preview, export, layout options and versions.
import { Compartment, Decoration, EditorSelection, EditorState, EditorView, StateEffect, StateField, closeSearchPanel, openSearchPanel,
         redo, searchPanelOpen, undo } from "codemirror";
import { DocSession } from "../collab/peer.js";
import { PresenceSender, PresenceView, peersField } from "../collab/presence.js";
import { Socket } from "../collab/socket.js";
import { HtmlPreview, headingId } from "../preview/html.js";
import { PdfPreview } from "../preview/pdf.js";
import { LocalRenderer } from "../preview/local.js";
import { openTimeline } from "./timeline.js";
import { openAssist } from "./assist.js";
import { getCitations, getDoc, getDocByUrl, prune, putCitations, putLayout } from "../store/idb.js";
import { $, api, confirmDialog, el, t, toast, wireCommon } from "../ui/dom.js";
import { AnchorLoader, anchorsField, conflictGutter } from "./anchors.js";
import { setCiteHooks, setLibrary } from "./citations.js";
import {
  activeFormatting, cycleHeading, insertCitation,
  toggleBold, toggleBulletList, toggleCode, toggleInlineMath, toggleItalic,
  toggleOrderedList, toggleQuote, toggleStrike, toggleTaskList,
} from "./commands.js";
import { CommentsPanel } from "./comments.js";
import { FieldsPanel, LayoutFields, fieldsExtension } from "./fields.js";
import { GrammarChecker, grammarExtension, showMatches } from "./grammar.js";
import { figures } from "./figures.js";
import { ImageInserter } from "./images.js";
import { InsertTools } from "./insert-tools.js";
import { RichPaste } from "./paste.js";
import { documentLanguage } from "../workers/grammar-text.js";
import { ReferencesPanel } from "./references.js";
import { SelectionBubble, reviewFrontMatter } from "./review.js";
import { characterCompletion, openOutline } from "./script.js";
import { access, accessExtensions, baseExtensions } from "./setup.js";
import { setSuggestMode, suggestField, suggestMode } from "./suggest-mode.js";
import { TitleCardEditor, refreshCard } from "./title-card.js";
import { frontMatter } from "./cite-tools.js";
import { ChaptersDrawer } from "./chapters.js";
import { addColumn, addRow, cycleAlign, deleteColumn, deleteRow, deleteTable, tableAt } from "./tables.js";
import { setWriterMode, writerField, writerMode } from "./writer-mode.js";
import { livePreview } from "./live-preview.js";
import { tableView } from "./table-view.js";
import { SlideTools } from "./slides.js";
import { Corkboard, DailyTarget, TableRead, openBreakdown, setHighlight, studioExtension } from "./studio.js";

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
  $("#crumb-doc .crumb-name").textContent = snap.doc.path;
  $("#doc-kind").textContent = snap.doc.kind === "markdown" ? "Markdown"
    : snap.doc.kind.charAt(0).toUpperCase() + snap.doc.kind.slice(1);
  for (const node of document.querySelectorAll("[data-layout-name]")) node.textContent = snap.layout?.name ?? "";
}
const docId = snap.doc.id;
const projectId = snap.project.id;
const kind = snap.doc.kind;
const readOnly = !snap.can.edit;
const isMarkdown = kind === "markdown";
// The review view (§14.1): the text as it reads, read-only, for commenting and suggesting.
const review = Boolean(snap.review) && isMarkdown;
// Screenplay layouts: scenes in the outline, character names to complete, a page estimate.
const isScript = isMarkdown && Boolean(snap.layout?.script);
let characters = [];

const split = $("#split");
// Data sheets (.csv, .tsv) open as a grid to type into; the CSV text is a switch away (§6.3).
const isSheet = /\.(csv|tsv)$/i.test(snap.doc.path);
let sheet = null;
// Slide decks: the slide sorter and the presenter view, which shows the web deck (§6.3).
const isDeck = isMarkdown && Boolean(snap.layout?.slides);
let slideTools = null;
let deckUrl = null;
let deckVersion = -1;

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
// The review view keeps the text focusable (read-only), so keyboard users can select passages too;
// on touch screens that would bring up the keyboard, so there it isn't.
const coarse = window.matchMedia("(pointer: coarse)").matches;
const base = baseExtensions({ kind, readOnly: readOnly || review, reading: review, live: false, label: t("Text of {name}", { name: snap.doc.path }),
                              editable: review ? !coarse : !readOnly });
let bubble = null; // the review view's selection toolbar

// Suggest mode (§8.10): typing becomes suggestions instead of changing the text. Anyone who
// may comment can turn it on; it opens the text to typing for commenters and in the review view.
const canSuggest = Boolean(snap.can.comment);
const suggestKey = `dw.suggest.${docId}`;
const suggest = canSuggest ? suggestMode({
  submit: sendSuggestion, touch: coarse, onComment: () => comments.compose(),
  labels: { box: t("Suggestion"), strike: t("Delete"), replace: t("Replace"), comment: t("Comment") },
}) : null;

// Grammar and spelling (§6.8): only while writing Markdown, and not on a metered connection
// (Harper is a 20 MB download the first time).
const grammar = isMarkdown && !readOnly && !review && !navigator.connection?.saveData
  && snap.grammar && snap.grammar.choice !== "off" && (snap.grammar.harper || snap.grammar.languagetool)
  ? new GrammarChecker({
    grammar: snap.grammar,
    docKey: docId,
    onError: (message) => toast(t("Grammar checking stopped: {message}", { message })),
  })
  : null;
let library = new Map();
let references = null; // the references panel, created below
let fieldsPanel = null; // the fields panel, created below
// Clicking commented text opens its thread. On touch screens, a tap on it while the keyboard is
// down opens the thread without focusing the text, so the keyboard stays down.
const markClicks = EditorView.domEventHandlers({
  mousedown(event, v) {
    if (!coarse || v.hasFocus || !event.target.closest?.("[data-thread]")) return false;
    event.preventDefault();
    return true;
  },
  click(event) {
    const mark = event.target.closest?.("[data-thread]");
    if (mark) comments.focusThread(Number(mark.dataset.thread));
    return false;
  },
});
// The line a click in the preview jumped to lights up for a moment.
const flashLine = StateEffect.define();
const flashField = StateField.define({
  create: () => Decoration.none,
  update(set, tr) {
    let out = set.map(tr.changes);
    for (const e of tr.effects) {
      if (e.is(flashLine)) {
        out = e.value === null ? Decoration.none
          : Decoration.set([Decoration.line({ class: "cm-jump-flash" }).range(e.value)]);
      }
    }
    return out;
  },
  provide: (f) => EditorView.decorations.from(f),
});
// Pasted and dropped images go to figures/ and become figures in the text.
const images = !readOnly && !review && !offlineRecord && (isMarkdown || kind === "latex") && snap.can.upload !== false
  ? new ImageInserter({ projectId, kind })
  : null;
// Formatted text from Word, Google Docs and web pages is pasted as Markdown.
const richPaste = isMarkdown && !readOnly && !review ? new RichPaste() : null;
let layoutOptions = { ...snap.layoutOptions };
// The front matter fields the layout prints (§9.4): autocomplete and hover help in the
// front matter, and the Fields panel below.
const layoutFields = isMarkdown ? new LayoutFields(snap.fields, {
  layoutName: snap.layout?.name ?? "",
  titlePage: () => layoutOptions.titlepage ?? snap.fields?.titlePage ?? false,
}) : null;
// The front matter shows as a title card to type into; the YAML is a switch away.
const yamlKey = `dw.yaml.${docId}`;
const titleCard = isMarkdown && !review ? new TitleCardEditor({
  fields: layoutFields,
  locked: (state) => state.readOnly || Boolean(state.field(suggestField, false)?.on),
  onMore: () => fieldsPanel?.show(),
  yaml: stored(yamlKey, "off") === "on",
  onYaml: (on) => store(yamlKey, on ? "on" : "off"),
}) : null;
// Links, footnotes, tables and cross-references through popovers; pictures as figures; writer mode.
const tools = isMarkdown ? new InsertTools({ view: null, host: $("#editor").closest(".pane"),
                                             readOnly: (state) => state.readOnly,
                                             projectId: offlineRecord ? null : projectId }) : null;
const marksKey = "dw.marks";
const figureField = isMarkdown ? figures({ projectId: offlineRecord ? null : projectId,
                                           locked: (state) => state.readOnly }) : null;

// Plain text editing ("Raw"): the Markdown as it is, in a monospaced font, without the live preview,
// the title card, figures or tables. The choice is kept per browser, like "Show Markdown marks".
const rawKey = "dw.raw";
const rendering = new Compartment();
let raw = isMarkdown && !review && stored(rawKey, "off") === "on";
const richExtensions = isMarkdown ? [
  livePreview({ reading: review }), titleCard ? titleCard.extension() : [], figureField ?? [], tableView({ reading: review }),
] : [];

// The search panel in the app's colours (CodeMirror's own are light grey gradients).
const searchTheme = EditorView.theme({
  ".cm-panel .cm-textfield, .cm-panel .cm-button": {
    background: "var(--bg)", backgroundImage: "none", color: "var(--fg)", border: "1px solid var(--line)",
    borderRadius: "var(--radius)", font: "inherit", fontSize: "14px", padding: "2px 8px",
  },
  ".cm-panel .cm-button:hover, .cm-panel .cm-button:active": { background: "var(--hover)", backgroundImage: "none" },
  ".cm-panel button[name=close]": { color: "var(--muted)", fontSize: "18px" },
});

// The search panel's words, in the interface language.
const searchPhrases = EditorState.phrases.of(Object.fromEntries(
  ["Find", "Replace", "next", "previous", "all", "match case", "regexp", "by word", "replace", "replace all", "close",
   "current match", "on line", "Go to line", "go"].map((w) => [w, t(w)])));

const session = new DocSession({
  socket, docId, clientID: snap.clientID, text: snap.text, version: snap.version,
  makeState: (text, version) => EditorState.create({ doc: text, extensions: [
    base, searchPhrases, searchTheme, peersField, anchorsField, conflictGutter((id) => openConflicts(id)), markClicks,
    review ? reviewFrontMatter : [], richPaste ? richPaste.extension() : [], images ? images.extension() : [],
    flashField,
    layoutFields ? fieldsExtension(layoutFields) : [], rendering.of(raw ? [] : richExtensions),
    isMarkdown ? writerField.init(() => !review && stored(marksKey, "on") === "off") : [],
    tools ? tools.extension({ writer: writerMode }) : [], isScript ? characterCompletion(() => characters) : [],
    grammar ? grammarExtension({ ignore: (m, w) => { grammar.ignore(m, w); checkGrammar(); },
                                 addWord: (w) => { grammar.addWord(w); checkGrammar(); } }) : [],
    suggest ? suggest.extension : [], isMarkdown ? studioExtension : [],
    session.extension(version)] }),
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
    if (trs.some((tr) => tr.docChanged)) { docChanged(); sheet?.docChanged(); slideTools?.docChanged(); }
    if (trs.some((tr) => tr.selection || tr.docChanged)) {
      presenceSender.schedule();
      updateFormatBar();
    }
    if (trs.some((tr) => tr.selection)) cursorMoved();
    if (bubble && trs.some((tr) => tr.selection || tr.docChanged)) {
      if (suggesting()) bubble.hide(); // suggest mode has its own selection toolbar
      else bubble.update();
    }
    if (trs.some((tr) => tr.selection || tr.docChanged)) references?.cursorMoved();
    if (trs.some((tr) => tr.docChanged)) fieldsPanel?.docChanged();
  },
});
const { text: _text, version: _version, clientID: _clientID, ...meta } = snap;
session.keep = { url: location.pathname, meta, pinned: Boolean(snap.doc.pinned) };
session.start(view);
if (isSheet) {
  const { SheetView } = await import("./sheet.js");
  const editorNode = $("#editor");
  sheet = new SheetView({ view, host: editorNode.closest(".pane"), path: snap.doc.path, docId,
                          readOnly: readOnly || review, canEdit: !readOnly && !offlineRecord });
  const textKey = `dw.sheet-text.${docId}`;
  const asText = el("button", { type: "button", "aria-pressed": "false", title: t("Edit the file as text") }, t("Text"));
  const showText = (on) => {
    asText.setAttribute("aria-pressed", String(on));
    editorNode.hidden = !on;
    sheet.node.classList.toggle("text-mode", on);
    store(textKey, on ? "on" : "off");
    if (!on) sheet.refresh();
  };
  asText.addEventListener("click", () => showText(asText.getAttribute("aria-pressed") !== "true"));
  sheet.toolbar.append(asText);
  showText(stored(textKey, "off") === "on");
}

// Where the cursor starts: where it was when this document was last open in this browser, or else
// at the first line of text after the front matter (not on the title card). On touch screens the
// text isn't focused, so the keyboard only comes up when someone taps into the text.
const cursorKey = `dw.cursor.${docId}`;
function firstBodyLine(state) {
  const fm = isMarkdown ? frontMatter(state.sliceDoc(0, Math.min(state.doc.length, 20000))) : null;
  let line = state.doc.lineAt(fm ? Math.min(fm.to, state.doc.length) : 0);
  while (line.number < state.doc.lines && !line.text.trim()) line = state.doc.line(line.number + 1);
  return line.text.trim() ? line.from : (fm ? Math.min(fm.to, state.doc.length) : 0);
}
function focusEditor() {
  if (!coarse) view.focus();
}
function placeCursor() {
  const saved = Number.parseInt(stored(cursorKey, ""), 10);
  const fm = isMarkdown ? frontMatter(view.state.sliceDoc(0, Math.min(view.state.doc.length, 20000))) : null;
  const usable = Number.isInteger(saved) && saved >= (fm ? fm.to : 0) && saved <= view.state.doc.length;
  const pos = usable ? saved : firstBodyLine(view.state);
  view.dispatch({ selection: { anchor: pos }, effects: EditorView.scrollIntoView(pos, { y: usable ? "center" : "nearest" }) });
  let cursorTimer = null;
  const keepCursor = () => store(cursorKey, String(view.state.selection.main.head));
  view.dom.addEventListener("keyup", () => { clearTimeout(cursorTimer); cursorTimer = setTimeout(keepCursor, 500); });
  view.dom.addEventListener("pointerup", () => { clearTimeout(cursorTimer); cursorTimer = setTimeout(keepCursor, 500); });
  window.addEventListener("pagehide", keepCursor);
  focusEditor();
}
// The keyboard came up: the line being typed on stays in view above it.
document.addEventListener("dw:keyboard", () => {
  view.requestMeasure();
  view.dispatch({ effects: EditorView.scrollIntoView(view.state.selection.main.head, { y: "nearest", yMargin: 48 }) });
});
if (tools) {
  tools.view = view;
  tools.listen(view.dom);
}

// -- Formatting bar and mobile actions menu -------------------------------
const formatBar = $("#format-bar");
// Commands that open a popover return "popover", so the popover keeps the focus.
const POPOVER = "popover";
const FORMAT_CMDS = {
  undo, redo,
  bold: toggleBold, italic: toggleItalic, strike: toggleStrike, code: toggleCode,
  heading: cycleHeading, quote: toggleQuote, bullet: toggleBulletList,
  ordered: toggleOrderedList, task: toggleTaskList,
  link: (v, btn) => { tools.link(btn); return POPOVER; },
  footnote: (v, btn) => { tools.note({ anchor: btn }); return POPOVER; },
  table: (v, btn) => { tools.table(btn); return POPOVER; },
  insert: (v, btn) => {
    tools.menu(btn, [
      [t("Footnote"), "", (a) => tools.note({ anchor: a })],
      [t("Cross-reference"), t("Figure 1, Table 2"), (a) => tools.crossRef(a)],
      [t("Citation"), "[@]", () => { insertCitation(view); view.focus(); }],
      [t("Math"), t("Fractions, matrices, Greek letters"), (a) => tools.math(a)],
      [t("Table"), "", (a) => tools.table(a)],
      [t("Diagram"), t("Flowchart, graph, drawing"), (a) => tools.diagram(a)],
      [t("Table from data"), ".csv", (a) => tools.data(a, "table")],
      [t("Chart"), t("Bar, line, pie from .csv"), (a) => tools.data(a, "chart")],
    ]);
    return POPOVER;
  },
  math: toggleInlineMath, cite: insertCitation,
  refs: () => references?.toggle(),
  image: (v) => images?.pick(v),
  marks: () => setMarks(writerMode(view.state)),
  raw: () => setRaw(!raw),
  find: (v) => toggleFind(v),
};

// Find and replace (Ctrl+F) from a button too; the panel opens below the text.
function toggleFind(v = view) {
  if (searchPanelOpen(v.state)) closeSearchPanel(v);
  else openSearchPanel(v);
  return POPOVER; // the panel takes the focus
}
const TABLE_CMDS = { row: addRow, column: addColumn, align: cycleAlign, "delete-row": deleteRow,
                     "delete-column": deleteColumn, delete: deleteTable };
const tableBar = $("#table-bar");

// "Show Markdown marks" on (the line with the cursor shows its source) or off (writer mode).
function setMarks(on) {
  store(marksKey, on ? "on" : "off");
  view.dispatch({ effects: setWriterMode.of(!on) });
  updateFormatBar();
}

// Plain text editing on or off: the rendering extensions come and go; the text stays as it is.
function setRaw(on) {
  if (!isMarkdown || review) return;
  raw = on;
  store(rawKey, on ? "on" : "off");
  view.dispatch({ effects: rendering.reconfigure(on ? [] : richExtensions) });
  applyRaw();
  view.dispatch({ effects: EditorView.scrollIntoView(view.state.selection.main.head, { y: "center" }) });
}
function applyRaw() {
  split.classList.toggle("raw-mode", raw);
  for (const b of document.querySelectorAll('[data-cmd="raw"], #toggle-raw')) b.setAttribute("aria-pressed", String(raw));
  updateFormatBar();
}
$("#toggle-raw")?.addEventListener("click", () => setRaw(!raw));

if (formatBar) {
  formatBar.hidden = readOnly || !isMarkdown || review;
  for (const bar of [formatBar, tableBar]) {
    bar?.addEventListener("mousedown", (event) => {
      if (event.target.closest("[data-cmd], [data-table]")) event.preventDefault();
    });
  }
  formatBar.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-cmd]");
    if (!btn || readOnly || !isMarkdown || review) return;
    const cmd = FORMAT_CMDS[btn.dataset.cmd];
    if (cmd) {
      if (cmd(view, btn) !== POPOVER) view.focus();
      updateFormatBar();
    }
  });
  tableBar?.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-table]");
    const cmd = btn && TABLE_CMDS[btn.dataset.table];
    if (!cmd || readOnly) return;
    cmd(view);
    view.focus();
    updateFormatBar();
  });
}

function updateFormatBar() {
  if (!formatBar || formatBar.hidden) {
    if (tableBar) tableBar.hidden = true;
    return;
  }
  if (tableBar) tableBar.hidden = !tableAt(view.state, view.state.selection.main.head);
  const marks = formatBar.querySelector('[data-cmd="marks"]');
  marks?.setAttribute("aria-pressed", String(!writerMode(view.state)));
  if (marks) marks.hidden = raw; // in plain text every mark shows anyway
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
applyRaw();

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

// The bar's shortcuts to menu items (wide screens): they show what the item shows and do what it does.
for (const proxy of document.querySelectorAll("[data-proxy]")) {
  const item = document.getElementById(proxy.dataset.proxy);
  if (!item) { proxy.remove(); continue; }
  const mirror = () => {
    proxy.hidden = item.hidden;
    proxy.textContent = item.textContent;
    for (const name of ["aria-pressed", "title"]) {
      if (item.hasAttribute(name)) proxy.setAttribute(name, item.getAttribute(name));
      else proxy.removeAttribute(name);
    }
    proxy.classList.toggle("has-unread", item.classList.contains("has-unread"));
  };
  new MutationObserver(mirror).observe(item, { attributes: true, childList: true, characterData: true, subtree: true });
  mirror();
  proxy.addEventListener("click", () => item.click());
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
      if (suggest) suggest.retry(view);
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
      if (suggest) setSuggesting(stored(suggestKey, "off") === "on", { quiet: true });
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
      if (offlineRecord && msg.code === "bad_client_id") {
        // A copy kept offline for a long time: its client ID expired. Online again, the real
        // editor page opens with a new one and merges this browser's edits (§6.5).
        session.persistNow().then(() => location.reload());
        break;
      }
      toast(msg.message ?? t("Something went wrong."), { error: true });
      break;
    default:
  }
}

// -- Word count and outline (worker) -----------------------------------------
let outline = [];
let studio = null; // the writing studio: its daily target and corkboard, set up below
let corkboard = null;
const normseite = snap.layout?.id === "normseite" || snap.layout?.parent === "normseite";
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
  studio?.update({ words: event.data.words, ...(normseite ? { normseiten: event.data.normseiten ?? 0 } : {}),
                   ...(event.data.script ? { pages: event.data.script.pages } : {}) });
  const sp = event.data.script;
  if (sp) {
    characters = sp.characters;
    // About a page a minute on screen (§9.3).
    const pages = sp.pages.toLocaleString(locale, { maximumFractionDigits: 1 });
    const minutes = Math.max(1, Math.round(sp.pages));
    $("#script-stats").textContent = !sp.pages ? ""
      : sp.pages === 1 ? t("about 1 page · 1 min") : t("about {pages} pages · {minutes} min", { pages, minutes });
    $("#script-stats").title = t("{n} scenes. Estimated at 55 lines a page; the PDF has the exact count.",
                                 { n: sp.scenes });
  }
};
function countWords() {
  statsId += 1;
  stats.postMessage({ id: statsId, text: view.state.doc.toString(), script: isScript, normseite });
}
countWords();

let grammarTimer = null;
async function checkGrammar() {
  if (!grammar) return;
  const doc = view.state.doc;
  const text = doc.toString();
  const matches = await grammar.check(text, documentLanguage(text, snap.project.lang));
  // A check that an edit overtook is dropped; the edit schedules a new one.
  if (matches && view.state.doc === doc) showMatches(view, matches);
}
if (grammar) grammarTimer = setTimeout(checkGrammar, 1500);

function docChanged() {
  corkboard?.docChanged();
  clearTimeout(statsTimer);
  statsTimer = setTimeout(countWords, 400);
  if (grammar) {
    clearTimeout(grammarTimer);
    grammarTimer = setTimeout(checkGrammar, 800);
  }
  requestPreview();
}

function showOutline() {
  openOutline(view, outline, { script: isScript });
}
$("#open-outline")?.addEventListener("click", showOutline);

// A first draft of slides for this document, in a new project (§6.3).
$("#make-deck")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const made = await api("POST", `/api/documents/${docId}/companion-deck`);
    toast(t("The slides are ready in a new project."));
    location.href = made.url;
  } catch (err) {
    toast(err.message, { error: true });
  } finally {
    button.disabled = false;
  }
});

if (isDeck) {
  slideTools = new SlideTools({
    view, readOnly, options: () => layoutOptions, getUrl: () => deckUrl,
    requestHtml: () => socket.send({ t: "render", doc: docId, version: session.version(), target: "html",
                                     layout: snap.layout.id, options: layoutOptions }),
  });
  $("#open-sorter")?.addEventListener("click", () => slideTools.openSorter());
  $("#open-presenter")?.addEventListener("click", () => slideTools.openPresenter());
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
let previewOn = stored(review ? "dw.reviewPreview" : "dw.preview", review ? "off" : "on") === "on";
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
  onPick: (pick) => findInSource(pick),
  hint: t("Double-click to show this in the source"),
  label: t("PDF preview"),
  scrollRatio: takeScrollRatio(),
});
if (isLatex) {
  for (const radio of document.querySelectorAll('input[name="preview-target"]')) radio.closest("label").hidden = true;
}

$(`input[name="preview-target"][value="${target === "html" ? "html" : "pdf"}"]`).checked = true;

// PDFs made in this browser (§6.4): as you type, without the server, and offline. Chosen with
// "In this browser"; the offline editor, and an editor whose connection dropped, always use it.
const canLocal = Boolean(snap.browserRender) && pdfTarget === "pdf-typst";
let localOn = canLocal && (Boolean(offlineRecord) || stored("dw.localRender", "off") === "on");
let local = null;
let lastServerPdf = null;
const localToggle = $("#local-render");
if (localToggle) {
  localToggle.closest("label").hidden = !canLocal;
  localToggle.checked = localOn;
  localToggle.disabled = Boolean(offlineRecord);
  localToggle.addEventListener("change", () => {
    localOn = localToggle.checked;
    store("dw.localRender", localOn ? "on" : "off");
    if (localOn && target !== "pdf-typst") $('input[name="preview-target"][value="pdf"]').click();
    else requestPreview(0);
  });
}

function showDiagnostic(text, title = text) {
  const diag = $("#diagnostic");
  diag.textContent = text;
  diag.className = text ? "diagnostic-error" : "";
  diag.title = text ? title : "";
}

let localSeq = 0;
async function renderLocally() {
  local ??= new LocalRenderer({ projectId, docId, offline: Boolean(offlineRecord) });
  const mine = ++localSeq;
  setPreviewState(t("Rendering in this browser…"));
  try {
    const out = await local.render(view.state.doc.toString());
    if (!out || mine !== localSeq || target !== "pdf-typst") return;
    showDiagnostic("");
    clearPreviewError("pdf");
    pdf.showData(out.pdf);
    pane.dataset.madeIn = "browser";
    setPreviewState(t("Preview"), true);
  } catch (err) {
    if (mine !== localSeq) return;
    setPreviewState(t("Preview failed"));
    const message = String(err.message ?? err);
    showDiagnostic(err.status === 404 || message === "not available offline"
      ? t("The PDF preview isn't available offline yet: open it once while online.")
      : message.split("\n")[0], message);
  }
}
const darkPaper = $("#dark-paper");
darkPaper.checked = stored("dw.darkPaper", "off") === "on";
html.setDark(darkPaper.checked);
pane.classList.toggle("dark-paper", darkPaper.checked);

// The preview header's state; on phones, where the Preview tab already says it, only news shows.
function setPreviewState(text, idle = false) {
  const node = $("#preview-state");
  node.textContent = text;
  node.classList.toggle("idle", idle);
}

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
    focusEditor();
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
  store(review ? "dw.reviewPreview" : "dw.preview", previewOn ? "on" : "off");
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

function requestPreview(delay) {
  clearTimeout(previewTimer);
  if (!previewVisible()) return;
  const inBrowser = canLocal && target === "pdf-typst";
  delay ??= inBrowser && localOn ? 400 : target === "html" ? 300 : 1000;
  previewTimer = setTimeout(() => {
    if (inBrowser && localOn) {
      renderLocally();
      return;
    }
    const sent = socket.send({ t: "render", doc: docId, version: session.version(), target,
                               layout: snap.layout.id, options: layoutOptions });
    if (sent) setPreviewState(t("Rendering…"));
    else if (inBrowser) renderLocally();
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
  if (msg.target === "html" && msg.url && (msg.version ?? 0) >= deckVersion) {
    deckVersion = msg.version ?? 0;
    deckUrl = msg.url;
    slideTools?.rendered(msg.url);
  }
  if (msg.target !== target) return;
  if (msg.url && msg.target !== "html") lastServerPdf = msg.url;
  if (localOn && msg.target === "pdf-typst") return;
  if (!msg.url) {
    // "limited": a render limit of the server; the next edit tries again.
    setPreviewState(msg.status === "limited" ? t("Preview paused") : t("Preview failed"));
    return;
  }
  if ((msg.version ?? 0) < shown[msg.target]) return;
  shown[msg.target] = msg.version ?? 0;
  setPreviewState(t("Preview"), true);
  if (msg.target === "html") {
    clearPreviewError("html");
    html.show(msg.url);
  } else {
    clearPreviewError("pdf");
    delete pane.dataset.madeIn;
    pdf.show(msg.url);
  }
}

// One-way scroll sync: the heading above the cursor (§6.4).
let lastHeading = null;
function cursorMoved() {
  if (!isMarkdown || !outline.length || !previewVisible()) return;
  const line = view.state.doc.lineAt(view.state.selection.main.head).number;
  let current = null;
  for (const h of outline) {
    if (h.scene) continue; // scene headings aren't headings in the preview
    if (h.line > line) break;
    current = h;
  }
  // Which of the headings with this title it is, for the PDF.
  const nth = current ? outline.filter((o) => !o.scene && o.line < current.line && o.title === current.title).length : 0;
  const id = current ? headingId(current.title) : "";
  const key = `${target}:${id}:${nth}`;
  if (key === lastHeading) return;
  lastHeading = key;
  // The PDF follows along too: it scrolls to the heading's page once that page has text.
  if (target === "html") html.scrollTo(id);
  else pdf.scrollToHeading(current?.title ?? "", nth).then((done) => { if (!done && lastHeading === key) lastHeading = null; });
}

// From the PDF preview back to the source (§6.4): double-click (double-tap) a spot. LaTeX answers
// with SyncTeX; otherwise the server finds the words around the spot in the build's files.
let picking = 0;
async function findInSource(pick) {
  // A PDF made in this browser asks with the server's last build, or else looks for the words.
  const key = /\/api\/renders\/([0-9a-f]{64})\//.exec(pick.url ?? lastServerPdf ?? "")?.[1];
  if (!key) {
    if (!findWords(pick)) toast(t("This spot couldn't be found in the source."));
    return;
  }
  const mine = ++picking;
  try {
    const hit = await api("POST", `/api/projects/${projectId}/source-at`, {
      key, page: pick.page, pages: pick.pages, x: pick.x, y: pick.y, words: pick.words, at: pick.at, doc: docId,
    });
    if (mine !== picking) return;
    if (hit.path !== snap.doc.path) {
      // The other file's editor opens the same preview at the same place.
      try { sessionStorage.setItem(`dw.pdfScroll.${projectId}`, String(pdf.scrollRatio())); } catch { /* ignore */ }
      location.href = `/p/${projectId}/${hit.path.split("/").map(encodeURIComponent).join("/")}#L${hit.line}C${hit.column}`;
      return;
    }
    jumpToLine(hit.line, hit.column);
  } catch (err) {
    if (mine !== picking) return;
    toast(err.status === 404 ? t("This spot couldn't be found in the source.") : err.message, { error: err.status !== 404 });
  }
}

// The words around a picked spot, found in the text: the longest run of them that occurs once.
function findWords({ words = [], at = 0 }) {
  const text = view.state.doc.toString();
  const gap = "[^\\p{L}\\p{N}]+";
  for (let size = Math.min(6, words.length); size >= 2; size--) {
    for (let start = Math.max(0, at - size + 1); start <= Math.min(at, words.length - size); start++) {
      const re = new RegExp(words.slice(start, start + size).map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join(gap), "giu");
      const hits = [...text.matchAll(re)];
      if (hits.length !== 1) continue;
      const pos = hits[0].index;
      const line = view.state.doc.lineAt(pos);
      jumpToLine(line.number, pos - line.from);
      return true;
    }
  }
  return false;
}

function takeScrollRatio() {
  try {
    const key = `dw.pdfScroll.${projectId}`;
    const value = Number(sessionStorage.getItem(key) ?? 0);
    sessionStorage.removeItem(key);
    return Number.isFinite(value) ? value : 0;
  } catch {
    return 0;
  }
}

function jumpToLine(line, column = 0) {
  const doc = view.state.doc;
  const target = doc.line(Math.max(1, Math.min(line, doc.lines)));
  const pos = Math.min(target.from + Math.max(0, column), target.to);
  if (narrow.matches && split.classList.contains("show-preview")) setMobilePreview(false);
  view.dispatch({ selection: { anchor: pos }, effects: [EditorView.scrollIntoView(pos, { y: "center" }), flashLine.of(target.from)] });
  focusEditor();
  setTimeout(() => view.dispatch({ effects: flashLine.of(null) }), 1600);
}

// Links to a line (#L12, or #L12C4 with a column), from a click in another file's preview.
{
  const m = /^#L(\d+)(?:C(\d+))?$/.exec(location.hash);
  if (m) {
    history.replaceState(null, "", location.pathname + location.search);
    requestAnimationFrame(() => jumpToLine(Number(m[1]), Number(m[2] ?? 0)));
  }
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
  const button = event.submitter ?? $('#export-form button[value="export"]');
  const how = button.value; // "pdf", "print" or "export"
  const targetName = button.dataset.target ?? new FormData(event.target).get("target");
  if (!targetName) return;
  // The handout is the slides' PDF with the handout option on.
  const handout = !button.dataset.target && event.target.querySelector("input[name=target]:checked")?.hasAttribute("data-handout");
  const options = handout ? { ...layoutOptions, handout: true } : layoutOptions;
  status.textContent = how === "print" ? t("Preparing to print…") : t("Exporting…");
  const buttons = [...event.target.querySelectorAll("button[type=submit]")];
  for (const b of buttons) b.disabled = true;
  try {
    const { job } = await api("POST", `/api/projects/${projectId}/export`,
                              { target: targetName, doc: docId, options });
    for (let i = 0; i < 600; i++) {
      await new Promise((r) => setTimeout(r, i < 10 ? 500 : 1500));
      const s = await api("GET", `/api/jobs/${job}`);
      if (s.status === "queued" || s.status === "running") continue;
      status.replaceChildren();
      if (s.url && how === "print") {
        status.append(t("The print dialog opens."), " ",
                      el("a", { href: s.url, target: "_blank", rel: "noopener" }, t("Open the PDF")), " ");
        printPdf(s.url);
      } else if (s.url) {
        // The download starts by itself; the link stays for another copy.
        const link = el("a", { href: s.url, download: "" }, t("Download again"));
        status.append(t("Downloaded."), " ", link, " ");
        link.click();
      } else {
        status.append(t("The export failed."), " ");
      }
      const message = s.diagnostics?.find((d) => d.severity === "error")?.message;
      if (message) status.append(el("span", { class: "error" }, message));
      return;
    }
    status.textContent = t("The export is taking too long.");
  } catch (err) {
    status.textContent = err.message;
  } finally {
    for (const b of buttons) b.disabled = false;
  }
});

// Printing: the browser's PDF viewer prints from a hidden frame on this page. Phones can't print
// a frame, so there the PDF opens on its own, with the system's print and share.
function printPdf(url) {
  const old = $("#print-frame");
  if (old) old.replaceWith();
  if (/iPhone|iPad|Android/i.test(navigator.userAgent)) {
    window.open(url, "_blank", "noopener");
    return;
  }
  const frame = el("iframe", { id: "print-frame", class: "print-frame", src: url, title: t("Print"), "aria-hidden": "true", tabindex: "-1" });
  frame.addEventListener("load", () => {
    try {
      frame.contentWindow.focus();
      frame.contentWindow.print();
    } catch {
      window.open(url, "_blank", "noopener");
    }
  }, { once: true });
  document.body.append(frame);
}

// -- Layout and its options -------------------------------------------------
// The dialog shows the project's layout and the options it offers authors. Each option
// starts at the layout's own setting; only options someone changed are stored, so they
// stay overrides and everything else keeps following the layout (and a new layout).
const optionsDialog = $("#options-dialog");
const optionsForm = $("#options-form");
const OPTION_LABELS = { "a4": "A4", "a5": "A5", "us-letter": "US Letter", "us-legal": "US Legal" };
let optionsLayout = snap.layout; // the layout picked in the dialog
let layoutList = null; // from /api/layouts, loaded when the dialog first opens

const sameValue = (a, b) => (a ?? "") === (b ?? "") || (typeof a === "number" && Number(b) === a);

function optionValue(name, spec) {
  const input = optionsForm.elements[name];
  if (!input) return undefined;
  if (spec.type === "boolean") return input.checked;
  if (spec.type === "number") return input.value === "" ? undefined : Number(input.value);
  return input.value;
}

function optionField(name, spec, value) {
  const label = t(spec.label ?? name);
  const current = value ?? spec.default;
  const id = `option-${name.replace(/[^a-z0-9]/gi, "-")}`;
  const disabled = !snap.can.change_layout;
  let control;
  if (spec.type === "boolean") {
    control = el("input", { type: "checkbox", id, name, checked: Boolean(current), disabled });
  } else if (spec.type === "enum") {
    control = el("select", { id, name, disabled }, spec.values.map((v) =>
      el("option", { value: v, selected: v === (current ?? "") },
        spec.labels?.[v] ?? OPTION_LABELS[v] ?? (v === "" ? t("None") : v))));
  } else if (spec.type === "number") {
    control = el("input", { type: "number", id, name, min: spec.min, max: spec.max, step: spec.step ?? "any",
                            value: current ?? "", disabled });
  } else {
    control = el("input", { type: "text", id, name, value: current ?? "", maxlength: 200, disabled });
  }
  const wide = spec.type === "text" || Boolean(spec.labels);
  const field = el("div", { class: `design-field type-${spec.type}${wide ? " wide" : ""}` },
    el("span", { class: "field-head" },
      el("label", { class: "field-label", for: id }, label),
      el("span", { class: "changed-dot", title: t("Changed") }),
      disabled ? null : el("button", { type: "button", class: "link reset-field", title: t("Use the layout's setting"),
                                       "aria-label": t("Reset {label}", { label }), onclick: () => {
        if (spec.type === "boolean") control.checked = Boolean(spec.default);
        else control.value = spec.default ?? "";
        markChanged();
      } }, "↺")),
    control);
  const markChanged = () => field.classList.toggle("changed", !sameValue(optionValue(name, spec), spec.default));
  control.addEventListener(spec.type === "boolean" || spec.type === "enum" ? "change" : "input", markChanged);
  queueMicrotask(markChanged);
  return field;
}

function renderOptions(carry) {
  const fields = $("#options-fields");
  const vars = optionsLayout?.variables ?? {};
  fields.replaceChildren(...Object.entries(vars).map(([name, spec]) => optionField(name, spec, carry[name])));
  if (!Object.keys(vars).length) fields.append(el("p", { class: "muted" }, t("This layout has no options.")));
  const edit = $("#options-edit-layout");
  if (edit && optionsLayout) edit.href = `/layouts/${encodeURIComponent(optionsLayout.id)}`;
}

// The changed options of the layout picked in the dialog.
function changedOptions() {
  const next = {};
  for (const [name, spec] of Object.entries(optionsLayout?.variables ?? {})) {
    const value = optionValue(name, spec);
    if (value !== undefined && !sameValue(value, spec.default)) next[name] = value;
  }
  return next;
}

async function loadLayoutPicker() {
  const pick = $("#options-layout");
  if (!pick || !snap.can.change_layout || layoutList) return;
  try {
    layoutList = (await api("GET", "/api/layouts")).layouts;
  } catch {
    return; // offline: the current layout's options still work
  }
  if (!layoutList.some((l) => l.id === snap.layout.id)) layoutList.unshift(snap.layout);
  pick.replaceChildren(...layoutList.map((l) => el("option", { value: l.id, selected: l.id === snap.layout.id }, l.name)));
  pick.closest("label").hidden = false;
}

$("#options-layout")?.addEventListener("change", (event) => {
  const carry = changedOptions();
  optionsLayout = layoutList.find((l) => l.id === event.target.value) ?? snap.layout;
  renderOptions(carry);
});

$("#open-options").addEventListener("click", async () => {
  optionsLayout = snap.layout;
  const pick = $("#options-layout");
  if (pick) pick.value = snap.layout.id;
  renderOptions(layoutOptions);
  $("#options-save").hidden = !snap.can.change_layout;
  optionsDialog.showModal();
  await loadLayoutPicker();
});

optionsForm.addEventListener("submit", async (event) => {
  if (event.submitter?.value !== "save") return;
  event.preventDefault();
  const next = changedOptions();
  const layoutChanged = optionsLayout && optionsLayout.id !== snap.layout.id;
  try {
    await api("PATCH", `/api/projects/${projectId}`,
              { ...(layoutChanged ? { layout: optionsLayout.id } : {}), layoutOptions: next });
    if (layoutChanged) {
      // Fields, scripts and the preview all follow the layout: load the document with the new one.
      for (let i = 0; i < 50 && session.unconfirmed() > 0; i++) await new Promise((r) => setTimeout(r, 100));
      location.reload();
      return;
    }
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
      ? t("Since then: {added} words added, {removed} removed", { added: diff.added, removed: diff.removed })
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
// The writing helper (§8.10), for people who turned it on: its answers become suggestions.
const assistButton = $("#open-assist");
if (assistButton && snap.assist && !offlineRecord) {
  assistButton.hidden = false;
  assistButton.addEventListener("click", () => openAssist({
    view, session, docId, onSuggested: () => { comments.load(); if (!narrow.matches) comments.show(); },
  }));
}
$("#open-timeline")?.addEventListener("click", () => openTimeline({ docId, name: snap.doc.path }));
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
  members: snap.members ?? [], isUser: !snap.user?.guest && !offlineRecord, selectText: !review,
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
  split.classList.toggle("comments-open", !sidePanel.hidden);
  if (!suggesting()) bubble?.update();
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
$("#new-suggestion").hidden = !canSuggest;
$("#new-suggestion").addEventListener("click", () => setSuggesting(!suggesting()));

function suggesting() {
  return Boolean(view.state.field(suggestField, false)?.on);
}

// Turn suggest mode on or off: the banner says it's on, and the text opens to typing for
// people who otherwise only read it.
function setSuggesting(on, { quiet = false } = {}) {
  if (!suggest) return;
  store(suggestKey, on ? "on" : "off");
  const locked = !on && (readOnly || review);
  view.dispatch({ effects: [setSuggestMode.of(on),
    access.reconfigure(accessExtensions(locked, on || (review ? !coarse : !readOnly))), refreshCard.of(null)] });
  const button = $("#new-suggestion");
  button.setAttribute("aria-pressed", String(on));
  button.title = on ? t("Stop suggesting: type to change the text again (Ctrl+Alt+G)")
    : t("Suggest changes: what you type becomes suggestions (Ctrl+Alt+G)");
  $("#suggest-banner").hidden = !on;
  const reviewBanner = $("#review-banner");
  if (reviewBanner) reviewBanner.hidden = on; // one banner at a time
  // The banner takes the formatting bar's place: formatting doesn't apply to suggestions.
  if (formatBar) formatBar.hidden = on || readOnly || !isMarkdown || review;
  updateFormatBar();
  bubble?.hide();
  if (on && !quiet) view.focus();
}
$("#suggest-off")?.addEventListener("click", () => setSuggesting(false));
if (suggest) setSuggesting(stored(suggestKey, readOnly && !review ? "on" : "off") === "on", { quiet: true });
// Send the suggestion being typed before the page goes away.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden" && suggest) suggest.finish(view);
});

// One suggestion typed in suggest mode, sent as a thread with positions at the synced version.
async function sendSuggestion(locate) {
  for (let i = 0; i < 50 && session.unconfirmed() > 0; i++) await new Promise((r) => setTimeout(r, 100));
  const item = locate();
  if (!item) return true;
  const synced = session.toSynced([[item.from, item.to]]);
  if (!synced) return false;
  try {
    await api("POST", `/api/documents/${docId}/comments`,
              { body: "", from: synced[0][0], to: synced[0][1], version: session.version(), suggestion: item.text });
  } catch (err) {
    if (err.code === "unchanged") return true;
    if (!err.status || err.status >= 500) {
      toast(t("Your suggestion will be sent once you're back online."));
      return false;
    }
    toast(t("A suggestion couldn't be sent: {message}", { message: err.message }), { error: true });
    return true;
  }
  await comments.load();
  return true;
}

if (review) {
  if (snap.can.comment) {
    bubble = new SelectionBubble(view, { onComment: () => comments.compose(), onSuggest: () => comments.compose(true) });
  }
  if (!narrow.matches) comments.show();
}

// -- References (§10.4) ------------------------------------------------------
const refsPanel = $("#refs-panel");
references = isMarkdown && refsPanel ? new ReferencesPanel({
  view, panel: refsPanel, readOnly, canBrowse, projectId: offlineRecord ? null : projectId,
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
  reveal: titleCard ? (key) => titleCard.reveal(view, key) : null,
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

$("#open-find")?.addEventListener("click", () => toggleFind());

// -- Chapters and files ------------------------------------------------------
const chapters = new ChaptersDrawer({
  host: split, projectId, docId, docPath: snap.doc.path, files: snap.files ?? [],
  canEdit: Boolean(snap.can.edit), canBuild: Boolean(snap.can.change_layout), offline: Boolean(offlineRecord),
  // Edits still on their way go first, so nothing is lost by leaving the page.
  settle: async () => { for (let i = 0; i < 50 && session.unconfirmed() > 0; i++) await new Promise((r) => setTimeout(r, 100)); },
  onBuild: () => { shown = { html: -1, "pdf-typst": -1, "pdf-latex": -1 }; requestPreview(0); },
  openReferences: references ? () => references.show() : null,
});
for (const button of document.querySelectorAll("[data-open-chapters]")) {
  // From the Actions menu, focus goes back to the menu's button, as the menu closes.
  button.addEventListener("click", () => chapters.toggle(button.closest("#toolbar-buttons") ? moreBtn : button));
}

// -- Writing studio: corkboard, breakdown, line highlights, table read, daily target -----------
corkboard = isMarkdown ? new Corkboard({
  host: split, view, script: isScript, projectId, docPath: snap.doc.path, offline: Boolean(offlineRecord),
  locked: (state) => readOnly || review || state.readOnly || suggesting(),
  openChapters: () => chapters.show(),
}) : null;
for (const button of document.querySelectorAll("[data-open-corkboard]")) {
  button.hidden = !corkboard;
  button.addEventListener("click", () => corkboard?.toggle(button.closest("#toolbar-buttons") ? moreBtn : button));
}
if (corkboard && location.hash === "#corkboard") {
  history.replaceState(null, "", location.pathname + location.search);
  requestAnimationFrame(() => corkboard.show());
}
const speakerChip = $("#speaker-chip");
function setSpeaker(name) {
  view.dispatch({ effects: setHighlight.of(name) });
  if (speakerChip) {
    speakerChip.hidden = !name;
    speakerChip.textContent = name ? t("Highlighting {name} ×", { name }) : "";
    speakerChip.title = t("Stop highlighting");
  }
}
speakerChip?.addEventListener("click", () => setSpeaker(null));
const breakdownButton = $("#open-breakdown");
if (breakdownButton) {
  breakdownButton.hidden = !isScript;
  breakdownButton.addEventListener("click", () => openBreakdown(view, { name: snap.doc.path.split("/").pop(), onHighlight: setSpeaker }));
}
const tableRead = isScript && TableRead.supported()
  ? new TableRead({ view, host: $(".editor-pane"), projectId, lang: documentLanguage(view.state.doc.toString(), snap.project.lang) })
  : null;
const readButton = $("#start-table-read");
if (readButton) {
  readButton.hidden = !tableRead;
  readButton.addEventListener("click", () => tableRead?.start());
}
window.addEventListener("pagehide", () => tableRead?.stop());
studio = isMarkdown && !readOnly && !review ? new DailyTarget({
  button: $("#session-target"), projectId, docId,
  units: isScript ? ["pages", "words"] : normseite ? ["normseiten", "words"] : ["words"],
}) : null;
const targetButton = $("#open-target");
if (targetButton) {
  targetButton.hidden = !studio;
  targetButton.addEventListener("click", () => studio?.edit());
}
countWords();

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
          el("span", { class: "muted" }, `(${[l.role, l.review && t("review view"), l.has_password && "password", l.expired ? "expired" : l.expires_at && "expires",
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
        role: f.role.value, expires_days: f.expires_days.value, password: f.password.value, label: f.label.value,
        review: f.review.checked });
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
  } else if (mod && event.altKey && (event.key === "g" || event.code === "KeyG")) {
    event.preventDefault();
    if (canSuggest) setSuggesting(!suggesting());
  } else if (mod && event.altKey && event.code === "KeyO" && isMarkdown) {
    event.preventDefault();
    showOutline();
  }
});

// -- Rename and delete the document ------------------------------------------
async function settle() {
  // Edits still on their way go first, so nothing is lost by leaving the page.
  for (let i = 0; i < 50 && session.unconfirmed() > 0; i++) await new Promise((r) => setTimeout(r, 100));
}

const docUrl = (path) => `/p/${projectId}/${path.split("/").map(encodeURIComponent).join("/")}`;
const renameDialog = $("#rename-dialog");
$("#rename-doc")?.addEventListener("click", () => {
  $("#rename-path").value = snap.doc.path;
  renameDialog.showModal();
});
$("#rename-form")?.addEventListener("submit", async (event) => {
  if (event.submitter?.value !== "save") return;
  event.preventDefault();
  try {
    await settle();
    const { path } = await api("PATCH", `/api/documents/${docId}`, { path: $("#rename-path").value.trim() });
    location.href = docUrl(path);
  } catch (err) {
    toast(err.message, { error: true });
  }
});
$("#delete-doc")?.addEventListener("click", async () => {
  if (!(await confirmDialog(t("Move {name} to the trash?", { name: snap.doc.path })))) return;
  try {
    await settle();
    await api("DELETE", `/api/documents/${docId}`);
    location.href = `/p/${projectId}`;
  } catch (err) {
    toast(err.message, { error: true });
  }
});

// Last, once everything above exists: the offline editor starts at the synced version
// and replays this browser's edits on top as unconfirmed changes, which are sent (or
// merged, after a long time) once the server is back.
if (offlineRecord) {
  session.resume(offlineRecord);
  session.reportStatus();
}
placeCursor();

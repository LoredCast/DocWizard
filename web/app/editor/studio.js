// The writing studio (ARCHITECTURE.md §6.3): the corkboard of chapters and scenes, a screenplay's
// production breakdown, the character line highlighter, the table read and daily targets. The
// models are in manuscript.js; this module is their interface. Moving a card is one ordinary edit
// of the text, so collaboration, history and undo work as for typing.
import { Decoration, EditorView, StateEffect, StateField } from "codemirror";
import { api, el, t, toast } from "../ui/dom.js";
import { board, breakdown, moveCard, moveLane, readingScript, speechLines } from "./manuscript.js";

const fmtNumber = (n) => Number(n).toLocaleString(document.documentElement.lang || undefined);
const stored = (key, fallback = null) => {
  try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; }
};
const store = (key, value) => {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private window */ }
};

// -- Line highlights: one character's speeches, and the line being read aloud -------------------
export const setHighlight = StateEffect.define(); // a character's name, or null
export const setReading = StateEffect.define(); // [first line, last line], or null

function lineDecos(doc, ranges, cls) {
  const out = [];
  for (const [a, b] of ranges) {
    for (let n = Math.max(1, a); n <= Math.min(b, doc.lines); n++) out.push(Decoration.line({ class: cls }).range(doc.line(n).from));
  }
  return out;
}

const highlightField = StateField.define({
  create: () => ({ name: null, reading: null, decos: Decoration.none }),
  update(value, tr) {
    let { name, reading } = value;
    let changed = tr.docChanged;
    for (const e of tr.effects) {
      if (e.is(setHighlight)) { name = e.value; changed = true; }
      if (e.is(setReading)) { reading = e.value; changed = true; }
    }
    if (!changed) return value;
    if (!name && !reading) return { name, reading, decos: Decoration.none };
    const doc = tr.state.doc;
    const decos = [
      ...(name ? lineDecos(doc, speechLines(doc.toString(), name), "cm-speaker") : []),
      ...(reading ? lineDecos(doc, [reading], "cm-reading") : []),
    ].sort((x, y) => x.from - y.from);
    return { name, reading, decos: Decoration.set(decos, true) };
  },
  provide: (f) => EditorView.decorations.from(f, (v) => v.decos),
});
export const studioExtension = [highlightField];

export function highlighted(state) {
  return state.field(highlightField, false)?.name ?? null;
}

// -- Corkboard ---------------------------------------------------------------------------------
export class Corkboard {
  constructor({ host, view, script = false, locked, projectId, docPath, offline, openChapters }) {
    Object.assign(this, { host, view, script, locked, projectId, docPath, offline, openChapters });
    this.panel = el("section", { class: "corkboard", id: "corkboard", role: "region", "aria-label": t("Corkboard"), hidden: true });
    this.panel.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !e.defaultPrevented) { e.preventDefault(); this.hide(true); }
    });
    host.append(this.panel);
    this.timer = null;
    this.pressed = false; // a card is held down (it may become a drag)
    this.stale = false; // the text changed while it was held
    this.chapters = null;
  }

  get open() { return !this.panel.hidden; }

  toggle(from) { return this.open ? this.hide(true) : this.show(from); }

  show(from = null) {
    this.returnFocus = from ?? document.activeElement;
    this.panel.hidden = false;
    for (const b of document.querySelectorAll("[data-open-corkboard]")) b.setAttribute("aria-pressed", "true");
    this.render();
    this.panel.querySelector(".cork-card")?.focus();
    this.loadChapters();
  }

  hide(restore = false) {
    if (!this.open) return;
    this.panel.hidden = true;
    for (const b of document.querySelectorAll("[data-open-corkboard]")) b.setAttribute("aria-pressed", "false");
    if (restore) this.returnFocus?.focus?.();
  }

  // Someone typed (or a collaborator did): the cards follow, unless a card is held down. Drawing
  // the cards anew under a held card would drop the drag, so that waits for the release.
  docChanged() {
    if (!this.open) return;
    clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      if (this.pressed) this.stale = true;
      else this.render();
    }, 300);
  }

  async loadChapters() {
    if (this.offline) return;
    try {
      const project = await api("GET", `/api/projects/${this.projectId}`);
      const docs = new Set(project.tree.filter((f) => f.type === "document").map((f) => f.path));
      const build = (Array.isArray(project.buildInputs) ? project.buildInputs : []).filter((p) => docs.has(p) && /\.(md|markdown)$/i.test(p));
      this.chapters = build.length > 1 ? build : null;
      if (this.open) this.render();
    } catch { /* the board works without the chapter list */ }
  }

  get readOnly() { return this.locked(this.view.state); }

  render() {
    this.stale = false;
    const focused = this.panel.querySelector(".cork-card:focus")?.dataset.id;
    const text = this.view.state.doc.toString();
    this.model = board(text, { script: this.script });
    const cards = this.model.lanes.reduce((n, l) => n + l.cards.length, 0);
    const words = this.model.lanes.reduce((n, l) => n + l.cards.reduce((m, c) => m + c.words, 0), 0);
    const lanes = el("div", { class: "cork-lanes" }, this.model.lanes.map((lane, i) => this.lane(lane, i)));
    const head = el("div", { class: "pane-head" },
      el("span", {}, t("Corkboard")),
      el("span", { class: "muted" }, cards === 1 ? t("1 card · {words} words", { words: fmtNumber(words) })
        : t("{n} cards · {words} words", { n: cards, words: fmtNumber(words) })),
      el("span", { class: "spacer" }),
      el("button", { type: "button", onclick: () => this.hide(true) }, t("Close")));
    const strip = this.chapters ? el("nav", { class: "cork-chapters", "aria-label": t("Chapters") },
      el("span", { class: "muted" }, t("Chapters:")),
      el("ol", {}, this.chapters.map((p) => el("li", {}, p === this.docPath
        ? el("strong", { "aria-current": "page" }, p)
        : el("a", { href: `/p/${this.projectId}/${p.split("/").map(encodeURIComponent).join("/")}#corkboard` }, p)))),
      this.openChapters ? el("button", { type: "button", class: "link", onclick: () => { this.hide(); this.openChapters(); } }, t("Reorder chapters")) : null)
      : null;
    const hint = el("p", { class: "muted cork-hint" }, this.readOnly
      ? t("Choose a card to go to its scene.")
      : t("Drag a card to move its scene, or use Alt and the arrow keys. Enter goes to the scene."));
    const body = cards ? lanes : el("p", { class: "muted cork-empty" }, this.script
      ? t("No scenes yet. A scene starts with a line like INT. KITCHEN - DAY.")
      : t("No scenes yet. Chapters start with # Title, scenes with ## Title or a scene break (***)."));
    this.panel.replaceChildren(head, el("div", { class: "cork-body" }, strip, hint, body));
    if (focused !== undefined) this.panel.querySelector(`.cork-card[data-id="${focused}"]`)?.focus();
  }

  lane(lane, index) {
    const words = lane.cards.reduce((n, c) => n + c.words, 0);
    const titled = lane.title !== null;
    const moveButtons = titled && !this.readOnly ? [
      el("button", { type: "button", class: "link", "aria-label": t("Move {name} earlier", { name: lane.title }),
                     title: t("Move the chapter earlier"), disabled: index === 0 || this.model.lanes[index - 1].title === null ? true : null,
                     onclick: () => this.applyLane(index, index - 1) }, "↑"),
      el("button", { type: "button", class: "link", "aria-label": t("Move {name} later", { name: lane.title }),
                     title: t("Move the chapter later"), disabled: index === this.model.lanes.length - 1 ? true : null,
                     onclick: () => this.applyLane(index, index + 2) }, "↓"),
    ] : [];
    return el("section", { class: "cork-lane", "data-lane": String(index), "aria-label": lane.title ?? t("Opening") },
      el("h3", {}, el("button", { type: "button", class: "link cork-lane-title", onclick: () => this.go(lane.from) },
        lane.title ?? t("Opening")), el("span", { class: "muted" }, t("{n} words", { n: fmtNumber(words) })), ...moveButtons),
      el("ol", { class: "cork-cards" }, lane.cards.map((c) => this.card(c, index))));
  }

  card(c, laneIndex) {
    const title = c.title ?? (c.synopsis ? null : t("Empty scene"));
    const li = el("li", {
      class: `cork-card${c.kind === "break" ? " after-break" : ""}`, tabindex: "0", "data-id": String(c.id),
      "aria-label": `${c.title ?? (c.synopsis.slice(0, 60) || t("Empty scene"))}, ${t("{n} words", { n: fmtNumber(c.words) })}`,
    },
    title ? el("strong", { class: "cork-title" }, title) : null,
    c.synopsis ? el("span", { class: "cork-synopsis" }, c.synopsis) : null,
    el("span", { class: "cork-meta muted" }, t("{n} words", { n: fmtNumber(c.words) })));
    li.addEventListener("click", () => { if (!this.justDragged) this.go(c.from); });
    li.addEventListener("keydown", (e) => this.cardKey(e, c, laneIndex));
    if (!this.readOnly) li.addEventListener("pointerdown", (e) => this.drag(e, li, c));
    return li;
  }

  cardKey(e, c, laneIndex) {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      this.go(c.from);
      return;
    }
    if (!e.altKey || this.readOnly) return;
    const lane = this.model.lanes[laneIndex];
    const at = lane.cards.findIndex((x) => x.id === c.id);
    let to = null;
    if (e.key === "ArrowUp" && at > 0) to = [laneIndex, at - 1];
    else if (e.key === "ArrowDown" && at < lane.cards.length - 1) to = [laneIndex, at + 2];
    else if (e.key === "ArrowLeft" && laneIndex > 0) to = [laneIndex - 1, null];
    else if (e.key === "ArrowRight" && laneIndex < this.model.lanes.length - 1) to = [laneIndex + 1, 0];
    if (!to) return;
    e.preventDefault();
    this.apply(c, to[0], to[1]);
  }

  // Go to a card's scene in the text.
  go(pos) {
    this.hide();
    this.view.dispatch({ selection: { anchor: pos }, effects: EditorView.scrollIntoView(pos, { y: "start", yMargin: 24 }) });
    this.view.focus();
  }

  dispatch(change, label) {
    if (!change) return false;
    if (this.readOnly) return false;
    this.view.dispatch({ changes: change, userEvent: "move.corkboard", scrollIntoView: false });
    this.render();
    if (label) toast(label);
    return true;
  }

  apply(c, toLane, index) {
    const text = this.view.state.doc.toString();
    // The text may have changed since the board was drawn: find the card again by its line.
    const model = board(text, { script: this.script });
    const card = model.lanes.flatMap((l) => l.cards).find((x) => x.line === c.line && x.kind === c.kind) ?? c;
    if (!this.dispatch(moveCard(text, model, card.id, toLane, index))) return;
    // The moved card keeps the focus, found again by its text.
    const moved = this.model.lanes[toLane]?.cards.find((x) => x.own === card.own);
    if (moved) this.panel.querySelector(`.cork-card[data-id="${moved.id}"]`)?.focus();
  }

  applyLane(from, index) {
    const text = this.view.state.doc.toString();
    this.dispatch(moveLane(text, board(text, { script: this.script }), from, index));
  }

  // Dragging a card with a mouse or a finger: it follows the pointer and drops between the cards
  // of the lane under it. A short movement counts as a click.
  drag(event, li, c) {
    if (event.button !== 0 || event.target.closest("button, a")) return;
    const pointer = event.pointerId;
    this.pressed = true;
    const startX = event.clientX;
    const startY = event.clientY;
    let active = false;
    let drop = null;
    const marker = el("li", { class: "cork-drop", "aria-hidden": "true" });
    const onMove = (e) => {
      if (e.pointerId !== pointer) return;
      if (!active) {
        if (Math.hypot(e.clientX - startX, e.clientY - startY) < 8) return;
        active = true;
        this.dragging = true;
        li.setPointerCapture(e.pointerId);
        li.classList.add("dragging");
      }
      e.preventDefault();
      li.style.transform = `translate(${e.clientX - startX}px, ${e.clientY - startY}px)`;
      const lanes = [...this.panel.querySelectorAll(".cork-lane")];
      const lane = lanes.find((l) => {
        const box = l.getBoundingClientRect();
        return e.clientX >= box.left && e.clientX <= box.right && e.clientY >= box.top && e.clientY <= box.bottom;
      }) ?? lanes.reduce((best, l) => {
        const box = l.getBoundingClientRect();
        const d = Math.abs(e.clientY - (box.top + box.bottom) / 2) + Math.abs(e.clientX - (box.left + box.right) / 2);
        return !best || d < best.d ? { l, d } : best;
      }, null)?.l;
      if (!lane) return;
      const list = lane.querySelector(".cork-cards");
      const others = [...list.querySelectorAll(".cork-card")].filter((x) => x !== li);
      const before = others.find((x) => {
        const box = x.getBoundingClientRect();
        return e.clientY < box.top + box.height / 2;
      });
      list.insertBefore(marker, before ?? null);
      const cards = this.model.lanes[Number(lane.dataset.lane)].cards;
      drop = [Number(lane.dataset.lane), before ? cards.findIndex((x) => String(x.id) === before.dataset.id) : cards.length];
    };
    const onUp = (e) => {
      if (e.pointerId !== pointer) return;
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      this.pressed = false;
      if (!active) {
        // A click: the cards held back while it was down are drawn once it has gone to its scene.
        if (this.stale) this.docChanged();
        return;
      }
      this.dragging = false;
      this.justDragged = true;
      setTimeout(() => { this.justDragged = false; }, 0);
      li.classList.remove("dragging");
      li.style.transform = "";
      marker.remove();
      if (drop) this.apply(c, drop[0], drop[1]);
      else this.render();
    };
    // On the window, so a release outside the card (before it is captured) still ends the press.
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  }
}

// -- Production breakdown -----------------------------------------------------------------------
const eighths = (n) => {
  const pages = Math.floor(n / 8);
  const rest = n % 8;
  return [pages ? String(pages) : "", rest ? `${rest}/8` : ""].filter(Boolean).join(" ") || "0";
};

function csv(rows) {
  return rows.map((r) => r.map((v) => {
    const s = String(v ?? "");
    return /[",\n;]/.test(s) || /^[=+\-@]/.test(s) ? `"${s.replace(/"/g, '""').replace(/^([=+\-@])/, "'$1")}"` : s;
  }).join(",")).join("\r\n");
}

export function openBreakdown(view, { name = "script", onHighlight }) {
  const data = breakdown(view.state.doc.toString());
  const dialog = el("dialog", { class: "wide breakdown-dialog", "aria-labelledby": "breakdown-title" });
  const go = (line) => {
    const pos = view.state.doc.line(Math.min(line, view.state.doc.lines)).from;
    dialog.close();
    view.dispatch({ selection: { anchor: pos }, effects: EditorView.scrollIntoView(pos, { y: "start" }) });
    view.focus();
  };
  const current = highlighted(view.state);
  const tables = {
    scenes: () => el("table", {},
      el("thead", {}, el("tr", {}, el("th", { class: "num" }, "#"), el("th", {}, t("Scene")), el("th", {}, t("Time")),
        el("th", { class: "num" }, t("Pages")), el("th", {}, t("Cast")))),
      el("tbody", {}, data.scenes.map((s) => el("tr", {},
        el("td", { class: "num" }, String(s.n)),
        el("td", {}, el("button", { type: "button", class: "link", onclick: () => go(s.line) }, s.title)),
        el("td", {}, s.time), el("td", { class: "num" }, eighths(s.eighths)), el("td", {}, s.cast.join(", ")))))),
    cast: () => el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, t("Character")), el("th", { class: "num" }, t("Speeches")),
        el("th", { class: "num" }, t("Words")), el("th", {}, t("Scenes")), el("th", {}, el("span", { class: "visually-hidden" }, t("Highlight"))))),
      el("tbody", {}, data.cast.map((c) => el("tr", {},
        el("td", {}, c.name), el("td", { class: "num" }, fmtNumber(c.speeches)), el("td", { class: "num" }, fmtNumber(c.words)),
        el("td", {}, c.scenes.join(", ")),
        el("td", {}, el("button", { type: "button", "aria-pressed": String(current === c.name),
          "aria-label": t("Highlight the lines of {name}", { name: c.name }),
          onclick: () => { onHighlight(current === c.name ? null : c.name); dialog.close(); } },
          current === c.name ? t("Highlighted") : t("Highlight"))))))),
    locations: () => el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, t("Location")), el("th", {}, t("Interior or exterior")), el("th", {}, t("Time")),
        el("th", { class: "num" }, t("Pages")), el("th", {}, t("Scenes")))),
      el("tbody", {}, data.locations.map((l) => el("tr", {},
        el("td", {}, l.name), el("td", {}, l.settings.join(", ")), el("td", {}, l.times.join(", ")),
        el("td", { class: "num" }, eighths(l.eighths)), el("td", {}, l.scenes.join(", ")))))),
  };
  const labels = { scenes: t("Scenes"), cast: t("Cast"), locations: t("Locations") };
  const body = el("div", { class: "breakdown-body", role: "tabpanel", id: "breakdown-panel", tabindex: "0" });
  const tabs = el("div", { class: "breakdown-tabs", role: "tablist", "aria-label": t("Breakdown") });
  const select = (key) => {
    for (const b of tabs.children) b.setAttribute("aria-selected", String(b.dataset.tab === key));
    body.setAttribute("aria-labelledby", `breakdown-tab-${key}`);
    body.replaceChildren(tables[key]());
  };
  for (const key of Object.keys(tables)) {
    tabs.append(el("button", { type: "button", role: "tab", id: `breakdown-tab-${key}`, "data-tab": key,
      "aria-controls": "breakdown-panel", onclick: () => select(key) }, labels[key]));
  }
  tabs.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const keys = Object.keys(tables);
    const at = keys.indexOf(tabs.querySelector('[aria-selected="true"]').dataset.tab);
    const next = keys[(at + (e.key === "ArrowRight" ? 1 : keys.length - 1)) % keys.length];
    select(next);
    tabs.querySelector(`[data-tab="${next}"]`).focus();
  });
  const download = () => {
    const rows = [["Scene", "Heading", "Interior/Exterior", "Location", "Time", "Eighths", "Cast", "Act"],
      ...data.scenes.map((s) => [s.n, s.title, s.setting, s.location, s.time, s.eighths, s.cast.join("; "), s.act ?? ""])];
    const url = URL.createObjectURL(new Blob([`﻿${csv(rows)}\r\n`], { type: "text/csv;charset=utf-8" }));
    const a = el("a", { href: url, download: `${name.replace(/\.[^.]+$/, "")}-breakdown.csv` });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  dialog.append(
    el("h2", { id: "breakdown-title" }, t("Breakdown")),
    el("p", { class: "muted" }, data.scenes.length
      ? t("{scenes} scenes, {cast} speaking parts, {locations} locations, about {pages} pages.", {
        scenes: data.scenes.length, cast: data.cast.length, locations: data.locations.length, pages: eighths(data.eighths) })
      : t("No scenes yet. A scene starts with a line like INT. KITCHEN - DAY.")),
    tabs, body,
    el("div", { class: "buttons" },
      current ? el("button", { type: "button", onclick: () => { onHighlight(null); dialog.close(); } }, t("Stop highlighting")) : null,
      data.scenes.length ? el("button", { type: "button", onclick: download }, t("Download as CSV")) : null,
      el("button", { type: "button", onclick: () => dialog.close() }, t("Close"))));
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  select("scenes");
  dialog.showModal();
}

// -- Table read ---------------------------------------------------------------------------------
// The browser's speech synthesis reads the script from the cursor: the narrator the scene headings
// and action, each character their dialogue in a voice of their own. Voices are kept per project.
export class TableRead {
  constructor({ view, host, projectId, lang }) {
    Object.assign(this, { view, host, projectId, lang });
    this.queue = [];
    this.playing = false;
    this.bar = null;
    this.key = `dw.voices.${projectId}`;
    this.settings = stored(this.key, { voices: {}, rate: 1, action: true });
  }

  static supported() {
    return typeof window !== "undefined" && "speechSynthesis" in window && "SpeechSynthesisUtterance" in window;
  }

  voices() {
    const all = window.speechSynthesis.getVoices();
    const lang = (this.lang || document.documentElement.lang || "en").slice(0, 2).toLowerCase();
    const mine = all.filter((v) => v.lang.toLowerCase().startsWith(lang));
    return mine.length ? mine : all;
  }

  voiceFor(who) {
    const list = this.voices();
    if (!list.length) return null;
    const wanted = this.settings.voices[who ?? ""];
    const found = list.find((v) => v.voiceURI === wanted);
    if (found) return found;
    // Not chosen yet: the narrator gets the first voice, characters the others in turn.
    if (!who) return list[0];
    const names = [...new Set(readingScript(this.view.state.doc.toString()).map((r) => r.who).filter(Boolean))];
    const i = names.indexOf(who);
    return list[(Math.max(0, i) + 1) % list.length];
  }

  start() {
    if (!TableRead.supported()) {
      toast(t("This browser can't read aloud."), { error: true });
      return;
    }
    this.stop();
    const line = this.view.state.doc.lineAt(this.view.state.selection.main.head).number;
    this.queue = readingScript(this.view.state.doc.toString(), line, { action: this.settings.action });
    if (!this.queue.length) {
      toast(t("Nothing to read from here."));
      return;
    }
    this.playing = true;
    this.showBar();
    this.next();
  }

  next() {
    if (!this.playing) return;
    const item = this.queue.shift();
    if (!item) { this.stop(); return; }
    const u = new SpeechSynthesisUtterance(item.text);
    const voice = this.voiceFor(item.who);
    if (voice) { u.voice = voice; u.lang = voice.lang; } else if (this.lang) u.lang = this.lang;
    u.rate = this.settings.rate;
    u.onstart = () => {
      this.view.dispatch({ effects: [setReading.of([item.line, item.endLine]),
        EditorView.scrollIntoView(this.view.state.doc.line(Math.min(item.line, this.view.state.doc.lines)).from, { y: "center" })] });
      this.status(item.who ?? t("Narrator"));
    };
    u.onend = () => this.next();
    u.onerror = (e) => {
      if (e.error === "interrupted" || e.error === "canceled") return;
      this.next();
    };
    window.speechSynthesis.speak(u);
  }

  pause() {
    if (!this.playing) return;
    if (window.speechSynthesis.paused) window.speechSynthesis.resume();
    else window.speechSynthesis.pause();
    this.bar?.querySelector("[data-pause]")?.setAttribute("aria-pressed", String(window.speechSynthesis.paused));
  }

  stop() {
    this.playing = false;
    this.queue = [];
    if (TableRead.supported()) window.speechSynthesis.cancel();
    this.view.dispatch({ effects: setReading.of(null) });
    this.bar?.remove();
    this.bar = null;
  }

  status(text) {
    const node = this.bar?.querySelector(".table-read-who");
    if (node) node.textContent = text;
  }

  showBar() {
    this.bar?.remove();
    this.bar = el("div", { class: "table-read", role: "toolbar", "aria-label": t("Table read") },
      el("span", { class: "table-read-who", role: "status" }),
      el("button", { type: "button", "data-pause": "", "aria-pressed": "false", onclick: () => this.pause() }, t("Pause")),
      el("button", { type: "button", onclick: () => this.openVoices() }, t("Voices")),
      el("button", { type: "button", onclick: () => this.stop() }, t("Stop")));
    this.host.append(this.bar);
  }

  openVoices() {
    const list = this.voices();
    const names = [...new Set(readingScript(this.view.state.doc.toString()).map((r) => r.who).filter(Boolean))];
    const dialog = el("dialog", { "aria-labelledby": "voices-title" });
    const pick = (who, label) => {
      const id = `voice-${(who || "narrator").replace(/[^a-z0-9]+/gi, "-")}`;
      const current = this.voiceFor(who)?.voiceURI;
      return el("label", { for: id }, label, el("select", { id, "data-who": who },
        list.map((v) => el("option", { value: v.voiceURI, selected: v.voiceURI === current }, `${v.name} (${v.lang})`))));
    };
    const form = el("form", { method: "dialog", class: "form" },
      el("h2", { id: "voices-title" }, t("Voices")),
      list.length ? null : el("p", { class: "muted" }, t("This browser offers no voices.")),
      list.length ? pick("", t("Narrator")) : null,
      ...(list.length ? names.map((n) => pick(n, n)) : []),
      el("label", { class: "choice" }, el("input", { type: "checkbox", name: "action", checked: this.settings.action }), t("Read scene headings and action")),
      el("label", {}, t("Speed"), el("select", { name: "rate" }, [0.8, 1, 1.2, 1.5].map((r) =>
        el("option", { value: String(r), selected: r === this.settings.rate }, `${r}×`)))),
      el("div", { class: "buttons" },
        el("button", { type: "submit", value: "cancel" }, t("Cancel")),
        el("button", { type: "submit", value: "save", class: "primary" }, t("Save"))));
    form.addEventListener("submit", (e) => {
      if (e.submitter?.value !== "save") return;
      for (const s of form.querySelectorAll("select[data-who]")) this.settings.voices[s.dataset.who] = s.value;
      this.settings.action = form.elements.action.checked;
      this.settings.rate = Number(form.elements.rate.value) || 1;
      store(this.key, this.settings);
    });
    dialog.append(form);
    dialog.addEventListener("close", () => dialog.remove());
    document.body.append(dialog);
    dialog.showModal();
  }
}

// -- Daily targets ------------------------------------------------------------------------------
// A target per project and day: words, Normseiten or script pages written today. Each document
// counts from where it stood the first time it was opened today, in this browser.
export const UNITS = { words: "words", normseiten: "Normseiten", pages: "pages" };

export class DailyTarget {
  constructor({ button, projectId, docId, units }) {
    Object.assign(this, { button, projectId, docId, units });
    this.goalKey = `dw.target.${projectId}`;
    this.dayKey = `dw.today.${projectId}`;
    this.counts = {};
    button?.addEventListener("click", () => this.edit());
    this.render();
  }

  get goal() {
    const g = stored(this.goalKey);
    return g && g.daily > 0 && this.units.includes(g.unit) ? g : null;
  }

  // New counts from the stats worker: {words, normseiten, pages}.
  update(counts) {
    this.counts = counts;
    const day = new Date().toLocaleDateString("sv"); // YYYY-MM-DD in local time
    let today = stored(this.dayKey);
    if (!today || today.day !== day) today = { day, docs: {}, reached: false };
    const mine = today.docs[this.docId] ?? { start: { ...counts } };
    mine.now = { ...counts };
    for (const unit of Object.keys(counts)) if (mine.start[unit] === undefined) mine.start[unit] = counts[unit];
    today.docs[this.docId] = mine;
    const goal = this.goal;
    const done = goal ? this.progress(today, goal.unit) : 0;
    if (goal && done >= goal.daily && !today.reached) {
      today.reached = true;
      toast(t("You reached today's target."));
    }
    store(this.dayKey, today);
    this.render();
  }

  progress(today, unit) {
    let n = 0;
    for (const d of Object.values(today.docs)) n += (d.now?.[unit] ?? 0) - (d.start?.[unit] ?? 0);
    return Math.max(0, Math.round(n * 10) / 10);
  }

  label(unit, n) {
    const num = fmtNumber(n);
    if (unit === "normseiten") return t("{n} Normseiten", { n: num });
    if (unit === "pages") return t("{n} pages", { n: num });
    return t("{n} words", { n: num });
  }

  render() {
    if (!this.button) return;
    const goal = this.goal;
    this.button.hidden = !goal;
    if (!goal) return;
    const today = stored(this.dayKey) ?? { docs: {} };
    const done = this.progress(today, goal.unit);
    this.button.textContent = t("Today: {done} of {goal}", { done: fmtNumber(done), goal: this.label(goal.unit, goal.daily) });
    this.button.classList.toggle("target-reached", done >= goal.daily);
    this.button.title = t("Your daily target. Counted in this browser.");
  }

  edit() {
    const goal = this.goal ?? { unit: this.units[0], daily: this.units[0] === "words" ? 1000 : 3 };
    const unitNames = { words: t("Words"), normseiten: t("Normseiten"), pages: t("Script pages") };
    const dialog = el("dialog", { "aria-labelledby": "target-title" });
    const form = el("form", { method: "dialog", class: "form" },
      el("h2", { id: "target-title" }, t("Daily target")),
      el("p", { class: "muted" }, t("How much you want to write each day in this project. Progress is counted from where each document stood when you first opened it today.")),
      el("div", { class: "row" },
        el("label", {}, t("Target"), el("input", { type: "number", name: "daily", min: "0.5", step: "any", required: true, value: String(goal.daily) })),
        el("label", {}, t("Unit"), el("select", { name: "unit" }, this.units.map((u) =>
          el("option", { value: u, selected: u === goal.unit }, unitNames[u]))))),
      el("div", { class: "buttons" },
        this.goal ? el("button", { type: "submit", value: "remove", formnovalidate: true }, t("Remove target")) : null,
        el("button", { type: "submit", value: "cancel", formnovalidate: true }, t("Cancel")),
        el("button", { type: "submit", value: "save", class: "primary" }, t("Save"))));
    form.addEventListener("submit", (e) => {
      const how = e.submitter?.value;
      if (how === "save") {
        const daily = Number(form.elements.daily.value);
        if (daily > 0) store(this.goalKey, { daily, unit: form.elements.unit.value });
      } else if (how === "remove") {
        try { localStorage.removeItem(this.goalKey); } catch { /* ignore */ }
      }
      const today = stored(this.dayKey);
      if (today) { today.reached = false; store(this.dayKey, today); }
      this.render();
    });
    dialog.append(form);
    dialog.addEventListener("close", () => dialog.remove());
    document.body.append(dialog);
    dialog.showModal();
  }
}

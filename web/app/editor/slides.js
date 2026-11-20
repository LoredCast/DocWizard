// Slide decks in the editor (§6.3): the slide sorter, which reorders slides by moving their
// Markdown, and the presenter view, with the current and next slide from the web deck, the
// speaker notes and a timer. An audience window shows the current slide on a second screen.
import { el, t } from "../ui/dom.js";
import { minimalChange } from "./sheet-model.js";
import { moveSlide, parseSlides } from "./slides-model.js";

const KIND_LABEL = { title: "Title", section: "Section", references: "References" };

export class SlideTools {
  /** *getUrl*: the latest URL of the HTML preview, or null; *requestHtml*: asks for a fresh one. */
  constructor({ view, readOnly, options, getUrl, requestHtml }) {
    this.view = view;
    this.readOnly = readOnly;
    this.options = options;
    this.getUrl = getUrl;
    this.requestHtml = requestHtml;
    this.sorter = null;
    this.presenter = null;
  }

  deck() {
    return parseSlides(this.view.state.doc.toString(), this.options());
  }

  docChanged() {
    if (this.sorter) this.renderSorter();
    if (this.presenter) this.updatePresenter();
  }

  rendered(url) {
    if (this.presenter && url) this.showFrames();
  }

  // -- Slide sorter ----------------------------------------------------------
  openSorter() {
    if (this.sorter) return;
    const grid = el("ol", { class: "dw-sorter-grid", "aria-label": t("Slides") });
    const count = el("span", { class: "muted" });
    const close = () => this.closeSorter();
    const root = el("div", { class: "dw-sorter", role: "dialog", "aria-modal": "true", "aria-label": t("Slide sorter") },
      el("div", { class: "dw-sorter-head" },
        el("h2", {}, t("Slides")), count, el("span", { class: "spacer" }),
        el("span", { class: "muted hint" }, this.readOnly ? ""
          : t("Drag a slide, or select it and press Alt+← or Alt+→ to move it.")),
        el("button", { type: "button", onclick: () => { close(); this.openPresenter(); } }, t("Present")),
        el("button", { type: "button", class: "primary", onclick: close }, t("Done"))),
      grid);
    root.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.preventDefault(); close(); }
    });
    document.body.append(root);
    this.sorter = { root, grid, count, focus: null };
    this.renderSorter();
    grid.querySelector("li")?.focus();
  }

  closeSorter() {
    this.sorter?.root.remove();
    this.sorter = null;
    this.view.focus();
  }

  renderSorter() {
    const { grid, count } = this.sorter;
    const deck = this.deck();
    const active = document.activeElement?.closest?.(".dw-slide-card")?.dataset.index;
    count.textContent = t("{n} slides", { n: deck.slides.length });
    grid.replaceChildren(...deck.slides.map((s, i) => this.card(deck, s, i)));
    const again = this.sorter.focus ?? active;
    this.sorter.focus = null;
    if (again != null) grid.querySelector(`[data-index="${again}"]`)?.focus();
  }

  card(deck, s, i) {
    const movable = s.from !== null && !this.readOnly;
    const title = s.title || (KIND_LABEL[s.kind] ? t(KIND_LABEL[s.kind]) : "");
    const li = el("li", {
      class: `dw-slide-card${s.kind === "section" || s.kind === "title" ? " section" : ""}`,
      tabindex: "0", draggable: movable ? "true" : "false", "data-index": String(i),
      "aria-label": t("Slide {n}: {title}", { n: s.n, title: title || t("untitled") }),
    },
    el("span", { class: "n" }, String(s.n)),
    el("strong", {}, title || el("span", { class: "muted" }, t("(no title)"))),
    el("span", { class: "lines" }, s.lines.slice(0, 5).join("\n")),
    s.notes ? el("span", { class: "has-notes", title: s.notes }, t("Notes")) : null);
    li.addEventListener("click", () => this.goTo(s));
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); this.goTo(s); }
      if (!movable || !e.altKey) return;
      const step = { ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1 }[e.key];
      if (!step) return;
      e.preventDefault();
      const to = step < 0 ? i - 1 : i + 2;
      if (this.move(deck, i, to)) this.sorter.focus = String(step < 0 ? i - 1 : i + 1);
    });
    if (movable) {
      li.addEventListener("dragstart", (e) => {
        e.dataTransfer.setData("text/x-dw-slide", String(i));
        e.dataTransfer.effectAllowed = "move";
        li.classList.add("dragging");
      });
      li.addEventListener("dragend", () => li.classList.remove("dragging"));
    }
    li.addEventListener("dragover", (e) => {
      if (!e.dataTransfer.types.includes("text/x-dw-slide")) return;
      e.preventDefault();
      const after = e.offsetX > li.clientWidth / 2;
      li.classList.toggle("drop-after", after);
      li.classList.toggle("drop-before", !after);
    });
    li.addEventListener("dragleave", () => li.classList.remove("drop-before", "drop-after"));
    li.addEventListener("drop", (e) => {
      const from = Number(e.dataTransfer.getData("text/x-dw-slide"));
      const after = li.classList.contains("drop-after");
      li.classList.remove("drop-before", "drop-after");
      if (Number.isNaN(from)) return;
      e.preventDefault();
      this.move(deck, from, after ? i + 1 : i);
    });
    return li;
  }

  /** Moves slide *from* before slide *to* as one edit, which collaborators see and Undo reverts. */
  move(deck, from, to) {
    if (this.readOnly) return false;
    const before = this.view.state.doc.toString();
    const after = moveSlide(before, deck, from, to);
    if (after === null) return false;
    const change = minimalChange(before, after);
    if (!change) return false;
    this.view.dispatch({ changes: change, userEvent: "move.slide" });
    return true;
  }

  goTo(s) {
    this.closeSorter();
    const pos = s.from ?? (s.kind === "references" ? this.view.state.doc.length : 0);
    this.view.dispatch({ selection: { anchor: pos }, scrollIntoView: true });
    this.view.focus();
  }

  // -- Presenter view --------------------------------------------------------
  openPresenter(start = null) {
    if (this.presenter) return;
    const deck = this.deck();
    let index = 0;
    if (start === null) {
      // Start at the slide the cursor is in.
      const pos = this.view.state.selection.main.head;
      deck.slides.forEach((s, i) => { if (s.from !== null && s.from <= pos) index = i; });
    }
    const frame = () => el("iframe", { class: "dw-presenter-frame", sandbox: "", referrerpolicy: "no-referrer",
                                       title: t("Slide"), tabindex: "-1" });
    const now = frame();
    const next = frame();
    next.title = t("Next slide");
    const clock = el("span", { class: "dw-presenter-clock", "aria-label": t("Time") }, "0:00");
    const count = el("span", { class: "dw-presenter-count" });
    const title = el("span", { class: "dw-presenter-title" });
    const notes = el("div", { class: "dw-presenter-notes", "data-empty": t("No notes for this slide."),
                              "aria-live": "polite" });
    const nextTitle = el("span", { class: "dw-presenter-title" });
    const waiting = el("p", { class: "muted", hidden: true }, t("Preparing the slides…"));
    const pauseBtn = el("button", { type: "button", onclick: () => this.toggleClock() }, t("Pause"));
    const root = el("div", { class: "dw-presenter", role: "dialog", "aria-modal": "true", "aria-label": t("Presenter view") },
      el("div", { class: "dw-presenter-head" },
        el("h2", {}, t("Presenter view")), count, clock, pauseBtn,
        el("button", { type: "button", onclick: () => this.resetClock() }, t("Reset")),
        el("span", { class: "spacer" }),
        el("button", { type: "button", onclick: () => this.step(-1), "aria-label": t("Previous slide") }, "←"),
        el("button", { type: "button", onclick: () => this.step(1), "aria-label": t("Next slide") }, "→"),
        el("button", { type: "button", onclick: () => this.openAudience(),
                       title: t("Opens the slides in a window for the projector or a second screen") }, t("Audience window")),
        el("button", { type: "button", class: "primary", onclick: () => this.closePresenter() }, t("End"))),
      el("div", { class: "dw-presenter-body" },
        el("div", { class: "dw-presenter-now" }, waiting, now, title),
        el("div", { class: "dw-presenter-next" }, el("span", { class: "muted" }, t("Next")), next, nextTitle),
        notes));
    const onKey = (e) => {
      if (e.target.closest?.("button") && (e.key === " " || e.key === "Enter")) return;
      const keys = { ArrowRight: 1, ArrowDown: 1, PageDown: 1, " ": 1, n: 1, ArrowLeft: -1, ArrowUp: -1, PageUp: -1, p: -1 };
      if (e.key in keys) { e.preventDefault(); this.step(keys[e.key]); }
      else if (e.key === "Home") { e.preventDefault(); this.show(0); }
      else if (e.key === "End") { e.preventDefault(); this.show(this.presenter.deck.slides.length - 1); }
      else if (e.key === "Escape") { e.preventDefault(); this.closePresenter(); }
    };
    document.addEventListener("keydown", onKey, true);
    document.body.append(root);
    this.presenter = { root, now, next, clock, count, title, notes, nextTitle, waiting, pauseBtn, onKey, deck, index,
                       started: Date.now(), paused: null, audience: null, timer: null };
    this.presenter.timer = setInterval(() => this.tick(), 500);
    root.tabIndex = -1;
    root.focus();
    this.requestHtml();
    this.show(start ?? index);
  }

  closePresenter() {
    const p = this.presenter;
    if (!p) return;
    clearInterval(p.timer);
    clearTimeout(this.htmlTimer);
    document.removeEventListener("keydown", p.onKey, true);
    p.root.remove();
    this.presenter = null;
    this.view.focus();
  }

  updatePresenter() {
    const p = this.presenter;
    p.deck = this.deck();
    this.show(Math.min(p.index, p.deck.slides.length - 1));
    clearTimeout(this.htmlTimer);
    this.htmlTimer = setTimeout(() => this.requestHtml(), 800);
  }

  step(by) {
    this.show(this.presenter.index + by);
  }

  show(index) {
    const p = this.presenter;
    const slides = p.deck.slides;
    p.index = Math.max(0, Math.min(index, slides.length - 1));
    const s = slides[p.index];
    const after = slides[p.index + 1];
    p.count.textContent = t("{n} of {total}", { n: p.index + 1, total: slides.length });
    p.title.textContent = s ? (s.title || (KIND_LABEL[s.kind] ? t(KIND_LABEL[s.kind]) : "")) : "";
    p.nextTitle.textContent = after ? (after.title || (KIND_LABEL[after.kind] ? t(KIND_LABEL[after.kind]) : ""))
      : t("End of the slides");
    p.notes.replaceChildren(...(s?.notes ? s.notes.split("\n").map((line) => el("p", {}, line)) : []));
    this.showFrames();
  }

  showFrames() {
    const p = this.presenter;
    const url = this.getUrl();
    p.waiting.hidden = Boolean(url);
    p.now.hidden = !url;
    if (!url) return;
    const base = url.split("#", 1)[0];
    const total = p.deck.slides.length;
    const at = (n) => `${base}#slide-${n}`;
    this.place(p.now, at(p.index + 1));
    p.next.hidden = p.index + 1 >= total;
    if (p.index + 1 < total) this.place(p.next, at(p.index + 2));
    if (p.audience && !p.audience.closed) {
      try { p.audience.location.replace(at(p.index + 1)); } catch { /* closed meanwhile */ }
    }
  }

  // Same page: only the fragment changes, so the frame scrolls without loading again.
  place(frame, url) {
    if (frame.dataset.url === url) return;
    const sameDoc = frame.dataset.url && frame.dataset.url.split("#", 1)[0] === url.split("#", 1)[0];
    frame.dataset.url = url;
    if (sameDoc) {
      try { frame.contentWindow.location.replace(url); return; } catch { /* fall through */ }
    }
    frame.src = url;
  }

  openAudience() {
    const p = this.presenter;
    const url = this.getUrl();
    if (!url) return;
    p.audience = window.open(`${url.split("#", 1)[0]}#slide-${p.index + 1}`, "dw-audience", "popup,width=1280,height=720");
    this.view.dom.ownerDocument.defaultView.focus();
  }

  tick() {
    const p = this.presenter;
    if (!p) return;
    const ms = (p.paused ?? Date.now()) - p.started;
    const sec = Math.floor(ms / 1000);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = String(sec % 60).padStart(2, "0");
    p.clock.textContent = h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
  }

  toggleClock() {
    const p = this.presenter;
    if (p.paused) {
      p.started += Date.now() - p.paused;
      p.paused = null;
      p.pauseBtn.textContent = t("Pause");
    } else {
      p.paused = Date.now();
      p.pauseBtn.textContent = t("Resume");
    }
    this.tick();
  }

  resetClock() {
    const p = this.presenter;
    p.started = Date.now();
    if (p.paused) p.paused = p.started;
    this.tick();
  }
}

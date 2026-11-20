// Comment threads in a side panel (ARCHITECTURE.md §8.8). Comments live on the server,
// never in the text; their anchors are kept current by the authority and, in this
// editor, by the anchors field.
import { EditorSelection, EditorView } from "codemirror";
import { api, confirmDialog, el, t as tr, toast } from "../ui/dom.js";
import { diffNodes } from "../ui/worddiff.js";
import { AnchorLoader, anchorsField, setActiveThread, setAnchors } from "./anchors.js";

const when = new Intl.DateTimeFormat(document.documentElement.lang || undefined, { dateStyle: "medium", timeStyle: "short" });

function relative(iso) {
  const date = new Date(iso.endsWith("Z") ? iso : `${iso}Z`);
  const secs = (Date.now() - date.getTime()) / 1000;
  if (secs < 60) return tr("just now");
  if (secs < 3600) return tr("{n} min ago", { n: Math.floor(secs / 60) });
  if (secs < 86400) return tr("{n} h ago", { n: Math.floor(secs / 3600) });
  if (secs < 2 * 86400) return tr("yesterday");
  return when.format(date);
}

// @mentions (§8.9): typing @ and a few letters suggests project members.
const MENTION_AT = /(?:^|[^\w@.])@([\w.-]*)$/u;

export function attachMentions(textarea, members) {
  if (!members?.length) return;
  const list = el("ul", { class: "mention-list", role: "listbox", hidden: true });
  textarea.after(list);
  let matches = [];
  let active = 0;
  const close = () => { list.hidden = true; matches = []; };
  const pick = (m) => {
    const before = textarea.value.slice(0, textarea.selectionStart);
    const after = textarea.value.slice(textarea.selectionStart);
    const start = before.lastIndexOf("@");
    textarea.value = `${before.slice(0, start)}@${m.username} ${after}`;
    const pos = start + m.username.length + 2;
    textarea.setSelectionRange(pos, pos);
    textarea.focus();
    close();
  };
  const show = () => {
    list.replaceChildren(...matches.map((m, i) => el("li", {
      role: "option", class: i === active ? "active" : "", "aria-selected": String(i === active),
      onmousedown: (e) => { e.preventDefault(); pick(m); },
    }, el("strong", {}, m.name), " ", el("span", { class: "muted" }, `@${m.username}`))));
    list.hidden = !matches.length;
  };
  textarea.addEventListener("input", () => {
    const m = MENTION_AT.exec(textarea.value.slice(0, textarea.selectionStart));
    if (!m) { close(); return; }
    const q = m[1].toLowerCase();
    matches = members.filter((x) => x.username.toLowerCase().startsWith(q) || x.name.toLowerCase().includes(q)).slice(0, 6);
    active = 0;
    show();
  });
  textarea.addEventListener("keydown", (e) => {
    if (list.hidden) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      active = (active + (e.key === "ArrowDown" ? 1 : matches.length - 1)) % matches.length;
      show();
    } else if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      pick(matches[active]);
    } else if (e.key === "Escape") {
      e.stopPropagation();
      close();
    }
  });
  textarea.addEventListener("blur", () => setTimeout(close, 150));
}

export class CommentsPanel {
  constructor({ session, docId, panel, list, onCount, members = [], isUser = false, selectText = true }) {
    this.session = session;
    this.selectText = selectText; // whether choosing a thread selects its text (not in the review view)
    this.docId = docId;
    this.panel = panel;
    this.list = list;
    this.members = members;
    this.isUser = isUser;
    this.onCount = onCount ?? (() => {});
    this.threads = [];
    this.canComment = false;
    this.canResolve = false;
    this.canAccept = false;
    this.showResolved = false;
    this.draft = null; // {quote}; its range lives in the anchors field, mapped through edits
    this.loader = new AnchorLoader(session, "comment", async () => {
      const data = await api("GET", `/api/documents/${docId}/comments`);
      this.threads = data.threads;
      this.canComment = data.canComment;
      this.canResolve = data.canResolve;
      this.canAccept = data.canAccept;
      this.render();
      return { version: data.version, items: data.threads.filter((t) => !t.resolved && !t.orphaned)
        .map((t) => ({ id: t.id, from: t.from, to: t.to, ...(t.suggestion !== null ? { suggestion: t.suggestion } : {}) })) };
    });
  }

  load() { return this.loader.load(); }
  flush() { this.loader.flush(); }

  get open() { return !this.panel.hidden; }

  show() {
    this.panel.hidden = false;
    this.panel.dispatchEvent(new Event("toggle"));
    this.render();
    this.markRead();
  }

  // Seeing the threads counts as reading their notifications.
  async markRead() {
    if (!this.isUser || !this.threads.some((t) => t.unread)) return;
    try {
      await api("POST", "/api/notifications/read", { doc: this.docId });
    } catch { /* offline: try again next time */ }
  }

  setDraft(range) {
    this.draft = range ? { quote: range.quote, text: range.text, suggest: Boolean(range.suggest) } : null;
    this.session.view?.dispatch({ effects: setAnchors.of({ kind: "draft",
      items: range ? [{ id: 0, from: range.from, to: range.to }] : [] }) });
  }

  // Positions of the draft at the synced version, once this editor's own edits are confirmed.
  async draftAnchor() {
    for (let i = 0; i < 50 && this.session.unconfirmed() > 0; i++) await new Promise((r) => setTimeout(r, 100));
    const view = this.session.view;
    const draft = view.state.field(anchorsField).draft[0];
    if (!draft || draft.to <= draft.from) return null;
    const sel = this.session.toSynced([[draft.from, draft.to]]);
    return sel ? { from: sel[0][0], to: sel[0][1], version: this.session.version() } : null;
  }

  hide() {
    this.panel.hidden = true;
    this.setDraft(null);
    this.panel.dispatchEvent(new Event("toggle"));
    this.session.view?.dispatch({ effects: setActiveThread.of(null) });
  }

  // Start a thread on the current selection; with suggest, one that proposes new text for it (§8.10).
  compose(suggest = false) {
    const view = this.session.view;
    const range = view.state.selection.main;
    if (range.empty) {
      toast(suggest ? tr("Select the text you want to change.") : tr("Select the text you want to comment on."));
      return;
    }
    const text = view.state.sliceDoc(range.from, range.to);
    this.setDraft({ from: range.from, to: range.to, quote: text.slice(0, 200), text, suggest });
    this.show();
    const box = this.list.querySelector("textarea");
    box?.focus();
    if (suggest) box?.select();
  }

  // Show the thread whose mark was clicked.
  focusThread(id) {
    this.show();
    this.select(id, false);
  }

  // Below 900px the panel is a sheet over the lower half of the text, so the passage moves
  // to the top of the text where it stays visible; the thread moves to the top of the sheet.
  select(id, moveCursor = true) {
    const view = this.session.view;
    const sheet = window.matchMedia("(max-width: 900px)").matches;
    const anchor = view.state.field(anchorsField).comment.find((a) => a.id === id);
    const effects = [setActiveThread.of(id)];
    if (anchor && (moveCursor || sheet)) {
      effects.push(EditorView.scrollIntoView(anchor.from, sheet ? { y: "start", yMargin: 32 } : { y: "center" }));
    }
    if (anchor && moveCursor && this.selectText) {
      view.dispatch({ effects, selection: EditorSelection.range(anchor.from, anchor.to) });
    } else {
      view.dispatch({ effects });
    }
    // On touch screens, choosing a thread is reading, not typing: the keyboard goes down so the
    // passage and the thread both stay in view.
    if (moveCursor && window.matchMedia("(pointer: coarse)").matches && view.hasFocus) view.contentDOM.blur();
    for (const node of this.list.querySelectorAll(".thread")) {
      node.classList.toggle("active", node.dataset.thread === String(id));
    }
    this.list.querySelector(`[data-thread="${id}"]`)?.scrollIntoView({ block: sheet ? "start" : "nearest" });
  }

  async send(method, url, body) {
    try {
      const result = await api(method, url, body);
      await this.load();
      return result;
    } catch (err) {
      toast(err.message, { error: true });
      return null;
    }
  }

  render() {
    const open = this.threads.filter((t) => !t.resolved);
    this.onCount(open.length, this.threads.filter((t) => t.unread).length);
    if (!this.open) return;
    const items = [];
    if (this.draft) {
      const suggest = this.draft.suggest;
      const form = el("form", { class: suggest ? "thread draft suggestion" : "thread draft" },
        el("blockquote", {}, this.draft.quote),
        suggest ? el("label", {}, tr("Replace with"),
          el("textarea", { name: "suggestion", rows: 4, "aria-label": tr("Suggested text"), maxlength: 50000 },
            this.draft.text)) : null,
        el("textarea", { name: "body", rows: suggest ? 2 : 3, required: !suggest,
                         "aria-label": suggest ? tr("Why (optional)") : tr("Comment"), maxlength: 10000,
                         placeholder: suggest ? tr("Why (optional)")
                           : (this.members.length ? tr("Comment, or @mention someone") : "") }),
        el("div", { class: "buttons" },
          el("button", { type: "button", onclick: () => { this.setDraft(null); this.render(); } }, tr("Cancel")),
          el("button", { type: "submit", class: "primary" }, suggest ? tr("Suggest") : tr("Comment"))));
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const body = form.elements.body.value.trim();
        const suggestion = suggest ? form.elements.suggestion.value : null;
        if (!suggest && !body) return;
        if (suggest && suggestion === this.draft.text) {
          toast(tr("Change the text first."));
          return;
        }
        const anchor = await this.draftAnchor();
        if (!anchor) {
          toast(tr("The commented text is gone. Select it again."), { error: true });
          return;
        }
        this.setDraft(null);
        await this.send("POST", `/api/documents/${this.docId}/comments`,
                        { body, ...anchor, ...(suggest ? { suggestion } : {}) });
      });
      items.push(form);
      attachMentions(form.elements.body, this.members);
    }
    // Suggest mode makes many small suggestions; editors can decide them all at once.
    const waiting = this.threads.filter((t) => t.suggestion !== null && !t.suggestionState && !t.resolved && !t.orphaned);
    if (waiting.length > 1 && (this.canAccept || this.canResolve)) {
      items.push(el("div", { class: "suggestion-bulk" },
        el("span", {}, tr("{n} suggestions", { n: waiting.length })),
        this.canResolve ? el("button", { type: "button", onclick: () => this.decideAll(waiting, "reject") }, tr("Reject all")) : null,
        this.canAccept ? el("button", { type: "button", class: "primary", onclick: () => this.decideAll(waiting, "accept") }, tr("Accept all")) : null));
    }
    const shown = this.threads.filter((t) => this.showResolved || !t.resolved);
    for (const t of shown) items.push(this.renderThread(t));
    if (!shown.length && !this.draft) {
      items.push(el("p", { class: "muted" },
        this.canComment ? tr("No comments yet. Select text and choose Comment.") : tr("No comments yet.")));
    }
    const resolvedCount = this.threads.length - open.length;
    const toggle = resolvedCount ? el("label", { class: "choice show-resolved" },
      el("input", { type: "checkbox", checked: this.showResolved,
                    onchange: (e) => { this.showResolved = e.target.checked; this.render(); } }),
      tr("Show resolved ({n})", { n: resolvedCount })) : null;
    this.list.replaceChildren(...items, ...(toggle ? [toggle] : []));
  }

  renderThread(t) {
    const node = el("article", { class: `thread${t.resolved ? " resolved" : ""}${t.unread ? " unread" : ""}`,
                                 "data-thread": String(t.id), id: `comment-${t.id}` },
      el("blockquote", { onclick: () => this.select(t.id), class: t.suggestion !== null ? "suggested" : "" },
        t.orphaned ? el("span", { class: "muted" }, tr("The commented text was deleted."))
          : (t.suggestion !== null ? diffNodes(t.quote, t.suggestion, el) : t.quote)));
    if (t.suggestion !== null) {
      const state = { accepted: tr("Accepted"), rejected: tr("Rejected") }[t.suggestionState];
      node.append(el("p", { class: "muted" }, state ? (t.resolvedBy ? tr("{state} by {name}", { state, name: t.resolvedBy }) : state)
        : tr("Suggested by {name}", { name: t.comments[0]?.author ?? "" })));
      if (!t.suggestionState && !t.orphaned) {
        const decide = el("div", { class: "buttons" });
        if (this.canResolve) {
          decide.append(el("button", { type: "button", onclick: () => this.decide(t, "reject") }, tr("Reject")));
        }
        if (this.canAccept) {
          decide.append(el("button", { type: "button", class: "primary", onclick: () => this.decide(t, "accept") }, tr("Accept")));
        }
        if (decide.childElementCount) node.append(decide);
      }
    }
    for (const c of t.comments) {
      if (!c.body) continue;
      node.append(el("div", { class: "comment" },
        el("p", { class: "meta" }, el("strong", {}, c.author), " ",
          el("span", { class: "muted", title: c.createdAt }, relative(c.createdAt)),
          c.updatedAt ? el("span", { class: "muted" }, " (edited)") : null),
        el("p", { class: "body" }, c.body),
        c.mine ? el("div", { class: "row" },
          el("button", { type: "button", class: "link", onclick: () => this.edit(node, c) }, tr("Edit")),
          el("button", { type: "button", class: "link",
                         onclick: () => this.send("DELETE", `/api/documents/${this.docId}/comments/${c.id}`) },
            tr("Delete"))) : null));
    }
    // One row at the end: the reply box, then Resolve or Reopen.
    const foot = el("div", { class: "thread-foot" });
    if (t.resolved) {
      foot.append(el("span", { class: "muted resolved-by" }, t.resolvedBy ? tr("Resolved by {name}", { name: t.resolvedBy }) : tr("Resolved")));
    } else if (this.canComment) {
      const reply = el("form", { class: "reply" },
        el("textarea", { name: "body", rows: 1, placeholder: tr("Reply"), "aria-label": tr("Reply"), maxlength: 10000 }),
        el("button", { type: "submit" }, tr("Reply")));
      reply.addEventListener("submit", async (event) => {
        event.preventDefault();
        const body = reply.elements.body.value.trim();
        if (body) await this.send("POST", `/api/documents/${this.docId}/comments`, { body, threadId: t.id });
      });
      foot.append(reply);
      attachMentions(reply.elements.body, this.members);
    }
    if (this.canResolve && (t.suggestion === null || t.suggestionState || t.orphaned)) {
      foot.append(el("button", { type: "button", onclick: () => this.send("PATCH",
        `/api/documents/${this.docId}/comments/${t.id}`, { resolved: !t.resolved }) },
        t.resolved ? tr("Reopen") : tr("Resolve")));
    }
    if (foot.childElementCount) node.append(foot);
    return node;
  }

  async decide(thread, decision, force = false) {
    try {
      await api("POST", `/api/documents/${this.docId}/comments/${thread.id}/suggestion`, { decision, force });
      await this.load();
    } catch (err) {
      if (err.code === "changed" && !force) {
        if (await confirmDialog(tr("The passage changed since this suggestion was made. Replace it with the suggestion anyway?"), tr("Replace"))) {
          await this.decide(thread, decision, true);
        }
        return;
      }
      toast(err.message, { error: true });
    }
  }

  // Decide every waiting suggestion, last in the text first. Passages that changed since
  // they were suggested are left for a decision one by one.
  async decideAll(threads, decision) {
    const n = threads.length;
    const question = decision === "accept" ? tr("Accept all {n} suggestions? Each one changes the text.", { n })
      : tr("Reject all {n} suggestions?", { n });
    if (!await confirmDialog(question, decision === "accept" ? tr("Accept all") : tr("Reject all"))) return;
    let skipped = 0;
    for (const t of [...threads].sort((a, b) => (b.from ?? 0) - (a.from ?? 0))) {
      try {
        await api("POST", `/api/documents/${this.docId}/comments/${t.id}/suggestion`, { decision, force: false });
      } catch (err) {
        if (err.code !== "decided") skipped++;
      }
    }
    await this.load();
    if (skipped) {
      toast(skipped === 1 ? tr("1 suggestion was left open: its passage changed or it couldn't be decided.")
        : tr("{n} suggestions were left open: their passages changed or they couldn't be decided.", { n: skipped }));
    }
  }

  edit(node, comment) {
    const form = el("form", { class: "reply" },
      el("textarea", { name: "body", rows: 3, "aria-label": tr("Edit comment"), maxlength: 10000 }, comment.body),
      el("button", { type: "submit" }, tr("Save")));
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      await this.send("PATCH", `/api/documents/${this.docId}/comments/${comment.id}`,
                      { body: form.elements.body.value });
    });
    node.append(form);
    form.elements.body.focus();
  }
}

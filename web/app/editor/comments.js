// Comment threads in a side panel (ARCHITECTURE.md §8.8). Comments live on the server,
// never in the text; their anchors are kept current by the authority and, in this
// editor, by the anchors field.
import { EditorSelection } from "codemirror";
import { api, el, t as tr, toast } from "../ui/dom.js";
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

export class CommentsPanel {
  constructor({ session, docId, panel, list, onCount }) {
    this.session = session;
    this.docId = docId;
    this.panel = panel;
    this.list = list;
    this.onCount = onCount ?? (() => {});
    this.threads = [];
    this.canComment = false;
    this.canResolve = false;
    this.showResolved = false;
    this.draft = null; // {quote}; its range lives in the anchors field, mapped through edits
    this.loader = new AnchorLoader(session, "comment", async () => {
      const data = await api("GET", `/api/documents/${docId}/comments`);
      this.threads = data.threads;
      this.canComment = data.canComment;
      this.canResolve = data.canResolve;
      this.render();
      return { version: data.version, items: data.threads.filter((t) => !t.resolved && !t.orphaned) };
    });
  }

  load() { return this.loader.load(); }
  flush() { this.loader.flush(); }

  get open() { return !this.panel.hidden; }

  show() {
    this.panel.hidden = false;
    this.panel.dispatchEvent(new Event("toggle"));
    this.render();
  }

  setDraft(range) {
    this.draft = range ? { quote: range.quote } : null;
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

  // Start a thread on the current selection.
  compose() {
    const view = this.session.view;
    const range = view.state.selection.main;
    if (range.empty) {
      toast(tr("Select the text you want to comment on."));
      return;
    }
    this.setDraft({ from: range.from, to: range.to, quote: view.state.sliceDoc(range.from, range.to).slice(0, 200) });
    this.show();
    this.list.querySelector("textarea")?.focus();
  }

  // Show the thread whose mark was clicked.
  focusThread(id) {
    this.show();
    this.select(id, false);
    this.list.querySelector(`[data-thread="${id}"]`)?.scrollIntoView({ block: "nearest" });
  }

  select(id, moveCursor = true) {
    const view = this.session.view;
    const anchor = view.state.field(anchorsField).comment.find((a) => a.id === id);
    const effects = setActiveThread.of(id);
    if (anchor && moveCursor) {
      view.dispatch({ effects, selection: EditorSelection.range(anchor.from, anchor.to), scrollIntoView: true });
    } else {
      view.dispatch({ effects });
    }
    for (const node of this.list.querySelectorAll(".thread")) {
      node.classList.toggle("active", node.dataset.thread === String(id));
    }
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
    this.onCount(open.length);
    if (!this.open) return;
    const items = [];
    if (this.draft) {
      const form = el("form", { class: "thread draft" },
        el("blockquote", {}, this.draft.quote),
        el("textarea", { name: "body", rows: 3, required: true, "aria-label": tr("Comment"), maxlength: 10000 }),
        el("div", { class: "buttons" },
          el("button", { type: "button", onclick: () => { this.setDraft(null); this.render(); } }, tr("Cancel")),
          el("button", { type: "submit", class: "primary" }, tr("Comment"))));
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const body = form.elements.body.value.trim();
        if (!body) return;
        const anchor = await this.draftAnchor();
        if (!anchor) {
          toast(tr("The commented text is gone. Select it again."), { error: true });
          return;
        }
        this.setDraft(null);
        await this.send("POST", `/api/documents/${this.docId}/comments`, { body, ...anchor });
      });
      items.push(form);
    }
    const shown = this.threads.filter((t) => this.showResolved || !t.resolved);
    for (const t of shown) items.push(this.renderThread(t));
    if (!shown.length && !this.draft) {
      items.push(el("p", { class: "muted" },
        this.canComment ? tr("No comments yet. Select text and choose Comment.") : tr("No comments yet.")));
    }
    const resolvedCount = this.threads.length - open.length;
    const toggle = resolvedCount ? el("label", { class: "choice" },
      el("input", { type: "checkbox", checked: this.showResolved,
                    onchange: (e) => { this.showResolved = e.target.checked; this.render(); } }),
      tr("Show resolved ({n})", { n: resolvedCount })) : null;
    this.list.replaceChildren(...items, ...(toggle ? [toggle] : []));
  }

  renderThread(t) {
    const node = el("article", { class: t.resolved ? "thread resolved" : "thread", "data-thread": String(t.id) },
      el("blockquote", { onclick: () => this.select(t.id) },
        t.orphaned ? el("span", { class: "muted" }, tr("The commented text was deleted.")) : t.quote));
    for (const c of t.comments) {
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
    const actions = el("div", { class: "buttons" });
    if (this.canResolve) {
      actions.append(el("button", { type: "button", onclick: () => this.send("PATCH",
        `/api/documents/${this.docId}/comments/${t.id}`, { resolved: !t.resolved }) },
        t.resolved ? tr("Reopen") : tr("Resolve")));
    }
    if (t.resolved) {
      node.append(el("p", { class: "muted" }, t.resolvedBy ? tr("Resolved by {name}", { name: t.resolvedBy }) : tr("Resolved")));
    } else if (this.canComment) {
      const reply = el("form", { class: "reply" },
        el("textarea", { name: "body", rows: 1, placeholder: tr("Reply"), "aria-label": tr("Reply"), maxlength: 10000 }),
        el("button", { type: "submit" }, tr("Reply")));
      reply.addEventListener("submit", async (event) => {
        event.preventDefault();
        const body = reply.elements.body.value.trim();
        if (body) await this.send("POST", `/api/documents/${this.docId}/comments`, { body, threadId: t.id });
      });
      node.append(reply);
    }
    if (actions.childElementCount) node.append(actions);
    return node;
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

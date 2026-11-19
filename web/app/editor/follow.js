// Follow mode and who is working elsewhere in the project (ARCHITECTURE.md §8.7, §8.12).
// A click on a person in the bar jumps to their cursor and keeps it in view as they move,
// until the reader types, clicks into the text, presses Escape or the person leaves. People
// working in the project's other documents are listed next to them, with a link there.
import { EditorView } from "codemirror";
import { peersField } from "../collab/presence.js";
import { $, api, el, t } from "../ui/dom.js";

const ELSEWHERE_EVERY = 30_000;

// The head of the first selection of any of *ids* (one person may have several tabs open).
export function peerHead(state, ids) {
  const peers = state.field(peersField, false);
  if (!peers) return null;
  for (const id of ids) {
    const p = peers.get(id);
    if (p?.sel?.length) return p.sel[0][1];
  }
  return null;
}

export class Follower {
  constructor({ view, projectId, docId, docUrl }) {
    this.view = view;
    this.projectId = projectId;
    this.docId = docId;
    this.docUrl = docUrl;
    this.target = null; // {key, name, color, ids}
    this.last = null;
    this.people = [];
    this.banner = $("#follow-banner");
    this.slot = $("#presence");
    this.elsewhere = $("#presence-elsewhere");
    this.slot?.addEventListener("click", (event) => {
      const btn = event.target.closest("[data-peer]");
      if (!btn) return;
      const person = this.people.find((p) => p.key === btn.dataset.peer);
      if (!person) return;
      if (this.target?.key === person.key) this.stop();
      else this.follow(person);
    });
    this.banner?.addEventListener("click", (event) => {
      if (event.target.closest("[data-stop-follow]")) this.stop();
    });
    // Taking over the text ends following; scrolling to read along doesn't.
    view.contentDOM.addEventListener("pointerdown", () => this.stop());
    view.contentDOM.addEventListener("keydown", (event) => {
      if (!["Shift", "Control", "Alt", "Meta"].includes(event.key)) this.stop();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && this.target) this.stop();
    });
    this.refreshElsewhere();
    this.timer = setInterval(() => { if (!document.hidden) this.refreshElsewhere(); }, ELSEWHERE_EVERY);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) this.refreshElsewhere(); });
  }

  // The people in this document, one entry per person: [{key, name, color, ids}].
  show(people) {
    this.people = people;
    if (this.target) {
      const still = people.find((p) => p.key === this.target.key);
      if (still) this.target = still;
      else this.lost();
    }
    if (!this.slot) return;
    this.slot.replaceChildren(...people.map((p) => {
      const following = this.target?.key === p.key;
      return el("button", { type: "button", class: "peer", "data-peer": p.key, "aria-pressed": String(following),
                            title: following ? t("Stop following {name}", { name: p.name })
                              : t("Go to {name}'s cursor and follow it", { name: p.name }) },
        el("span", { class: "dot", style: `--peer: var(--peer-${p.color})`, "aria-hidden": "true" }),
        el("span", { class: "name" }, p.name));
    }));
    this.slot.title = "";
  }

  follow(person) {
    this.target = person;
    this.last = null;
    this.render();
    this.show(this.people);
    if (this.jump(true) === null) this.say(t("Waiting for {name} to move their cursor.", { name: person.name }));
  }

  stop() {
    if (!this.target) return;
    this.target = null;
    this.render();
    this.show(this.people);
  }

  // Called after every transaction: the followed cursor may have moved.
  moved() {
    if (!this.target) return;
    queueMicrotask(() => this.jump(false));
  }

  jump(first) {
    if (!this.target) return null;
    const pos = peerHead(this.view.state, this.target.ids);
    if (pos === null) return null;
    if (!first && pos === this.last) return pos;
    this.last = pos;
    this.view.dispatch({ effects: EditorView.scrollIntoView(pos, first ? { y: "center" } : { y: "nearest", yMargin: 80 }) });
    if (first) this.render();
    return pos;
  }

  // The followed person left this document: say where they went, if they are still in the project.
  async lost() {
    const { name } = this.target;
    this.target = null;
    const where = await this.refreshElsewhere();
    const doc = where.find((d) => d.people.some((p) => p.name === name && !p.me));
    if (doc) {
      this.say(t("{name} moved to {path}.", { name, path: doc.path }),
        el("a", { href: this.docUrl(doc.path) }, t("Go there")));
    } else {
      this.say(t("{name} left.", { name }));
    }
  }

  render() {
    if (!this.target) {
      if (this.banner) this.banner.hidden = true;
      return;
    }
    this.say(t("Following {name}.", { name: this.target.name }));
  }

  say(text, ...extra) {
    if (!this.banner) return;
    this.banner.hidden = false;
    this.banner.replaceChildren(el("span", { class: "spacer" }, text), ...extra,
      el("button", { type: "button", class: "link", "data-stop-follow": "" }, this.target ? t("Stop following") : t("Close")));
  }

  async refreshElsewhere() {
    let docs = [];
    try {
      ({ docs } = await api("GET", `/api/projects/${this.projectId}/presence`));
    } catch {
      return [];
    }
    const others = docs.filter((d) => d.docId !== this.docId)
      .map((d) => ({ ...d, people: d.people.filter((p) => !p.me) }))
      .filter((d) => d.people.length);
    if (!this.elsewhere) return others;
    const count = new Set(others.flatMap((d) => d.people.map((p) => p.name))).size;
    this.elsewhere.hidden = count === 0;
    if (!count) {
      this.elsewhere.replaceChildren();
      return others;
    }
    const label = count === 1 ? t("1 in another chapter") : t("{n} in other chapters", { n: count });
    this.elsewhere.replaceChildren(el("details", { class: "menu elsewhere-menu" },
      el("summary", {}, label),
      el("ul", { class: "plain menu-body" }, ...others.map((d) => el("li", {},
        el("a", { href: this.docUrl(d.path) }, d.path), " ",
        el("span", { class: "muted" }, d.people.map((p) => p.name).join(", ")))))));
    return others;
  }
}

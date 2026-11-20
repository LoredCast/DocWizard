// The project's activity in a dialog (ARCHITECTURE.md §8.12): who edited, renamed, commented,
// suggested and synced what, newest first, with a link to each document that still exists.
import { $, api, el, t } from "../ui/dom.js";

const PAGE = 30;

function relative(at) {
  const secs = Date.now() / 1000 - at;
  const lang = document.documentElement.lang || undefined;
  const rtf = new Intl.RelativeTimeFormat(lang, { numeric: "auto" });
  if (secs < 60) return t("just now");
  if (secs < 3600) return rtf.format(-Math.round(secs / 60), "minute");
  if (secs < 86400) return rtf.format(-Math.round(secs / 3600), "hour");
  if (secs < 7 * 86400) return rtf.format(-Math.round(secs / 86400), "day");
  return new Date(at * 1000).toLocaleDateString(lang, { day: "numeric", month: "short", year: "numeric" });
}

export class ActivityDialog {
  constructor({ projectId, docUrl }) {
    this.projectId = projectId;
    this.docUrl = docUrl;
    this.dialog = $("#activity-dialog");
    this.list = $("#activity-items");
    this.more = $("#activity-more");
    this.last = null;
    this.more?.addEventListener("click", () => this.load(false));
  }

  open() {
    if (!this.dialog) return;
    this.dialog.showModal();
    this.load(true);
  }

  async load(fresh) {
    if (fresh) {
      this.last = null;
      this.list.replaceChildren(el("li", { class: "muted" }, t("Loading…")));
    }
    const query = new URLSearchParams({ limit: String(PAGE) });
    if (this.last !== null) query.set("before", String(this.last));
    let data;
    try {
      data = await api("GET", `/api/projects/${this.projectId}/activity?${query}`);
    } catch (err) {
      this.list.replaceChildren(el("li", { class: "error" }, err.message));
      return;
    }
    if (fresh) this.list.replaceChildren();
    for (const item of data.items) {
      const text = item.currentPath && item.kind !== "delete"
        ? el("a", { href: this.docUrl(item.currentPath) }, item.text) : item.text;
      this.list.append(el("li", { class: `activity-item activity-${item.kind}` },
        el("span", { class: "spacer" }, text),
        el("time", { class: "muted", datetime: item.when, title: new Date(item.at * 1000).toLocaleString() }, relative(item.at))));
    }
    if (!this.list.children.length) {
      this.list.append(el("li", { class: "muted" }, t("Nothing has happened yet. Edits, comments, renames and syncs show up here.")));
    }
    this.last = data.items.length ? data.items[data.items.length - 1].id : this.last;
    this.more.hidden = !data.more;
  }
}

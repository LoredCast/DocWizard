// The offline home page (ARCHITECTURE.md §6.5): lists the documents this browser
// keeps, then the rest of the recent list as "Not available offline".
import { getRecent, hasPendingEdits, listDocs } from "./store/idb.js";
import { $, el, wireCommon } from "./ui/dom.js";

wireCommon();

const when = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

const [docs, recent] = await Promise.all([listDocs(), getRecent()]);
const rows = [];
const seen = new Set();
for (const d of docs) {
  seen.add(d.docId);
  const pending = hasPendingEdits(d);
  rows.push(el("tr", { "data-href": d.url },
    el("td", {}, el("a", { href: d.url }, `${d.pinned ? "★ " : ""}${d.meta?.doc?.path ?? d.url}`)),
    el("td", {}, d.meta?.project?.name ?? ""),
    el("td", {}, when.format(new Date(d.updatedAt))),
    el("td", { class: "muted" }, pending ? "Changes waiting" : "")));
}
for (const r of recent?.items ?? []) {
  if (seen.has(r.docId)) continue;
  rows.push(el("tr", {},
    el("td", {}, r.path),
    el("td", {}, r.project),
    el("td", {}, ""),
    el("td", { class: "muted" }, "Not available offline")));
}
if (rows.length) {
  $("#offline-recent tbody").replaceChildren(...rows);
  $("#offline-recent").hidden = false;
} else {
  $("#offline-empty").hidden = false;
}

// Back online: the real home page.
window.addEventListener("online", () => location.reload());

// The offline home page (ARCHITECTURE.md §6.5): projects kept offline, then the documents
// this browser keeps, then the rest of the recent list as "Not available offline".
// At /p/<project> it lists that project's documents instead.
import { getRecent, hasPendingEdits, listDocs, listKeptProjects } from "./store/idb.js";
import { $, el, t, wireCommon } from "./ui/dom.js";

wireCommon();

const when = new Intl.DateTimeFormat(document.documentElement.lang || undefined, { dateStyle: "medium", timeStyle: "short" });
const parts = location.pathname.split("/").filter(Boolean);
const projectId = parts[0] === "p" ? decodeURIComponent(parts[1] ?? "") : null;

const [allDocs, recent, kept] = await Promise.all([listDocs(), getRecent(), listKeptProjects()]);
const docs = projectId ? allDocs.filter((d) => d.meta?.project?.id === projectId) : allDocs;

if (projectId) {
  const name = kept.find((p) => p.projectId === projectId)?.name ?? docs[0]?.meta?.project?.name ?? "";
  if (name) {
    document.title = `${name} · DocWizard`;
    $(".crumbs").append(el("span", {}, "/"), el("span", {}, name));
    $("#offline-heading").textContent = name;
  }
} else if (kept.length) {
  $("#offline-projects ul").replaceChildren(...kept.map((p) => el("li", {},
    el("a", { class: "spacer", href: `/p/${encodeURIComponent(p.projectId)}` }, p.name),
    el("span", { class: "muted" }, p.count === 1 ? t("1 document") : t("{n} documents", { n: p.count ?? 0 })))));
  $("#offline-projects").hidden = false;
}

const rows = [];
const seen = new Set();
for (const d of docs) {
  seen.add(d.docId);
  const pending = hasPendingEdits(d);
  rows.push(el("tr", { "data-href": d.url },
    el("td", {}, el("a", { href: d.url }, `${d.pinned ? "★ " : ""}${d.meta?.doc?.path ?? d.url}`)),
    el("td", {}, d.meta?.project?.name ?? ""),
    el("td", {}, when.format(new Date(d.updatedAt))),
    el("td", { class: "muted" }, pending ? t("Changes waiting") : "")));
}
for (const r of projectId ? [] : recent?.items ?? []) {
  if (seen.has(r.docId)) continue;
  rows.push(el("tr", {},
    el("td", {}, r.path),
    el("td", {}, r.project),
    el("td", {}, ""),
    el("td", { class: "muted" }, t("Not available offline"))));
}
if (rows.length) {
  $("#offline-recent tbody").replaceChildren(...rows);
  $("#offline-recent").hidden = false;
} else {
  $("#offline-empty").hidden = false;
}

// Back online: the real page. The "online" event isn't reliable (captive portals, a server
// that was down), so the shell also asks the server now and then.
const backOnline = async () => {
  try {
    const res = await fetch("/manifest.webmanifest", { cache: "no-store" });
    if (res.ok) location.reload();
  } catch { /* still offline */ }
};
window.addEventListener("online", backOnline);
setInterval(backOnline, 10_000);

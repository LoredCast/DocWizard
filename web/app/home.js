// Organizing the home page (ARCHITECTURE.md §6.2): the filter bar filters and sorts the projects
// and recent documents as people type, with the same rules as the server (pages/projects.py), and
// keeps them in the address; Organize sets a project's tags and archives it, for this person only.
import { $, $$, api, t, toast } from "./ui/dom.js";

const form = $("#home-filter");
const projectsTable = $("#projects-table");

const fold = (s) => String(s ?? "").normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase();

function readFilter() {
  const data = new FormData(form);
  return {
    q: String(data.get("q") ?? ""),
    tag: String(data.get("tag") ?? ""),
    layout: String(data.get("layout") ?? ""),
    shared: data.get("shared") === "1",
    archived: data.get("archived") === "1",
    sort: String(data.get("sort") || "name"),
  };
}

function project(row) {
  let tags = [];
  try { tags = JSON.parse(row.dataset.tags || "[]"); } catch { /* none */ }
  return {
    name: row.dataset.name ?? "", layout: row.dataset.layout ?? "", layoutName: row.dataset.layoutName ?? "",
    owner: row.dataset.owner ?? "", tags, shared: row.hasAttribute("data-shared"),
    archived: row.hasAttribute("data-archived"), updated: row.dataset.updated ?? "",
    members: Number(row.dataset.members ?? 0),
  };
}

// The same rules as project_matches() on the server, with accents ignored as well.
export function matches(p, f, extra = "") {
  if (p.archived !== f.archived) return false;
  if (f.tag && !p.tags.some((tag) => fold(tag) === fold(f.tag))) return false;
  if (f.layout && p.layout !== f.layout) return false;
  if (f.shared && !p.shared) return false;
  const words = fold(f.q).split(/\s+/).filter(Boolean);
  if (!words.length) return true;
  const hay = fold([p.name, ...p.tags, p.layoutName, p.owner, extra].join(" "));
  return words.every((w) => hay.includes(w));
}

export function compare(a, b, sort) {
  const key = sort.replace(/^-/, "");
  const byName = a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
  let d = 0;
  if (key === "layout") d = a.layoutName.localeCompare(b.layoutName, undefined, { sensitivity: "base" });
  else if (key === "updated") d = a.updated < b.updated ? -1 : a.updated > b.updated ? 1 : 0;
  else if (key === "members") d = a.members - b.members;
  d = d || byName;
  return sort.startsWith("-") ? -d : d;
}

function apply({ push = true } = {}) {
  const f = readFilter();
  const rows = $$("tbody tr", projectsTable);
  const byId = new Map(rows.map((row) => [row.dataset.project, project(row)]));
  let shownProjects = 0;
  for (const row of rows) {
    row.hidden = !matches(byId.get(row.dataset.project), f);
    if (!row.hidden) shownProjects += 1;
  }
  const body = projectsTable.tBodies[0];
  const sorted = [...rows].sort((a, b) => compare(byId.get(a.dataset.project), byId.get(b.dataset.project), f.sort));
  if (sorted.some((row, i) => body.rows[i] !== row)) body.append(...sorted);
  for (const th of $$("th[data-sort]", projectsTable)) {
    const key = th.dataset.sort;
    const on = f.sort.replace(/^-/, "") === key;
    const desc = f.sort.startsWith("-");
    th.setAttribute("aria-sort", on ? (desc ? "descending" : "ascending") : "none");
    const link = th.querySelector("a");
    link.textContent = th.dataset.label + (on ? (desc ? " ▾" : " ▴") : "");
  }
  $("#projects-wrap").hidden = shownProjects === 0;
  const none = $("#projects-none");
  none.hidden = shownProjects > 0;
  none.firstChild.textContent = `${f.archived && !rows.some((r) => r.hasAttribute("data-archived"))
    ? t("No archived projects.") : t("No projects match.")} `;
  $("#projects-heading").textContent = f.archived ? t("Archived projects") : t("Projects");

  const recent = $$("#recent-table tbody tr");
  let shownRecent = 0;
  for (const row of recent) {
    const p = byId.get(row.dataset.project);
    row.hidden = !p || !matches(p, f, row.dataset.path);
    if (!row.hidden) shownRecent += 1;
  }
  if ($("#recent-wrap")) {
    $("#recent-wrap").hidden = shownRecent === 0;
    $("#recent-none").hidden = shownRecent > 0;
  }
  const active = Boolean(f.q || f.tag || f.layout || f.shared || f.archived);
  for (const b of $$("button[data-clear-filter]", form)) b.hidden = !active;

  const params = new URLSearchParams();
  for (const [k, v] of Object.entries({ q: f.q, tag: f.tag, layout: f.layout, shared: f.shared ? "1" : "",
                                         archived: f.archived ? "1" : "", sort: f.sort === "name" ? "" : f.sort })) {
    if (v) params.set(k, v);
  }
  const url = params.size ? `/?${params}` : "/";
  if (push && url !== location.pathname + location.search) history.replaceState(null, "", url);
}

function clearFilter() {
  form.q.value = "";
  if (form.tag) form.tag.value = "";
  if (form.layout) form.layout.value = "";
  if (form.shared) form.shared.checked = false;
  if (form.archived) form.archived.checked = false;
  apply();
}

// Keep a project's row in line with its new tags and archive state.
function updateRow(row, { tags, archived }) {
  row.dataset.tags = JSON.stringify(tags);
  row.toggleAttribute("data-archived", archived);
  const slot = row.querySelector(".tags");
  slot.replaceChildren(...tags.flatMap((tag) => [" ", Object.assign(document.createElement("a"), {
    className: "tag", href: `/?tag=${encodeURIComponent(tag)}`, textContent: `#${tag}`,
  })]));
  for (const a of slot.querySelectorAll("a")) a.dataset.tag = a.textContent.slice(1);
  refreshChoices();
}

// The tag and archive choices follow the projects' tags.
function refreshChoices() {
  const rows = $$("tbody tr", projectsTable).map(project);
  const tags = [...new Map(rows.flatMap((p) => p.tags).map((tag) => [fold(tag), tag])).values()]
    .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
  let select = form.tag;
  if (!select && tags.length) {
    select = Object.assign(document.createElement("select"), { name: "tag" });
    select.setAttribute("aria-label", t("Tag"));
    form.q.after(select);
    select.addEventListener("change", () => apply());
  }
  if (select) {
    const current = select.value;
    select.replaceChildren(new Option(t("All tags"), ""), ...tags.map((tag) => new Option(tag, tag)));
    select.value = tags.some((tag) => fold(tag) === fold(current)) ? tags.find((tag) => fold(tag) === fold(current)) : "";
    select.hidden = !tags.length && !select.value;
  }
  const archived = rows.filter((p) => p.archived).length;
  let box = form.archived;
  if (!box && archived) {
    const label = document.createElement("label");
    label.className = "choice";
    box = Object.assign(document.createElement("input"), { type: "checkbox", name: "archived", value: "1" });
    label.append(box, " ", document.createElement("span"));
    form.querySelector('input[name="sort"]').before(label);
    box.addEventListener("change", () => apply());
  }
  if (box) {
    const label = box.closest("label");
    const text = t("Archived ({n})", { n: archived });
    if (label.lastChild.nodeType === Node.TEXT_NODE) label.lastChild.textContent = ` ${text}`;
    else label.lastChild.textContent = text;
    label.hidden = !archived && !box.checked;
  }
}

// -- Organize: tags and archive --------------------------------------------
const dialog = $("#organize-dialog");
const tagsInput = $("#organize-tags");
const picks = $("#organize-picks");
let organizing = null;

function splitTags(value) {
  const out = new Map();
  for (const raw of value.split(",")) {
    const tag = raw.replace(/#/g, " ").replace(/\s+/g, " ").trim();
    if (tag && !out.has(fold(tag))) out.set(fold(tag), tag);
  }
  return [...out.values()];
}

function showPicks() {
  const have = new Set(splitTags(tagsInput.value).map(fold));
  const all = [...new Map($$("tbody tr", projectsTable).flatMap((r) => project(r).tags).map((tag) => [fold(tag), tag])).values()]
    .filter((tag) => !have.has(fold(tag)))
    .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
  picks.replaceChildren(...all.map((tag) => {
    const b = Object.assign(document.createElement("button"), { type: "button", className: "tag-pick", textContent: `+ ${tag}` });
    b.addEventListener("click", () => {
      const list = splitTags(tagsInput.value);
      list.push(tag);
      tagsInput.value = list.join(", ");
      showPicks();
      tagsInput.focus();
    });
    return b;
  }));
  picks.hidden = !all.length;
}

function openOrganize(row) {
  organizing = row;
  const p = project(row);
  $("#organize-title").textContent = t("Organize {name}", { name: p.name });
  const f = $("#organize-form");
  f.action = `/p/${row.dataset.project}`;
  tagsInput.value = p.tags.join(", ");
  $("#organize-archived").checked = p.archived;
  showPicks();
  dialog.showModal();
}

if (form && projectsTable) {
  for (const node of $$("[data-no-js], [data-no-js-form]")) node.hidden = true;
  for (const b of $$("[data-organize]")) b.hidden = false;
  for (const th of $$("th[data-sort]", projectsTable)) {
    th.dataset.label = th.querySelector("a").textContent.replace(/ [▴▾]$/, "");
  }
  form.addEventListener("submit", (event) => { event.preventDefault(); apply(); });
  form.addEventListener("input", (event) => { if (event.target.name === "q") apply(); });
  form.addEventListener("change", (event) => { if (event.target.name !== "q") apply(); });
  for (const b of $$("[data-clear-filter]")) {
    b.addEventListener("click", (event) => { event.preventDefault(); clearFilter(); form.q.focus(); });
  }
  projectsTable.tHead.addEventListener("click", (event) => {
    const th = event.target.closest("th[data-sort]");
    if (!th) return;
    event.preventDefault();
    const key = th.dataset.sort;
    form.sort.value = form.sort.value === key ? `-${key}` : key;
    apply();
  });
  // A tag on a row filters by it.
  document.addEventListener("click", (event) => {
    const tag = event.target.closest("a.tag[data-tag]");
    if (!tag || event.metaKey || event.ctrlKey || event.shiftKey) return;
    event.preventDefault();
    if (form.tag) {
      form.tag.value = [...form.tag.options].find((o) => fold(o.value) === fold(tag.dataset.tag))?.value ?? "";
    }
    apply();
    form.tag?.focus();
  });
  // "/" puts the focus in the filter, as in many lists.
  document.addEventListener("keydown", (event) => {
    if (event.key !== "/" || event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.target.closest?.("input, textarea, select, [contenteditable], dialog")) return;
    event.preventDefault();
    form.q.focus();
  });
  document.addEventListener("click", (event) => {
    const b = event.target.closest("[data-organize]");
    if (b) openOrganize(b.closest("tr"));
  });
  tagsInput.addEventListener("input", showPicks);
  $("#organize-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const row = organizing;
    if (!row) return;
    const submit = event.submitter;
    if (submit) submit.disabled = true;
    try {
      const state = await api("PUT", `/api/projects/${row.dataset.project}/home`, {
        tags: splitTags(tagsInput.value), archived: $("#organize-archived").checked,
      });
      updateRow(row, state);
      dialog.close();
      apply();
      if (state.archived && !readFilter().archived) {
        toast(t("{name} is archived. Find it under Archived.", { name: project(row).name }));
      }
    } catch (err) {
      toast(err.message, { error: true });
    } finally {
      if (submit) submit.disabled = false;
    }
  });
  refreshChoices();
  apply({ push: false });
}

// The layout gallery and latextemplates.com template browser (ARCHITECTURE.md §9.3).
import { $, $$, api, confirmDialog, el, t, toast, wireCommon } from "../ui/dom.js";

wireCommon();

for (const button of $$("[data-new-layout]")) {
  button.addEventListener("click", async () => {
    const from = button.dataset.newLayout;
    const name = button.dataset.name ? t("Copy of {name}", { name: button.dataset.name }) : t("New layout");
    button.disabled = true;
    try {
      const layout = await api("POST", "/api/layouts", { name, extends: from });
      location.href = `/layouts/${encodeURIComponent(layout.id)}`;
    } catch (err) {
      toast(err.message, { error: true });
      button.disabled = false;
    }
  });
}

for (const button of $$("[data-delete-layout]")) {
  button.addEventListener("click", async () => {
    const id = button.dataset.deleteLayout;
    const name = button.dataset.name;
    if (!await confirmDialog(t("Delete the layout “{name}”?", { name }), t("Delete"))) return;
    button.disabled = true;
    try {
      await api("DELETE", `/api/layouts/${encodeURIComponent(id)}`);
      location.reload();
    } catch (err) {
      toast(err.message, { error: true });
      button.disabled = false;
    }
  });
}

async function createProjectFromLayout(button, layoutId, title) {
  const prev = button.textContent;
  button.disabled = true;
  button.textContent = t("Creating project…");
  try {
    const project = await api("POST", "/api/projects", {
      name: (title || "LaTeX Project").slice(0, 120),
      layout: layoutId,
      starter: true,
    });
    const path = project.mainPath
      ? `/${project.mainPath.split("/").map(encodeURIComponent).join("/")}`
      : "";
    location.href = `/p/${encodeURIComponent(project.id)}${path}`;
  } catch (err) {
    toast(err.message, { error: true });
    button.disabled = false;
    button.textContent = prev;
  }
}

async function uninstallTemplate(button, layoutId, name) {
  if (!await confirmDialog(t("Uninstall “{name}”? Projects that already use it keep their files.", { name }),
                           t("Uninstall"))) return;
  button.disabled = true;
  try {
    await api("DELETE", `/api/layouts/catalog/${encodeURIComponent(layoutId)}`);
    toast(t("Uninstalled “{name}”.", { name }));
    removeInstalledCard(layoutId);
    markCatalogItem(layoutId, false, false);
  } catch (err) {
    toast(err.message, { error: true });
    button.disabled = false;
  }
}

document.addEventListener("click", (event) => {
  const useBtn = event.target.closest("[data-use-layout]");
  if (useBtn) {
    createProjectFromLayout(useBtn, useBtn.dataset.useLayout, useBtn.dataset.name);
    return;
  }
  const uninstallBtn = event.target.closest("[data-uninstall-layout]");
  if (uninstallBtn) {
    uninstallTemplate(uninstallBtn, uninstallBtn.dataset.uninstallLayout, uninstallBtn.dataset.name);
  }
});

// -- Library / LaTeX Templates tabs -------------------------------------------
const tabLibrary = $("#tab-library");
const tabCatalog = $("#tab-catalog");
const panelLibrary = $("#panel-library");
const panelCatalog = $("#panel-catalog");
let catalogLoaded = false;

function showTab(which, { focusInput = false } = {}) {
  const isCatalog = which === "catalog";
  tabLibrary?.setAttribute("aria-selected", String(!isCatalog));
  tabCatalog?.setAttribute("aria-selected", String(isCatalog));
  if (panelLibrary) panelLibrary.hidden = isCatalog;
  if (panelCatalog) panelCatalog.hidden = !isCatalog;
  if (isCatalog) {
    if (location.hash !== "#latex-templates") history.replaceState(null, "", "#latex-templates");
    if (!catalogLoaded) loadCatalog({ reset: true });
    if (focusInput) $("#catalog-q")?.focus();
  } else if (location.hash === "#latex-templates") {
    history.replaceState(null, "", location.pathname + location.search);
  }
}

tabLibrary?.addEventListener("click", () => showTab("library"));
tabCatalog?.addEventListener("click", () => showTab("catalog"));
$("#open-latex-browser")?.addEventListener("click", () => showTab("catalog", { focusInput: true }));
$("#jump-catalog")?.addEventListener("click", () => showTab("catalog", { focusInput: true }));
for (const tab of [tabLibrary, tabCatalog].filter(Boolean)) {
  tab.addEventListener("keydown", (event) => {
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault();
      const next = tab === tabLibrary ? tabCatalog : tabLibrary;
      showTab(next === tabCatalog ? "catalog" : "library");
      next.focus();
    }
  });
}

// -- Installed LaTeX templates list (Library tab sync) ------------------------
function buildInstalledCard(layout) {
  const src = layout.source ?? {};
  const engine = layout.engine || "pdflatex";
  return el("li", { class: "layout-card", "data-installed-id": layout.id },
    el("div", { class: "layout-card-main" },
      el("a", { href: src.url || "#", target: "_blank", rel: "noopener noreferrer" }, el("strong", {}, layout.name)),
      " ", el("span", { class: "badge" }, "LaTeX"),
      " ", el("span", { class: "badge muted" }, engine),
      src.category ? [" ", el("span", { class: "badge muted" }, src.category)] : null,
      layout.description ? el("p", { class: "muted" }, layout.description) : null),
    el("div", { class: "row" },
      el("button", { type: "button", class: "primary", "data-use-layout": layout.id, "data-name": layout.name },
         t("New project")),
      src.pdfUrl ? el("a", { class: "button", href: src.pdfUrl, target: "_blank", rel: "noopener noreferrer" },
                      t("Preview PDF")) : null,
      layout.uninstallable
        ? el("button", { type: "button", class: "danger", "data-uninstall-layout": layout.id, "data-name": layout.name },
             t("Uninstall"))
        : null));
}

function addInstalledCard(layout) {
  const section = $("#installed-section");
  const list = $("#installed-list");
  if (!section || !list) return;
  if (list.querySelector(`[data-installed-id="${CSS.escape(layout.id)}"]`)) return;
  list.prepend(buildInstalledCard(layout));
  section.hidden = false;
}

function removeInstalledCard(layoutId) {
  const section = $("#installed-section");
  const list = $("#installed-list");
  if (!section || !list) return;
  list.querySelector(`[data-installed-id="${CSS.escape(layoutId)}"]`)?.remove();
  if (!list.children.length) section.hidden = true;
}

// -- latextemplates.com browser -----------------------------------------------
const filterForm = $("#catalog-filter");
const qInput = $("#catalog-q");
const catSelect = $("#catalog-category");
const engineSelect = $("#catalog-engine");
const statusEl = $("#catalog-status");
const catalogList = $("#catalog-list");
const moreWrap = $("#catalog-more-wrap");
const moreBtn = $("#catalog-more");
const refreshBtn = $("#catalog-refresh");

const PAGE_SIZE = 24;
let seq = 0;
let shown = 0;
let total = 0;
let categoriesFilled = false;
let searchTimer = null;

function renderCatalogCard(item) {
  const badges = [
    el("span", { class: "badge muted" }, item.categoryTitle),
    el("span", { class: "badge muted" }, item.engine),
  ];
  if (item.installed) badges.push(el("span", { class: "badge ok" }, t("Installed")));

  const actions = el("div", { class: "row" });
  if (item.pdfUrl) {
    actions.append(el("a", { class: "button", href: item.pdfUrl, target: "_blank", rel: "noopener noreferrer" },
                      t("Preview PDF")));
  }
  if (item.installed) {
    actions.append(
      el("button", { type: "button", class: "primary", "data-use-layout": item.layoutId, "data-name": item.title },
         t("New project")),
      item.uninstallable
        ? el("button", { type: "button", class: "danger", "data-uninstall-layout": item.layoutId, "data-name": item.title },
             t("Uninstall"))
        : null,
    );
  } else {
    const installBtn = el("button", { type: "button", class: "primary" }, t("Install"));
    installBtn.addEventListener("click", () => installFromCatalog(installBtn, item));
    actions.append(installBtn);
  }

  return el("li", { class: "layout-card", "data-catalog-layout": item.layoutId, "data-catalog-slug": item.slug },
    el("div", { class: "layout-card-main" },
      el("a", { href: item.url, target: "_blank", rel: "noopener noreferrer" }, el("strong", {}, item.title)),
      ...badges.flatMap((b) => [" ", b]),
      item.description ? el("p", { class: "muted" }, item.description) : null),
    actions);
}

const catalogItems = new Map();

function markCatalogItem(layoutId, installed, uninstallable) {
  const item = catalogItems.get(layoutId);
  if (!item) return;
  item.installed = installed;
  item.uninstallable = uninstallable;
  const node = catalogList?.querySelector(`[data-catalog-layout="${CSS.escape(layoutId)}"]`);
  if (node) node.replaceWith(renderCatalogCard(item));
}

async function installFromCatalog(button, item) {
  button.disabled = true;
  button.textContent = t("Installing…");
  try {
    const res = await api("POST", "/api/layouts/catalog/install", { slug: item.slug });
    const layout = res.layout;
    toast(t("Installed “{name}”.", { name: layout.name }));
    addInstalledCard(layout);
    markCatalogItem(item.layoutId, true, Boolean(layout.uninstallable));
  } catch (err) {
    toast(err.message, { error: true });
    button.disabled = false;
    button.textContent = t("Install");
  }
}

function updateCategories(categories) {
  if (!catSelect || categoriesFilled || !categories?.length) return;
  const current = catSelect.value;
  catSelect.replaceChildren(
    el("option", { value: "" }, t("All categories")),
    ...categories.map((c) => el("option", { value: c.slug }, `${c.title} (${c.count})`)),
  );
  catSelect.value = current;
  categoriesFilled = true;
}

async function loadCatalog({ reset = false, refresh = false } = {}) {
  if (!catalogList) return;
  const mine = ++seq;
  if (reset) {
    shown = 0;
    catalogItems.clear();
    catalogList.replaceChildren();
    if (moreWrap) moreWrap.hidden = true;
  }
  if (statusEl) {
    statusEl.className = "muted";
    statusEl.textContent = t("Loading…");
  }
  if (moreBtn) moreBtn.disabled = true;
  const params = new URLSearchParams({
    q: qInput?.value.trim() ?? "",
    category: catSelect?.value ?? "",
    engine: engineSelect?.value ?? "",
    offset: String(shown),
    limit: String(PAGE_SIZE),
  });
  if (refresh) params.set("refresh", "1");
  try {
    const data = await api("GET", `/api/layouts/catalog?${params.toString()}`);
    if (mine !== seq) return;
    catalogLoaded = true;
    if (refresh) categoriesFilled = false;
    updateCategories(data.categories);
    total = data.total ?? 0;
    const frag = document.createDocumentFragment();
    for (const item of data.items ?? []) {
      catalogItems.set(item.layoutId, item);
      frag.append(renderCatalogCard(item));
    }
    if (reset) catalogList.replaceChildren(frag);
    else catalogList.append(frag);
    shown += (data.items ?? []).length;
    if (statusEl) {
      statusEl.className = "muted";
      statusEl.textContent = total === 0
        ? t("No templates match.")
        : shown < total
          ? t("Showing {shown} of {total} templates", { shown, total })
          : (total === 1 ? t("1 template") : t("{n} templates", { n: total }));
    }
    if (moreWrap) moreWrap.hidden = shown >= total;
  } catch (err) {
    if (mine !== seq) return;
    if (statusEl) {
      statusEl.className = "error";
      statusEl.textContent = err.message;
    }
  } finally {
    if (mine === seq && moreBtn) moreBtn.disabled = false;
  }
}

filterForm?.addEventListener("submit", (event) => {
  event.preventDefault();
  clearTimeout(searchTimer);
  loadCatalog({ reset: true });
});
qInput?.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadCatalog({ reset: true }), 250);
});
catSelect?.addEventListener("change", () => loadCatalog({ reset: true }));
engineSelect?.addEventListener("change", () => loadCatalog({ reset: true }));
moreBtn?.addEventListener("click", () => loadCatalog({ reset: false }));
refreshBtn?.addEventListener("click", () => loadCatalog({ reset: true, refresh: true }));

if (location.hash === "#latex-templates") showTab("catalog");

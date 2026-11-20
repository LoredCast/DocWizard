// The layout editor (ARCHITECTURE.md §9.3): a form built from the design schema, a live PDF
// preview of a sample document, and saving as a custom layout. A custom layout stores only the
// settings that differ from the layout it is based on, so it follows that layout's updates.
import { $, $$, api, el, t, toast, wireCommon } from "../ui/dom.js";
import { PdfPreview } from "../preview/pdf.js";

wireCommon();

const form = $("#design-form");
const meta = $("#layout-meta");
const layoutId = form.dataset.layout;
const editable = form.dataset.editable === "true";
const saveButton = $("#save");
const copyButton = $("#save-copy");
const saveStatus = $("#save-status");
const previewStatus = $("#preview-status");
const diagnosticsBox = $("#preview-diagnostics");
const emptyBox = $("#preview-empty");

const LENGTH = /^(?:0|\d{1,3}(?:\.\d{1,3})?(?:mm|cm|in|pt|em))$/;
const LANGS = ["en-US", "en-GB", "de-DE", "de-AT", "de-CH", "fr-FR", "it-IT", "es-ES", "nl-NL"];

let schema = null;
let layout = null;
let values = {};        // the complete design as shown
let base = {};          // the complete design the saved settings are compared with
let options = [];       // options authors can change in a project
let dirty = false;

const pdf = new PdfPreview($("#pdf-container"), {
  onError: (err) => setPreviewStatus(err?.message ?? t("The preview failed."), true),
});

// -- loading -----------------------------------------------------------------
async function load() {
  [schema, layout] = await Promise.all([
    api("GET", "/api/layouts/schema"),
    api("GET", `/api/layouts/${encodeURIComponent(layoutId)}`),
  ]);
  values = structuredClone(layout.design);
  if (editable) {
    base = structuredClone(layout.parentDesign);
    options = layout.own?.options ?? Object.keys(layout.variables ?? {});
  } else {
    // A copy is based on this layout, so its settings are the starting point.
    base = structuredClone(layout.design);
    options = Object.keys(layout.variables ?? {});
  }
  render();
  schedulePreview(0);
}

// -- the form ----------------------------------------------------------------
const fieldsByKey = () => Object.fromEntries(schema.fields.map((f) => [f.key, f]));

function same(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

function changedKeys() {
  return schema.fields.map((f) => f.key).filter((k) => !same(values[k], base[k]));
}

function render() {
  const groups = schema.groups.map((group) => {
    const fields = schema.fields.filter((f) => f.group === group.id);
    const body = el("div", { class: "design-fields" }, fields.map(fieldRow));
    const details = el("details", { class: "design-group", "data-group": group.id },
      el("summary", {}, t(group.label), el("span", { class: "group-count muted" })),
      group.help ? el("p", { class: "muted field-help" }, t(group.help)) : null,
      body);
    return details;
  });
  groups[0].open = true;
  form.replaceChildren(...groups, optionsSection());
  refreshMarks();
}

function setValue(key, value) {
  values[key] = value;
  markDirty();
  refreshMarks();
  schedulePreview();
}

function fieldRow(field) {
  const id = `f-${field.key.replace(/[^a-z0-9]/gi, "-")}`;
  const control = makeControl(field, id);
  const reset = el("button", {
    type: "button", class: "link reset-field", title: t("Use the inherited value"),
    "aria-label": t("Reset {label}", { label: t(field.label) }),
    onclick: () => {
      values[field.key] = structuredClone(base[field.key]);
      markDirty();
      render();
      schedulePreview();
    },
  }, "↺");
  const label = el("label", { for: id, class: "field-label" }, t(field.label));
  return el("div", { class: `design-field type-${field.type}`, "data-key": field.key },
    el("div", { class: "field-head" }, label, el("span", { class: "changed-dot", title: t("Changed") }), reset),
    control,
    field.help ? el("span", { class: "muted field-help" }, t(field.help)) : null);
}

function makeControl(field, id) {
  const value = values[field.key];
  switch (field.type) {
    case "enum":
    case "citation-style": {
      const select = el("select", { id }, Object.entries(field.values).map(([v, label]) =>
        el("option", { value: v, selected: v === value }, t(label))));
      select.addEventListener("change", () => setValue(field.key, select.value));
      return select;
    }
    case "number": {
      const input = el("input", { id, type: "number", min: field.min, max: field.max, step: field.step ?? "any",
                                  value, inputmode: "decimal" });
      input.addEventListener("input", () => {
        const n = Number(input.value);
        const ok = input.value !== "" && Number.isFinite(n) && n >= field.min && n <= field.max;
        input.setAttribute("aria-invalid", ok ? "false" : "true");
        if (ok) setValue(field.key, n);
      });
      return input;
    }
    case "boolean": {
      const box = el("input", { id, type: "checkbox", checked: !!value });
      box.addEventListener("change", () => setValue(field.key, box.checked));
      return box;
    }
    case "length": {
      const input = el("input", { id, value, placeholder: "2.5cm", spellcheck: "false", class: "short" });
      input.addEventListener("input", () => {
        const v = input.value.trim();
        const ok = LENGTH.test(v);
        input.setAttribute("aria-invalid", ok ? "false" : "true");
        if (ok) setValue(field.key, v);
      });
      return input;
    }
    case "font":
      return fontControl(field, id, value);
    case "color": {
      const wrap = el("span", { class: "row tight" });
      const on = el("input", { type: "checkbox", checked: value !== "", "aria-label": t("Use a colour") });
      const pick = el("input", { id, type: "color", value: `#${value || "1a4fa0"}`, disabled: value === "" });
      const update = () => {
        pick.disabled = !on.checked;
        setValue(field.key, on.checked ? pick.value.slice(1) : "");
      };
      on.addEventListener("change", update);
      pick.addEventListener("input", update);
      wrap.append(on, pick);
      return wrap;
    }
    case "lang": {
      const list = el("datalist", { id: `${id}-list` }, LANGS.map((l) => el("option", { value: l })));
      const input = el("input", { id, value, list: `${id}-list`, placeholder: t("from the document"), class: "short" });
      input.addEventListener("input", () => {
        const v = input.value.trim();
        const ok = v === "" || /^[a-z]{2,3}(-[A-Z]{2})?$/.test(v);
        input.setAttribute("aria-invalid", ok ? "false" : "true");
        if (ok) setValue(field.key, v);
      });
      return el("span", {}, input, list);
    }
    case "text": {
      const input = el("input", { id, value, maxlength: 200 });
      input.addEventListener("input", () => setValue(field.key, input.value));
      return input;
    }
    case "longtext": {
      const area = el("textarea", { id, rows: 6, maxlength: 4000 }, value);
      area.addEventListener("input", () => setValue(field.key, area.value));
      return area;
    }
    case "cover":
      return coverEditor(field, id);
    default:
      return el("span", { class: "muted" }, String(value));
  }
}

function fontControl(field, id, value) {
  const fonts = field.fonts ?? {};
  const wrap = el("span", { class: "font-control" });
  const select = el("select", { id });
  if (field.key !== "text.font") select.append(el("option", { value: "" }, t("Same as the body text")));
  for (const [name, label] of Object.entries(fonts)) select.append(el("option", { value: name }, label));
  const known = value === "" || value in fonts;
  select.append(el("option", { value: "__other" }, t("Other font…")));
  select.value = known ? value : "__other";
  const other = el("input", { value: known ? "" : value, placeholder: t("Font name, from the layout's fonts folder"),
                               hidden: known, spellcheck: "false" });
  select.addEventListener("change", () => {
    other.hidden = select.value !== "__other";
    if (select.value !== "__other") setValue(field.key, select.value);
    else other.focus();
  });
  other.addEventListener("input", () => {
    const v = other.value.trim();
    const ok = /^[A-Za-z0-9][A-Za-z0-9 .\-]{0,62}$/.test(v);
    other.setAttribute("aria-invalid", ok ? "false" : "true");
    if (ok) setValue(field.key, v);
  });
  const sample = el("span", { class: "font-sample", style: `font-family: "${value || "inherit"}"` }, "Aa Bb Cc 123");
  wrap.append(select, other, sample);
  return wrap;
}

// The title fields: an ordered list of front matter fields, fixed text and gaps.
function coverEditor(field, id) {
  const items = structuredClone(values[field.key] ?? []);
  const list = el("ol", { class: "cover-list", id });
  const commit = () => { setValue(field.key, structuredClone(items)); draw(); };
  const styleSelect = (item) => {
    const s = el("select", { "aria-label": t("Style") }, Object.entries(schema.coverStyles).map(([v, label]) =>
      el("option", { value: v, selected: (item.style ?? "normal") === v }, t(label))));
    s.addEventListener("change", () => {
      if (s.value === "normal") delete item.style; else item.style = s.value;
      commit();
    });
    return s;
  };
  function draw() {
    list.replaceChildren(...items.map((item, i) => {
      const main = [];
      const sub = [];
      if ("key" in item) {
        const key = el("select", { "aria-label": t("Field") }, Object.entries(schema.coverKeys).map(([k, label]) =>
          el("option", { value: k, selected: item.key === k }, t(label))));
        key.addEventListener("change", () => { item.key = key.value; commit(); });
        const label = el("input", { value: item.label ?? "", placeholder: t("Label before it (optional)"),
                                    maxlength: 80, "aria-label": t("Label") });
        label.addEventListener("change", () => {
          if (label.value.trim()) item.label = label.value.trim(); else delete item.label;
          commit();
        });
        main.push(key);
        sub.push(label, styleSelect(item));
      } else if ("text" in item) {
        const text = el("input", { value: item.text, maxlength: 200, "aria-label": t("Fixed text") });
        text.addEventListener("change", () => { if (text.value.trim()) { item.text = text.value.trim(); commit(); } });
        main.push(el("span", { class: "cover-kind muted" }, t("Text")), text);
        sub.push(styleSelect(item));
      } else {
        const space = el("select", { "aria-label": t("Gap") }, [
          ["0.5em", t("Small gap")], ["1em", t("Gap")], ["2em", t("Large gap")], ["4em", t("Very large gap")],
          ["fill", t("Push apart (fill the page)")],
        ].map(([v, label]) => el("option", { value: v, selected: item.space === v }, label)));
        if (![...space.options].some((o) => o.value === item.space)) {
          space.prepend(el("option", { value: item.space, selected: true }, item.space));
        }
        space.addEventListener("change", () => { item.space = space.value; commit(); });
        main.push(el("span", { class: "cover-kind muted" }, t("Gap")), space);
      }
      const move = (d) => () => {
        const j = i + d;
        if (j < 0 || j >= items.length) return;
        [items[i], items[j]] = [items[j], items[i]];
        commit();
      };
      const buttons = el("span", { class: "cover-buttons" },
        el("button", { type: "button", class: "icon", title: t("Move up"), "aria-label": t("Move up"),
                       disabled: i === 0, onclick: move(-1) }, "↑"),
        el("button", { type: "button", class: "icon", title: t("Move down"), "aria-label": t("Move down"),
                       disabled: i === items.length - 1, onclick: move(1) }, "↓"),
        el("button", { type: "button", class: "icon", title: t("Remove"), "aria-label": t("Remove"),
                       onclick: () => { items.splice(i, 1); commit(); } }, "✕"));
      return el("li", { class: "cover-item" },
        el("div", { class: "cover-main" }, ...main, buttons),
        sub.length ? el("div", { class: "cover-sub" }, ...sub) : null);
    }));
  }
  draw();
  const add = (item) => () => { if (items.length < 30) { items.push(item); commit(); } };
  return el("div", { class: "cover-editor" }, list,
    el("div", { class: "row" },
      el("button", { type: "button", onclick: add({ key: "course" }) }, t("Add field")),
      el("button", { type: "button", onclick: add({ text: t("submitted by") }) }, t("Add text")),
      el("button", { type: "button", onclick: add({ space: "1em" }) }, t("Add gap"))),
    el("p", { class: "muted field-help" }, t("Fields show what the document's front matter says, for example course: or student-id:.")));
}

function optionsSection() {
  const specs = { ...schema.optionSpecs, ...(layout.variables ?? {}) };
  const boxes = Object.entries(specs).map(([name, spec]) => {
    const box = el("input", { type: "checkbox", checked: options.includes(name), value: name });
    box.addEventListener("change", () => {
      options = Object.keys(specs).filter((n) => n === name ? box.checked : options.includes(n));
      markDirty();
      schedulePreview();
    });
    return el("label", { class: "choice" }, box, t(spec.label ?? name));
  });
  return el("details", { class: "design-group", "data-group": "options" },
    el("summary", {}, t("Options for authors")),
    el("p", { class: "muted field-help" }, t("Settings people can change per project under Layout options. Their defaults are the values above.")),
    el("div", { class: "option-list" }, boxes));
}

function refreshMarks() {
  const changed = new Set(changedKeys());
  for (const row of $$(".design-field", form)) row.classList.toggle("changed", changed.has(row.dataset.key));
  for (const group of $$(".design-group", form)) {
    const n = $$(".design-field.changed", group).length;
    const count = $(".group-count", group);
    if (count) count.textContent = n ? ` · ${t("{n} changed", { n })}` : "";
  }
  for (const sample of $$(".font-sample", form)) {
    const key = sample.closest(".design-field").dataset.key;
    sample.style.fontFamily = values[key] ? `"${values[key]}", serif` : "inherit";
  }
}

function markDirty() {
  dirty = true;
  if (saveButton) saveButton.disabled = false;
  saveStatus.textContent = t("Unsaved changes");
}

// -- preview -----------------------------------------------------------------
let previewTimer = null;
let previewSeq = 0;

function setPreviewStatus(text, error = false) {
  previewStatus.textContent = text;
  previewStatus.classList.toggle("error", error);
}

function draftDesign() {
  const out = {};
  for (const key of changedKeys()) out[key] = values[key];
  return out;
}

function draftExtends() {
  return editable ? meta.elements.extends.value : layout.id;
}

function schedulePreview(delay = 700) {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(runPreview, delay);
}

async function runPreview() {
  const seq = ++previewSeq;
  setPreviewStatus(t("Rendering…"));
  try {
    const res = await api("POST", "/api/layouts/preview",
                          { extends: draftExtends(), design: draftDesign(), options });
    if (seq !== previewSeq) return;
    const errors = (res.diagnostics ?? []).filter((d) => d.severity === "error");
    diagnosticsBox.hidden = errors.length === 0;
    diagnosticsBox.textContent = errors.map((d) => d.message).join("\n");
    if (res.url) {
      emptyBox.hidden = true;
      pdf.show(res.url);
      setPreviewStatus("");
    } else {
      setPreviewStatus(t("The preview failed."), true);
    }
  } catch (err) {
    if (seq === previewSeq) setPreviewStatus(err.message, true);
  }
}

$("#refresh-preview").addEventListener("click", () => schedulePreview(0));

// -- saving ------------------------------------------------------------------
function metaBody() {
  const body = {
    name: meta.elements.name.value.trim(),
    description: meta.elements.description.value.trim(),
    design: draftDesign(),
    options,
  };
  if (editable) {
    body.extends = meta.elements.extends.value;
    body.shared = meta.elements.shared?.checked ?? false;
  }
  return body;
}

async function save() {
  const body = metaBody();
  if (!body.name) { meta.elements.name.focus(); return; }
  saveButton.disabled = true;
  saveStatus.textContent = t("Saving…");
  try {
    const saved = await api("PATCH", `/api/layouts/${encodeURIComponent(layoutId)}`, body);
    dirty = false;
    saveStatus.textContent = t("Saved");
    for (const node of $$("[data-layout-title]")) node.textContent = saved.name;
    document.title = `${saved.name} · DocWizard`;
  } catch (err) {
    saveButton.disabled = false;
    saveStatus.textContent = "";
    toast(err.message, { error: true });
  }
}

async function saveCopy() {
  const body = metaBody();
  body.extends = editable ? meta.elements.extends.value : layout.id;
  if (!editable || body.name === layout.name) body.name = t("Copy of {name}", { name: layout.name });
  body.shared = false;
  copyButton.disabled = true;
  try {
    const created = await api("POST", "/api/layouts", body);
    dirty = false;
    location.href = `/layouts/${encodeURIComponent(created.id)}`;
  } catch (err) {
    copyButton.disabled = false;
    toast(err.message, { error: true });
  }
}

saveButton?.addEventListener("click", save);
copyButton?.addEventListener("click", saveCopy);
meta.addEventListener("input", () => { if (editable) markDirty(); });
meta.addEventListener("submit", (event) => event.preventDefault());

// Changing the parent: compare with its design from now on, keeping what is shown.
meta.elements.extends?.addEventListener("change", async () => {
  try {
    const parent = await api("GET", `/api/layouts/${encodeURIComponent(meta.elements.extends.value)}`);
    base = structuredClone(parent.design);
    markDirty();
    refreshMarks();
    schedulePreview(0);
  } catch (err) {
    toast(err.message, { error: true });
  }
});

window.addEventListener("beforeunload", (event) => {
  if (dirty && editable) event.preventDefault();
});
document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "s") {
    event.preventDefault();
    if (editable && saveButton && !saveButton.disabled) save();
  }
});

// Narrow screens show one pane at a time.
const split = $("#layout-split");
for (const tab of $$(".mode-switch [data-mode]")) {
  tab.addEventListener("click", () => {
    for (const other of $$(".mode-switch [data-mode]")) other.setAttribute("aria-selected", String(other === tab));
    split.classList.toggle("show-preview", tab.dataset.mode === "preview");
    if (tab.dataset.mode === "preview") pdf.setVisible?.(true);
  });
}

load().catch((err) => {
  form.replaceChildren(el("p", { class: "error" }, err.message));
});

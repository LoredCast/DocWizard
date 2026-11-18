// Front matter fields (ARCHITECTURE.md §9.4): which fields the project's layout prints, what
// each one means and how to fill it in. The Fields panel lists them with their state in the
// document; inside the front matter, typing a key completes it and hovering a key explains it.
import { EditorSelection, EditorState, hoverTooltip } from "codemirror";
import { el, t } from "../ui/dom.js";
import { fieldState, frontMatterKeys, insertField, keyAt, keyBeingTyped, snippetOf } from "./field-tools.js";

// Front matter sits at the start; a long document needn't be read whole for it.
const HEAD = 20000;
const head = (state) => state.sliceDoc(0, Math.min(state.doc.length, HEAD));

function today() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

// What the editor knows about the layout's fields, from the page's snapshot.
export class LayoutFields {
  constructor(info, { layoutName, titlePage }) {
    this.catalog = info?.catalog ?? {};
    this.shown = (info?.shown ?? []).filter((item) => this.catalog[item.key]);
    this.general = (info?.general ?? []).filter((key) => this.catalog[key]);
    this.ownTemplates = Boolean(info?.ownTemplates);
    this.layoutName = layoutName;
    this.titlePage = titlePage;
    this.fieldsFirst = Boolean(info?.fieldsFirst);
    this.where = new Map(this.shown.map((item) => [item.key, item.where]));
    this.order = this.shown.map((item) => item.key);
  }

  field(key) { return this.catalog[key] ?? null; }

  isShown(key) { return this.where.has(key); }

  // "On the title page, after “Matrikelnummer:”"
  describeWhere(key) {
    const places = this.where.get(key) ?? [];
    const title = key === "title" || key === "subtitle";
    return places.map((p) => {
      const at = {
        cover: this.titlePage() ? t("On the title page")
          : title ? t("At the top of the first page")
            : this.fieldsFirst ? t("Above the title") : t("Under the title"),
        "corner-left": t("Top left of the first page"),
        "corner-right": t("Top right of the first page"),
        header: t("In the running header"),
        "page-number": t("Next to the page numbers"),
        declaration: t("On the declaration page"),
      }[p.at] ?? p.at;
      const how = p.as === "byline" ? t("as “by” and the names")
        : p.as === "place-date" ? t("as “Place, date”")
          : p.as === "surname" ? t("as the surname")
            : p.label ? t("after “{label}”", { label: p.label.trim() }) : "";
      return how ? `${at}, ${how}` : at;
    }).join(" · ");
  }
}

// -- Autocomplete and hover --------------------------------------------------

function completionInfo(fields, f) {
  return () => {
    const where = fields.isShown(f.key) ? fields.describeWhere(f.key)
      : t("{layout} doesn't print this field.", { layout: fields.layoutName });
    return el("div", { class: "field-info" },
      el("p", {}, t(f.help)),
      el("p", { class: "muted" }, where),
      el("pre", {}, f.example));
  };
}

function applySnippet(f) {
  return (view, _completion, from, to) => {
    const { text, cursor } = snippetOf(f, today());
    view.dispatch({ changes: { from, to, insert: text }, selection: EditorSelection.cursor(from + cursor),
                    userEvent: "input.complete" });
  };
}

function completionSource(fields) {
  return (context) => {
    const text = head(context.state);
    if (context.pos > text.length) return null;
    const typing = keyBeingTyped(text, context.pos);
    if (!typing || (!typing.typed && !context.explicit)) return null;
    const keys = frontMatterKeys(text);
    const options = [];
    const add = (key, boost, section) => {
      const f = fields.field(key);
      if (!f || (f.in && !f.topLevel) || keys.top.has(key) || options.some((o) => o.label === key)) return;
      options.push({ label: key, detail: t(f.label), info: completionInfo(fields, f), boost, section,
                     apply: applySnippet(f) });
    };
    const printed = { name: t("Printed by {layout}", { layout: fields.layoutName }), rank: 0 };
    const every = { name: t("For every document"), rank: 1 };
    const others = { name: t("Printed by other layouts"), rank: 2 };
    fields.order.forEach((key) => add(key, 2, printed));
    fields.general.forEach((key) => add(key, 1, every));
    Object.keys(fields.catalog).forEach((key) => add(key, -1, others));
    return { from: typing.from, options, validFor: /^[\w-]*$/ };
  };
}

function hoverSource(fields) {
  return hoverTooltip((view, pos) => {
    const text = head(view.state);
    if (pos > text.length) return null;
    const found = keyAt(text, pos);
    if (!found) return null;
    const f = fields.field(found.key);
    if (!f || (found.nested && !f.in)) return null;
    return {
      pos: found.from, end: found.to, above: true,
      create: () => ({ dom: el("div", { class: "cm-field-tip" },
        el("strong", {}, t(f.label)),
        completionInfo(fields, f)()) }),
    };
  });
}

export function fieldsExtension(fields) {
  // One source for the editor's lifetime: autocomplete tells sources apart by identity.
  const data = [{ autocomplete: completionSource(fields) }];
  return [
    EditorState.languageData.of(() => data),
    hoverSource(fields),
  ];
}

// -- The panel ---------------------------------------------------------------

// Text with the --- marks set as code, so they don't break across lines.
function withCode(text) {
  return text.split("---").flatMap((part, i) => (i ? [el("code", {}, "---"), part] : [part]));
}

export class FieldsPanel {
  constructor({ view, panel, fields, readOnly, onToggle, openOptions, hasOptions, reveal = null }) {
    this.view = view;
    this.panel = panel;
    this.fields = fields;
    this.readOnly = readOnly;
    this.onToggle = onToggle ?? (() => {});
    this.openOptions = openOptions;
    this.hasOptions = hasOptions;
    this.reveal = reveal; // shows a field in the title card, when the editor has one
    this.timer = null;
    this.body = el("div", { class: "panel-body" });
    panel.replaceChildren(
      el("div", { class: "pane-head" },
        el("span", {}, t("Fields")), el("span", { class: "spacer" }),
        el("button", { type: "button", onclick: () => this.hide() }, t("Close"))),
      this.body);
  }

  get open() { return !this.panel.hidden; }

  show() {
    this.panel.hidden = false;
    this.onToggle(true);
    this.render();
    this.panel.querySelector(".panel-body button, .panel-body summary")?.focus({ preventScroll: true });
  }

  hide() {
    this.panel.hidden = true;
    this.onToggle(false);
    this.view.focus();
  }

  toggle() { return this.open ? this.hide() : this.show(); }

  // The states change as the front matter is edited.
  docChanged() {
    if (!this.open) return;
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.render(), 250);
  }

  goTo(pos, key = null) {
    if (key && this.reveal?.(key)) return;
    this.view.dispatch({ selection: EditorSelection.cursor(pos), scrollIntoView: true });
    this.view.focus();
  }

  add(f) {
    const change = insertField(this.view.state.doc.toString(), f, { order: this.fields.order, today: today() });
    if (!change) return;
    this.view.dispatch({ changes: change.changes, selection: EditorSelection.cursor(change.cursor),
                         scrollIntoView: true, userEvent: "input" });
    if (this.reveal?.(f.in && !f.topLevel ? f.in : f.key)) return;
    this.view.focus();
  }

  item(key, keys, { shown }) {
    const f = this.fields.field(key);
    const state = fieldState(keys, f);
    const badge = {
      filled: el("span", { class: "badge field-filled" }, t("filled in")),
      empty: el("span", { class: "badge muted" }, t("empty")),
      missing: f.auto ? el("span", { class: "badge muted" }, t("automatic")) : null,
    }[state.state];
    let action = null;
    if (state.at !== null) {
      action = el("button", { type: "button", class: "link", onclick: () => this.goTo(state.at, f.in && !f.topLevel ? f.in : key) },
        state.state === "missing" ? t("Go to author") : t("Go to"));
    } else if (!this.readOnly) {
      action = el("button", { type: "button", onclick: () => this.add(f) }, t("Add"));
    }
    const where = shown ? this.fields.describeWhere(key) : null;
    return el("li", { class: `field-item field-is-${state.state}` },
      el("div", { class: "field-line" },
        el("code", { class: "field-key" }, f.in && !f.topLevel ? `${f.in}: ${key}` : key),
        el("span", { class: "field-label" }, t(f.label)), badge, el("span", { class: "spacer" }), action),
      el("p", { class: "field-help" }, t(f.help)),
      where ? el("p", { class: "field-where muted" }, where) : null,
      el("details", { class: "field-example" },
        el("summary", {}, t("Example")), el("pre", {}, f.example)));
  }

  render() {
    if (!this.open) return;
    const keys = frontMatterKeys(this.view.state.doc.toString());
    const fields = this.fields;
    const list = (items, opts) => el("ul", { class: "plain field-list" }, items.map((key) => this.item(key, keys, opts)));
    const parts = [
      el("p", { class: "muted field-intro" }, ...withCode(this.reveal
        ? t("Fill these in at the top of the document, in its title card. Go to or Add takes you there. In the YAML view they are the lines between --- at the very top, one “name: value” per line.")
        : t("Fields go in the front matter: the lines between --- at the very top of the document, one “name: value” per line. Typing a name there suggests the fields; hovering one explains it."))),
    ];
    if (fields.ownTemplates) {
      parts.push(el("p", { class: "muted" },
        t("{layout} uses templates of its own. Its README lists the fields it prints.", { layout: fields.layoutName })));
    } else {
      parts.push(el("h3", { class: "refs-heading" }, t("Printed by {layout}", { layout: fields.layoutName })),
        list(fields.order, { shown: true }));
    }
    // Fields in the document that this layout leaves out, say after switching layouts.
    const unused = Object.keys(fields.catalog).filter((key) => !fields.isShown(key) && !fields.general.includes(key)
      && fieldState(keys, fields.field(key)).state !== "missing");
    if (unused.length && !fields.ownTemplates) {
      parts.push(el("h3", { class: "refs-heading" }, t("Not printed by {layout}", { layout: fields.layoutName })),
        el("p", { class: "muted field-note" }, t("These fields are in the document, but this layout doesn't print them. Other layouts do.")),
        list(unused, { shown: false }));
    }
    parts.push(el("details", { class: "field-general" },
      el("summary", {}, t("For every document")),
      list(fields.general, { shown: false })));
    if (this.hasOptions) {
      parts.push(el("p", { class: "muted field-note" },
        t("Settings for the whole project, such as line spacing or the title page, are layout options."), " ",
        el("button", { type: "button", class: "link", onclick: () => this.openOptions() }, t("Layout options"))));
    }
    this.body.replaceChildren(...parts);
  }
}

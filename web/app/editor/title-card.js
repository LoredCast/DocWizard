// The title card (ARCHITECTURE.md §6.3, §9.4): in the editor, the front matter at the top of a
// document shows as its title, subtitle, author and date to type into, with the other fields
// under "More details", instead of YAML. The YAML stays the document's text, so every change in
// the card is an ordinary edit, and the "YAML" switch shows it as it is.
import { Decoration, EditorView, StateEffect, StateField, WidgetType } from "codemirror";
import { el, t } from "../ui/dom.js";
import { frontMatter } from "./cite-tools.js";
import { authorValue, authorsOf, readFields, setField } from "./front-matter-edit.js";

const HEAD = 20000;
const MAIN = ["title", "subtitle", "author", "date"];
const ISO = /^\d{4}-\d{2}-\d{2}$/;

export const showYamlEffect = StateEffect.define(); // true: the YAML; false: the card
export const refreshCard = StateEffect.define();

const head = (state) => state.sliceDoc(0, Math.min(state.doc.length, HEAD));

class TitleCard extends WidgetType {
  constructor(info, card) {
    super();
    this.info = info;
    this.card = card;
    this.key = JSON.stringify([info.fields, card.locked()]);
  }

  eq(other) { return other.key === this.key; }

  toDOM(view) {
    const dom = el("div", { class: "cm-title-card", role: "group", "aria-label": t("Document details") });
    this.card.render(dom, view, this.info);
    return dom;
  }

  updateDOM(dom, view) {
    this.card.render(dom, view, this.info);
    return true;
  }

  ignoreEvent() { return true; }

  get estimatedHeight() { return 160; }
}

class YamlBar extends WidgetType {
  constructor(card) { super(); this.card = card; }
  eq() { return true; }
  toDOM(view) {
    return el("div", { class: "cm-title-yaml-bar" },
      el("span", { class: "muted" }, t("Document details as YAML")),
      el("button", { type: "button", class: "link", onmousedown: (e) => e.preventDefault(),
                     onclick: () => this.card.setYaml(view, false) }, t("Show as a title card")));
  }
  ignoreEvent() { return true; }
}

export class TitleCardEditor {
  // fields: the layout's field catalog (LayoutFields), for labels; locked(state): no typing
  // (read-only, or suggesting); onMore(): opens the Fields panel; store: remembers the YAML switch.
  constructor({ fields = null, locked = () => false, onMore = null, yaml = false, onYaml = null } = {}) {
    this.fields = fields;
    this.isLocked = locked;
    this.onMore = onMore;
    this.onYaml = onYaml;
    this.startYaml = yaml;
    this.view = null;
    this.moreOpen = false;
    const self = this;
    this.field = StateField.define({
      create: (state) => ({ yaml: self.startYaml, deco: self.decorations(state, self.startYaml) }),
      update(value, tr) {
        let yaml = value.yaml;
        let refresh = false;
        for (const e of tr.effects) {
          if (e.is(showYamlEffect)) yaml = e.value;
          if (e.is(refreshCard)) refresh = true;
        }
        if (yaml === value.yaml && !tr.docChanged && !refresh) return value;
        return { yaml, deco: self.decorations(tr.state, yaml) };
      },
      provide: (f) => [
        EditorView.decorations.from(f, (v) => v.deco),
        EditorView.atomicRanges.of((view) => view.state.field(f).deco),
      ],
    });
  }

  extension() {
    return [this.field, EditorView.updateListener.of((u) => { this.view = u.view; })];
  }

  locked() { return this.state ? this.isLocked(this.state) : false; }

  decorations(state, yaml) {
    this.state = state;
    const text = head(state);
    const fm = frontMatter(text);
    if (!fm) return Decoration.none;
    if (yaml) return Decoration.set([Decoration.widget({ widget: new YamlBar(this), block: true, side: -1 }).range(0)]);
    const info = readFields(text);
    const end = fm.to - (text.slice(0, fm.to).endsWith("\r\n") ? 2 : text.slice(0, fm.to).endsWith("\n") ? 1 : 0);
    return Decoration.set([Decoration.replace({ widget: new TitleCard(info, this), block: true }).range(0, end)]);
  }

  yamlShown(state) { return state.field(this.field, false)?.yaml ?? false; }

  setYaml(view, yaml) {
    view.dispatch({ effects: showYamlEffect.of(yaml) });
    this.onYaml?.(yaml);
    if (yaml) {
      const fm = frontMatter(head(view.state));
      if (fm) view.dispatch({ selection: { anchor: Math.min(fm.bodyFrom, view.state.doc.length) }, scrollIntoView: true });
    }
    view.focus();
  }

  // Shows a field: its input in the card, or the YAML for fields the card can't edit.
  // Returns false when there is nothing to show (no front matter).
  reveal(view, key) {
    if (!frontMatter(head(view.state))) return false;
    if (this.yamlShown(view.state)) return false;
    const info = readFields(head(view.state));
    const f = info.fields.find((x) => x.key === key);
    if (f && f.kind === "complex") {
      this.setYaml(view, true);
      return false;
    }
    if (!MAIN.includes(key)) this.moreOpen = true;
    view.dispatch({ effects: refreshCard.of(null) });
    requestAnimationFrame(() => {
      const dom = view.dom.querySelector(".cm-title-card");
      const input = dom?.querySelector(`[data-key="${CSS.escape(key)}"]`);
      if (dom && !input && !f) {
        // A field the document doesn't have yet: add it, then type into it.
        this.write(view, key, "", { keepEmpty: true });
        requestAnimationFrame(() => view.dom.querySelector(`.cm-title-card [data-key="${CSS.escape(key)}"]`)?.focus());
        return;
      }
      const more = input?.closest(".tc-more");
      if (more) more.open = true;
      input?.scrollIntoView({ block: "nearest" });
      input?.focus();
    });
    return true;
  }

  label(key) {
    const f = this.fields?.field(key);
    if (f) return t(f.label);
    const words = key.replace(/[-_]+/g, " ");
    return words.charAt(0).toUpperCase() + words.slice(1);
  }

  write(view, key, value, { keepEmpty = false } = {}) {
    const text = head(view.state);
    const change = setField(text, key, value, { keep: keepEmpty ? ["title", key] : ["title"] });
    if (!change) return;
    view.dispatch({ changes: change, userEvent: "input.type" });
  }

  // Builds the card once, then only updates values, so typing in it never loses focus.
  render(dom, view, info) {
    this.view = view;
    const locked = this.locked();
    const byKey = new Map(info.fields.map((f) => [f.key, f]));
    const others = info.fields.filter((f) => !MAIN.includes(f.key));
    const shape = JSON.stringify([others.map((f) => [f.key, f.kind]), byKey.has("abstract"), locked]);
    if (dom.dataset.shape !== shape) {
      dom.dataset.shape = shape;
      dom.replaceChildren(...this.build(view, info, others, locked));
    }
    for (const input of dom.querySelectorAll("[data-key]")) {
      if (input === document.activeElement) continue;
      const key = input.dataset.key;
      const f = byKey.get(key);
      const value = key === "author" ? authorsOf(f) : f?.kind === "list" ? f.value.join(", ") : (f?.value ?? "");
      if (input.type === "date" && value && !ISO.test(value)) input.type = "text";
      if (input.value !== value) input.value = value;
      if (input.tagName === "TEXTAREA") input.rows = Math.min(12, Math.max(2, value.split("\n").length));
    }
  }

  input(view, key, { cls, placeholder, type = "text", multiline = false, label = null, locked = false, list = false }) {
    const value = (v) => (key === "author" ? authorValue(v)
      : list ? v.split(",").map((s) => s.trim()).filter(Boolean) : v);
    const attrs = {
      class: cls, "data-key": key, placeholder, disabled: locked, spellcheck: type === "text" ? "true" : "false",
      "aria-label": label ?? placeholder, title: label && label !== placeholder ? label : undefined,
      oninput: (e) => this.write(view, key, value(e.target.value), { keepEmpty: true }),
      onchange: (e) => {
        // An emptied subtitle goes, so the layout doesn't print an empty line.
        if (key === "subtitle" && !e.target.value.trim()) this.write(view, key, "");
      },
      onkeydown: (e) => {
        if (e.key === "Enter" && !multiline) {
          e.preventDefault();
          this.leave(view);
        } else if (e.key === "Escape") {
          e.preventDefault();
          this.leave(view);
        }
      },
    };
    return multiline ? el("textarea", { ...attrs, rows: 2 }) : el("input", { ...attrs, type });
  }

  // Back to the text, just below the card.
  leave(view) {
    const fm = frontMatter(head(view.state));
    const pos = fm ? Math.min(view.state.doc.length, fm.to) : 0;
    view.dispatch({ selection: { anchor: pos }, scrollIntoView: true });
    view.focus();
  }

  build(view, info, others, locked) {
    const byKey = new Map(info.fields.map((f) => [f.key, f]));
    const date = byKey.get("date");
    const dateType = !date || !date.value || ISO.test(date.value) ? "date" : "text";
    const author = byKey.get("author");
    const authorComplex = author?.kind === "complex";
    const parts = [
      this.input(view, "title", { cls: "tc-title", placeholder: t("Title"), locked }),
      this.input(view, "subtitle", { cls: "tc-subtitle", placeholder: t("Subtitle (optional)"), locked }),
      el("div", { class: "tc-byline" },
        authorComplex
          ? el("button", { type: "button", class: "link tc-complex", onclick: () => this.setYaml(view, true) },
            t("Authors: edit as YAML"))
          : this.input(view, "author", { cls: "tc-author", placeholder: t("Author"), locked,
                                         label: t("Author (several: separate with ;)") }),
        this.input(view, "date", { cls: "tc-date", placeholder: t("Date"), type: dateType, locked })),
    ];
    if (byKey.has("abstract")) {
      const abstract = byKey.get("abstract");
      parts.push(el("label", { class: "tc-abstract" }, el("span", { class: "tc-label" }, this.label("abstract")),
        abstract.kind === "complex" ? el("span", { class: "muted" }, t("Set in the YAML"))
          : this.input(view, "abstract", { cls: "tc-abstract-text", placeholder: t("Abstract"), multiline: true, locked })));
    }
    const rest = others.filter((f) => f.key !== "abstract");
    const details = el("details", { class: "tc-more", open: this.moreOpen || undefined,
                                    ontoggle: (e) => { this.moreOpen = e.target.open; } },
      el("summary", {}, rest.length ? t("More details ({n})", { n: rest.length }) : t("More details")),
      el("div", { class: "tc-fields" }, rest.map((f) => {
        const id = `tc-${f.key}`;
        return el("div", { class: "tc-field" },
          el("label", { for: id }, this.label(f.key)),
          f.kind === "complex" || f.kind === "block"
            ? el("button", { type: "button", class: "link", id, onclick: () => this.setYaml(view, true) }, t("Edit as YAML"))
            : Object.assign(this.input(view, f.key, { cls: "tc-input", placeholder: "", locked, label: this.label(f.key),
                                                     list: f.kind === "list" }),
              { id }));
      })),
      el("div", { class: "tc-actions" },
        this.onMore ? el("button", { type: "button", class: "link", onclick: () => this.onMore() }, t("Add a detail…")) : null));
    parts.push(el("div", { class: "tc-foot" }, details,
      el("button", { type: "button", class: "link tc-yaml", title: t("Show the document details as YAML, as they are in the file"),
                     onclick: () => this.setYaml(view, true) }, "YAML")));
    return parts;
  }
}

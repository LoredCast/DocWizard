// The Chapters drawer (ARCHITECTURE.md §6.3): the project's documents without the project page.
// Chapters are the build inputs, in order; dragging one by its handle (or the arrow keys on it) reorders
// the book. Other documents can join it, documents can be renamed, deleted and added, and the
// pictures and bibliography are listed under "Other files". Everything goes through the project
// and document APIs the project page uses.
import { api, confirmDialog, el, t, toast } from "../ui/dom.js";

const docUrl = (projectId, path) => `/p/${projectId}/${path.split("/").map(encodeURIComponent).join("/")}`;
const isMarkdown = (path) => /\.(md|markdown)$/i.test(path);

function parts(path) {
  const slash = path.lastIndexOf("/");
  const dir = slash < 0 ? "" : path.slice(0, slash + 1);
  const file = path.slice(slash + 1);
  const dot = file.lastIndexOf(".");
  return { dir, stem: dot > 0 ? file.slice(0, dot) : file, ext: dot > 0 ? file.slice(dot) : "" };
}

// A file name from a title, as the starters name chapters: lower case, ASCII, dashes.
export function slugify(title) {
  const map = { ä: "ae", ö: "oe", ü: "ue", ß: "ss" };
  return title.toLowerCase().replace(/[äöüß]/g, (c) => map[c]).normalize("NFKD")
    .replace(/[̀-ͯ]/g, "").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60) || "chapter";
}

// Where a new chapter goes: next to the last chapter, numbered after it when chapters are numbered
// (chapters/02-background.md is followed by chapters/03-<title>.md).
export function newChapterPath(chapters, currentPath, title, taken) {
  const last = chapters.length ? chapters[chapters.length - 1] : currentPath;
  const { dir } = parts(last ?? "");
  let prefix = "";
  // Numbered after every numbered file in that folder, in the book or not.
  const numbered = [...chapters, ...taken].filter((p) => parts(p).dir === dir && isMarkdown(p))
    .map((p) => /^(\d+)-/.exec(parts(p).stem)).filter(Boolean);
  if (numbered.length) {
    const width = numbered[numbered.length - 1][1].length;
    const next = Math.max(...numbered.map((m) => Number(m[1]))) + 1;
    prefix = `${String(next).padStart(width, "0")}-`;
  }
  const base = `${dir}${prefix}${slugify(title)}`;
  let path = `${base}.md`;
  for (let n = 2; taken.has(path); n++) path = `${base}-${n}.md`;
  return path;
}

export class ChaptersDrawer {
  constructor({ host, projectId, docId, docPath, canEdit, canBuild, offline, files = [], settle, onBuild, openReferences }) {
    Object.assign(this, { host, projectId, docId, docPath, canEdit, canBuild, offline, settle, onBuild, openReferences });
    this.tree = files.map((f) => ({ ...f }));
    this.build = [];
    this.expanded = null; // the path whose actions are showing
    this.renaming = null;
    this.panel = el("aside", { class: "chapters-drawer", id: "chapters", "aria-label": t("Chapters and files"), hidden: true });
    this.panel.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !e.defaultPrevented) { e.preventDefault(); this.hide(true); }
    });
    host.append(this.panel);
    document.addEventListener("pointerdown", (e) => {
      if (!this.open || this.panel.contains(e.target) || e.target.closest?.("[data-open-chapters]")) return;
      this.hide();
    });
  }

  get open() { return !this.panel.hidden; }

  toggle(from) { return this.open ? this.hide() : this.show(from); }

  async show(from = null) {
    this.returnFocus = from ?? document.activeElement;
    this.panel.hidden = false;
    for (const b of document.querySelectorAll("[data-open-chapters]")) b.setAttribute("aria-expanded", "true");
    this.render();
    this.panel.querySelector(".ch-row.current a, .pane-head button")?.focus();
    await this.load();
  }

  hide(restore = false) {
    if (!this.open) return;
    this.panel.hidden = true;
    this.expanded = null;
    this.renaming = null;
    for (const b of document.querySelectorAll("[data-open-chapters]")) b.setAttribute("aria-expanded", "false");
    if (restore) this.returnFocus?.focus?.();
  }

  async load() {
    if (this.offline) return;
    try {
      const project = await api("GET", `/api/projects/${this.projectId}`);
      this.tree = project.tree;
      this.build = Array.isArray(project.buildInputs) ? project.buildInputs : [];
      this.render();
    } catch (err) {
      if (err.status) toast(err.message, { error: true });
    }
  }

  get docs() { return this.tree.filter((f) => f.type === "document"); }

  // The chapters, in order: build inputs that are Markdown documents. A LaTeX root is no book.
  get chapters() {
    if (this.build.some((p) => p.endsWith(".tex"))) return [];
    const have = new Set(this.docs.map((d) => d.path));
    return this.build.filter((p) => have.has(p) && isMarkdown(p));
  }

  render() {
    const chapters = this.chapters;
    const inBook = new Set(chapters);
    const others = this.docs.filter((d) => !inBook.has(d.path));
    const assets = this.tree.filter((f) => f.type === "asset");
    const refs = others.filter((d) => /\.(json|bib|yaml|yml)$/i.test(d.path) && !isMarkdown(d.path));
    const writing = others.filter((d) => !refs.includes(d));
    const canBook = this.canBuild && !this.offline;
    const body = el("div", { class: "panel-body" });

    if (chapters.length) {
      body.append(
        el("p", { class: "hint muted" }, canBook
          ? t("Previewed and exported together, in this order. Drag a chapter to move it.")
          : t("Previewed and exported together, in this order.")),
        this.list(chapters.map((path) => this.docs.find((d) => d.path === path)), { book: true }));
    } else {
      body.append(el("p", { class: "hint muted" }, t("Each document is previewed and exported on its own.")));
    }
    if (this.canEdit && !this.offline) body.append(this.newForm(chapters));
    if (writing.length) {
      body.append(el("h3", {}, chapters.length ? t("Not in the book") : t("Documents")));
      body.append(this.list(writing, { book: false }));
      const md = writing.filter((d) => isMarkdown(d.path));
      if (canBook && !chapters.length && md.length > 1) {
        body.append(el("p", { class: "hint" }, el("button", { type: "button", class: "link", onclick: () => this.combine(md) },
          t("Combine these into one book")), " ", el("span", { class: "muted" }, t("(previewed and exported together)"))));
      }
    }
    const extra = [...refs.map((d) => ({ ...d, ref: true })), ...assets];
    if (extra.length) {
      const details = el("details", { class: "ch-files" }, el("summary", {}, t("Other files ({n})", { n: extra.length })),
        el("ul", { class: "plain" }, extra.map((f) => this.fileRow(f))));
      details.open = this.filesOpen ?? false;
      details.addEventListener("toggle", () => { this.filesOpen = details.open; });
      body.append(details);
    }
    this.panel.replaceChildren(
      el("div", { class: "pane-head" }, el("span", {}, t("Chapters")), el("span", { class: "spacer" }),
         el("button", { type: "button", onclick: () => this.hide(true) }, t("Close"))),
      body);
  }

  list(docs, { book }) {
    const ol = el(book ? "ol" : "ul", { class: `plain ch-list${book ? " ch-book" : ""}` });
    for (const doc of docs) ol.append(this.row(doc, { book }));
    return ol;
  }

  row(doc, { book }) {
    const { dir, stem, ext } = parts(doc.path);
    const current = doc.path === this.docPath;
    const li = el("li", { class: `ch-row${current ? " current" : ""}`, "data-path": doc.path });
    const canBook = this.canBuild && !this.offline;
    if (book && canBook) {
      const handle = el("button", { type: "button", class: "ch-handle", "aria-label": t("Move {name} (arrow keys)", { name: stem }),
                                    title: t("Drag to move, or use the arrow keys") }, "⋮⋮");
      handle.addEventListener("pointerdown", (e) => this.drag(e, li));
      handle.addEventListener("keydown", (e) => {
        if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
        e.preventDefault();
        this.move(doc.path, e.key === "ArrowUp" ? -1 : 1);
      });
      li.append(handle);
    }
    if (this.renaming === doc.path) {
      const input = el("input", { value: stem, "aria-label": t("New name"), spellcheck: "false" });
      const form = el("form", { class: "ch-rename", onsubmit: (e) => { e.preventDefault(); this.rename(doc, `${dir}${input.value.trim().replace(/[\\/]+/g, "-")}${ext}`); } },
        input, el("button", { type: "submit", class: "primary" }, t("Rename")),
        el("button", { type: "button", onclick: () => { this.renaming = null; this.render(); } }, t("Cancel")));
      input.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.preventDefault(); this.renaming = null; this.render(); } });
      li.append(form);
      queueMicrotask(() => { input.focus(); input.select(); });
      return li;
    }
    li.append(el("a", { class: "ch-name", href: docUrl(this.projectId, doc.path), "aria-current": current ? "page" : null },
      el("span", { class: "ch-title" }, stem), dir || (ext && !isMarkdown(doc.path)) ? el("span", { class: "muted ch-dir" }, dir + (isMarkdown(doc.path) ? "" : ext)) : null));
    if (this.canEdit && !this.offline) {
      const open = this.expanded === doc.path;
      li.append(el("button", { type: "button", class: "ch-more", "aria-expanded": String(open), "aria-label": t("More for {name}", { name: stem }),
        onclick: () => { this.expanded = open ? null : doc.path; this.render(); this.panel.querySelector(`[data-path="${CSS.escape(doc.path)}"] .ch-more`)?.focus(); } }, "⋯"));
      if (open) {
        li.append(el("div", { class: "ch-actions" },
          el("button", { type: "button", class: "link", onclick: () => { this.renaming = doc.path; this.render(); } }, t("Rename")),
          canBook && isMarkdown(doc.path) ? el("button", { type: "button", class: "link", onclick: () => (book ? this.leave(doc.path) : this.join(doc.path)) },
            book ? t("Leave out of the book") : t("Add to the book")) : null,
          el("button", { type: "button", class: "link danger-text", onclick: () => this.remove(doc) }, t("Delete"))));
      }
    }
    return li;
  }

  fileRow(f) {
    const href = `/api/projects/${this.projectId}/assets/${f.path.split("/").map(encodeURIComponent).join("/")}`;
    if (f.ref) {
      return el("li", { class: "ch-file" }, el("a", { href: docUrl(this.projectId, f.path) }, f.path),
        this.openReferences ? el("button", { type: "button", class: "link", onclick: () => { this.hide(); this.openReferences(); } }, t("Manage in References")) : null);
    }
    return el("li", { class: "ch-file" }, el("a", { href, target: "_blank", rel: "noopener" }, f.path));
  }

  newForm(chapters) {
    const input = el("input", { placeholder: chapters.length ? t("Title of the new chapter") : t("Title of the new document"),
                                "aria-label": t("Title"), maxlength: "120" });
    const form = el("form", { class: "ch-new", onsubmit: async (e) => {
      e.preventDefault();
      const title = input.value.trim();
      if (!title) { input.focus(); return; }
      form.querySelector("button").disabled = true;
      await this.create(title, chapters);
      form.querySelector("button").disabled = false;
    } }, input, el("button", { type: "submit" }, chapters.length ? t("Add chapter") : t("New document")));
    return form;
  }

  async saveBuild(next) {
    const before = this.build;
    this.build = next;
    this.render();
    try {
      await api("PATCH", `/api/projects/${this.projectId}`, { buildInputs: next });
      this.onBuild?.();
    } catch (err) {
      this.build = before;
      this.render();
      toast(err.message, { error: true });
    }
  }

  // The build list with the chapters in a new order, other entries where they were.
  withOrder(order) {
    const chapters = new Set(this.chapters);
    const out = [];
    let i = 0;
    for (const p of this.build) {
      if (chapters.has(p)) { if (i < order.length) out.push(order[i++]); } else if (this.docs.some((d) => d.path === p)) out.push(p);
    }
    return [...out, ...order.slice(i)];
  }

  move(path, by) {
    const order = [...this.chapters];
    const i = order.indexOf(path);
    const j = i + by;
    if (i < 0 || j < 0 || j >= order.length) return;
    [order[i], order[j]] = [order[j], order[i]];
    this.saveBuild(this.withOrder(order)).then(() => {
      this.panel.querySelector(`[data-path="${CSS.escape(path)}"] .ch-handle`)?.focus();
    });
  }

  // Dragging by the handle, with a mouse or a finger: the row follows, and drops between the others.
  drag(event, li) {
    if (event.button !== 0) return;
    event.preventDefault();
    const list = li.parentElement;
    const handle = event.currentTarget;
    handle.setPointerCapture(event.pointerId);
    const startY = event.clientY;
    const startTop = li.getBoundingClientRect().top;
    const start = [...list.children].indexOf(li);
    li.classList.add("dragging");
    const onMove = (e) => {
      li.style.transform = "";
      const rows = [...list.children].filter((r) => r !== li);
      const before = rows.find((r) => {
        const box = r.getBoundingClientRect();
        return e.clientY < box.top + box.height / 2;
      });
      if ((before ?? null) !== li.nextElementSibling) list.insertBefore(li, before ?? null);
      // The row stays under the pointer wherever it now sits in the list.
      li.style.transform = `translateY(${e.clientY - startY - (li.getBoundingClientRect().top - startTop)}px)`;
    };
    const onUp = () => {
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", onUp);
      handle.removeEventListener("pointercancel", onUp);
      li.classList.remove("dragging");
      li.style.transform = "";
      const order = [...list.children].map((r) => r.dataset.path);
      if ([...list.children].indexOf(li) !== start) this.saveBuild(this.withOrder(order));
    };
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
    handle.addEventListener("pointercancel", onUp);
  }

  join(path) {
    this.expanded = null;
    const base = this.chapters.length ? this.build : [];
    return this.saveBuild([...base, path]);
  }

  leave(path) {
    this.expanded = null;
    return this.saveBuild(this.build.filter((p) => p !== path));
  }

  // Several documents become one book: the one with the front matter first (main.md, book.md,
  // the open one when nothing says), then the others by name.
  combine(md) {
    const first = md.find((d) => /(^|\/)(main|index|book|thesis|novel|paper)\.md$/i.test(d.path)) ?? md.find((d) => d.path === this.docPath) ?? md[0];
    const rest = md.filter((d) => d !== first).map((d) => d.path).sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
    return this.saveBuild([first.path, ...rest]);
  }

  async create(title, chapters) {
    const taken = new Set(this.tree.map((f) => f.path));
    const path = newChapterPath(chapters, isMarkdown(this.docPath) ? this.docPath : null, title, taken);
    try {
      await api("POST", `/api/projects/${this.projectId}/documents`, { path, text: `# ${title}\n\n` });
      if (chapters.length && this.canBuild) await api("PATCH", `/api/projects/${this.projectId}`, { buildInputs: [...this.withOrder(chapters), path] });
      await this.settle?.();
      location.href = docUrl(this.projectId, path);
    } catch (err) {
      toast(err.message, { error: true });
    }
  }

  async rename(doc, path) {
    if (path === doc.path || !parts(path).stem) { this.renaming = null; this.render(); return; }
    try {
      if (doc.id === this.docId) await this.settle?.();
      const result = await api("PATCH", `/api/documents/${doc.id}`, { path });
      if (doc.id === this.docId) { location.href = docUrl(this.projectId, result.path); return; }
      this.renaming = null;
      this.expanded = null;
      await this.load();
      this.onBuild?.();
    } catch (err) {
      toast(err.message, { error: true });
    }
  }

  async remove(doc) {
    if (!(await confirmDialog(t("Move {name} to the trash?", { name: doc.path })))) return;
    try {
      if (doc.id === this.docId) await this.settle?.();
      await api("DELETE", `/api/documents/${doc.id}`);
      if (this.chapters.includes(doc.path)) {
        await api("PATCH", `/api/projects/${this.projectId}`, { buildInputs: this.build.filter((p) => p !== doc.path) }).catch(() => {});
      }
      if (doc.id === this.docId) {
        const next = this.chapters.find((p) => p !== doc.path) ?? this.docs.find((d) => d.id !== doc.id && isMarkdown(d.path))?.path;
        location.href = next ? docUrl(this.projectId, next) : `/p/${this.projectId}`;
        return;
      }
      this.expanded = null;
      await this.load();
      this.onBuild?.();
    } catch (err) {
      toast(err.message, { error: true });
    }
  }
}

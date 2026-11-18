// Images pasted, dropped or picked into the editor (ARCHITECTURE.md §6.3): each one is uploaded to
// figures/ in the project, and a figure with a caption to fill in and a label to cite takes its place.
// While the upload runs, a placeholder holds the spot, and it moves with the text around it.
import { Decoration, EditorSelection, EditorView, StateEffect, StateField, WidgetType } from "codemirror";
import { api, t, toast } from "../ui/dom.js";

// What Typst and pandoc's HTML take; LaTeX documents take PNG, JPEG and PDF only.
const MARKDOWN_TYPES = { "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
                         "image/svg+xml": "svg" };
const LATEX_TYPES = { "image/png": "png", "image/jpeg": "jpg", "application/pdf": "pdf" };
const GENERIC = /^(image|images|blob|clipboard|screenshot|pasted|unnamed|untitled|figure)$/;
const FOLDER = "figures";

class Placeholder extends WidgetType {
  constructor(id, name) {
    super();
    this.id = id;
    this.name = name;
  }
  eq(other) { return other.id === this.id; }
  toDOM() {
    const span = document.createElement("span");
    span.className = "cm-image-upload";
    span.textContent = t("Uploading {name}…", { name: this.name });
    return span;
  }
  ignoreEvent() { return true; }
}

const addPending = StateEffect.define();
const removePending = StateEffect.define();

const pendingField = StateField.define({
  create: () => Decoration.none,
  update(set, tr) {
    let out = set.map(tr.changes);
    for (const e of tr.effects) {
      if (e.is(addPending)) {
        const deco = Decoration.widget({ widget: new Placeholder(e.value.id, e.value.name), side: 1, id: e.value.id });
        out = out.update({ add: [deco.range(e.value.pos)] });
      } else if (e.is(removePending)) {
        out = out.update({ filter: (_f, _t, d) => d.spec.id !== e.value });
      }
    }
    return out;
  },
  provide: (f) => EditorView.decorations.from(f),
});

function pendingPos(state, id) {
  let pos = null;
  state.field(pendingField).between(0, state.doc.length, (from, _to, d) => {
    if (d.spec.id === id) { pos = from; return false; }
    return undefined;
  });
  return pos;
}

export function slug(name) {
  const stem = name.replace(/\.[^.]*$/, "").replace(/ß/g, "ss").normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 48);
  return !stem || GENERIC.test(stem) || /^image-?\d*$/.test(stem) ? "figure" : stem;
}

// The text a figure becomes. Markdown: pandoc's implicit figure, with an id crossref.lua numbers
// and [@fig:…] cites. LaTeX: a figure environment.
export function figureText(kind, path, label) {
  if (kind === "latex") {
    return `\\begin{figure}[htbp]\n  \\centering\n  \\includegraphics[width=\\linewidth]{${path}}\n`
      + `  \\caption{${t("Caption")}}\n  \\label{fig:${label}}\n\\end{figure}`;
  }
  return `![${t("Caption")}](${path}){#fig:${label}}`;
}

// The figure stands as a paragraph of its own: blank lines around it, unless they're there already.
export function placeFigure(state, pos, text) {
  const line = state.doc.lineAt(pos);
  let at = pos;
  // Inside a line of text, the figure goes after that line.
  if (line.text.trim() && pos > line.from && pos < line.to) at = line.to;
  const before = state.doc.sliceString(Math.max(0, at - 2), at);
  const after = state.doc.sliceString(at, Math.min(state.doc.length, at + 2));
  const lead = at === 0 || before === "\n\n" ? "" : before.endsWith("\n") ? "\n" : "\n\n";
  const tail = at === state.doc.length ? "\n"
    : after === "\n\n" || (after === "\n" && at + 1 === state.doc.length) ? ""
    : after.startsWith("\n") ? "\n" : "\n\n";
  return { at, insert: lead + text + tail, offset: lead.length };
}

export class ImageInserter {
  constructor({ projectId, kind, onInserted } = {}) {
    this.projectId = projectId;
    this.kind = kind;
    this.types = kind === "latex" ? LATEX_TYPES : MARKDOWN_TYPES;
    this.onInserted = onInserted ?? null;
    this.seq = 0;
  }

  accepts(file) {
    return Boolean(file && this.types[file.type]);
  }

  extension() {
    const self = this;
    return [pendingField, EditorView.domEventHandlers({
      paste(event, view) {
        const data = event.clipboardData;
        if (!data || view.state.readOnly) return false;
        // Text wins: word processors put a picture of the copied text on the clipboard too.
        if (data.getData("text/plain").trim()) return false;
        const files = [...data.files].filter((f) => self.accepts(f));
        if (!files.length) return false;
        event.preventDefault();
        self.insert(view, files, view.state.selection.main.head);
        return true;
      },
      drop(event, view) {
        const files = [...(event.dataTransfer?.files ?? [])];
        if (!files.length || view.state.readOnly) return false;
        const images = files.filter((f) => self.accepts(f));
        if (!images.length) return false; // other files: CodeMirror inserts their text
        event.preventDefault();
        const pos = view.posAtCoords({ x: event.clientX, y: event.clientY }) ?? view.state.selection.main.head;
        self.insert(view, images, pos);
        return true;
      },
    })];
  }

  // Opens the file picker (the toolbar's image button, and phones, where pasting files is rare).
  pick(view) {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = Object.keys(this.types).join(",");
    input.multiple = true;
    input.addEventListener("change", () => {
      const files = [...input.files].filter((f) => this.accepts(f));
      if (files.length) this.insert(view, files, view.state.selection.main.head);
    }, { once: true });
    input.click();
  }

  async insert(view, files, pos) {
    for (const file of files) {
      const id = `img${++this.seq}`;
      const name = file.name && !/^image\.\w+$/i.test(file.name) ? file.name : t("image");
      view.dispatch({ effects: addPending.of({ id, pos, name }) });
      let path = null;
      try {
        const ext = this.types[file.type];
        const want = `${FOLDER}/${slug(file.name || "")}.${ext}`;
        const url = `/api/projects/${this.projectId}/assets/${want.split("/").map(encodeURIComponent).join("/")}?unique=1`;
        path = (await api("PUT", url, file, { raw: true })).path;
      } catch (err) {
        toast(navigator.onLine === false ? t("Images can be added while online.")
          : t("The image couldn't be uploaded: {message}", { message: err.message }), { error: true });
      }
      const at = pendingPos(view.state, id);
      if (path === null || at === null) {
        view.dispatch({ effects: removePending.of(id) });
        continue;
      }
      const label = path.split("/").pop().replace(/\.[^.]*$/, "");
      const text = figureText(this.kind, path, label);
      const place = placeFigure(view.state, at, text);
      // Select the caption, ready to be typed over.
      const caption = text.indexOf(t("Caption"));
      const start = place.at + place.offset + caption;
      view.dispatch({
        changes: { from: place.at, insert: place.insert },
        effects: removePending.of(id),
        selection: EditorSelection.range(start, start + t("Caption").length),
        scrollIntoView: true,
        userEvent: "input.paste",
      });
      pos = start + text.length - caption;
      this.onInserted?.(path);
    }
    view.focus();
  }
}

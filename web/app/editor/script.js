// Screenplays (§9.3): character names to complete, and the outline of acts, scenes and headings.
import { EditorSelection, EditorState, EditorView } from "codemirror";
import { el, t } from "../ui/dom.js";

// A character cue is a line in capitals after a blank line. Offer the names already used,
// most frequent first; the stats worker finds them (workers/stats.js).
export function characterCompletion(names) {
  const source = (context) => {
    const line = context.state.doc.lineAt(context.pos);
    const typed = line.text.slice(0, context.pos - line.from);
    if (line.text.length !== typed.length) return null; // only at the end of the line
    if (line.number > 1 && context.state.doc.line(line.number - 1).text.trim() !== "") return null;
    if (!/^@?[\p{Lu}\p{N}][\p{Lu}\p{N} .'’-]*$/u.test(typed) && !(context.explicit && typed === "")) return null;
    const prefix = typed.replace(/^@/, "");
    const options = names().filter((n) => n !== prefix && n.toLocaleUpperCase().startsWith(prefix.toLocaleUpperCase()))
      .map((n, i) => ({ label: n, boost: -i, detail: t("character") }));
    if (!options.length) return null;
    return { from: line.from + (typed.startsWith("@") ? 1 : 0), options, validFor: /^[\p{Lu}\p{N} .'’-]*$/u };
  };
  const data = [{ autocomplete: source }];
  return EditorState.languageData.of(() => data);
}

// The outline: a dialog listing headings (and scenes in a screenplay); choosing one goes there.
export function openOutline(view, outline, { script = false } = {}) {
  const dialog = el("dialog", { "aria-labelledby": "outline-title", class: "outline-dialog" });
  const go = (line) => {
    const pos = view.state.doc.line(Math.min(line, view.state.doc.lines)).from;
    view.dispatch({ selection: EditorSelection.cursor(pos), effects: EditorView.scrollIntoView(pos, { y: "start" }) });
    dialog.close();
    view.focus();
  };
  const here = view.state.doc.lineAt(view.state.selection.main.head).number;
  let current = null;
  for (const h of outline) if (h.line <= here) current = h;
  let sceneNo = 0;
  const items = outline.map((h) => {
    if (h.scene) sceneNo += 1;
    const button = el("button", { type: "button", class: "link", onclick: () => go(h.line) },
      h.scene ? el("span", { class: "outline-no" }, `${sceneNo}`) : "", h.title || t("(untitled)"));
    if (h === current) button.setAttribute("aria-current", "location");
    return el("li", { class: `outline-level-${Math.min(h.level, 4)}` }, button);
  });
  dialog.append(
    el("h2", { id: "outline-title" }, t("Outline")),
    items.length ? el("ol", { class: "outline-list" }, ...items)
      : el("p", { class: "muted" }, script ? t("No scenes yet. A scene starts with a line like INT. KITCHEN - DAY.")
        : t("No headings yet. Lines that start with # are headings.")),
    el("div", { class: "buttons" }, el("button", { type: "button", onclick: () => dialog.close() }, t("Close"))));
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
  dialog.querySelector("[aria-current]")?.focus();
}

// Pasting formatted text (ARCHITECTURE.md §6.3): Word, Google Docs and web pages put HTML on the
// clipboard, which becomes Markdown with its headings, emphasis, lists, links and tables. A small
// note offers to paste it as plain text instead, as Google Docs does; Ctrl+Shift+V pastes plain
// text right away.
import { EditorSelection, EditorView, syntaxTree } from "codemirror";
import { el, t, toast } from "../ui/dom.js";
import { htmlToMarkdown } from "./paste-html.js";

// Where pasted text stays as it is: code, math, the front matter and raw HTML.
const LITERAL = new Set(["FencedCode", "CodeBlock", "InlineCode", "FrontMatter", "InlineMath", "DisplayMath",
                         "HTMLBlock", "CommentBlock"]);

function inLiteral(state, pos) {
  for (let node = syntaxTree(state).resolveInner(pos, -1); node; node = node.parent) {
    if (LITERAL.has(node.name)) return true;
  }
  return false;
}

// Blocks pasted into the middle of a paragraph get blank lines around them.
function placeBlocks(state, from, to, text) {
  if (!/\n\n|^(#{1,6} |[-*] |\d+\. |> |\| |```)/.test(text)) return text;
  const before = state.sliceDoc(state.doc.lineAt(from).from, from);
  const after = state.sliceDoc(to, state.doc.lineAt(to).to);
  return (before.trim() ? "\n\n" : "") + text + (after.trim() ? "\n\n" : "");
}

export class RichPaste {
  constructor({ parse = (html) => new DOMParser().parseFromString(html, "text/html").body } = {}) {
    this.parse = parse;
    this.plainNext = 0; // a time until which pastes are plain: Ctrl+Shift+V was pressed
    this.note = null;
    this.last = null; // { from, to, plain } of the last formatted paste, until the next edit
  }

  extension() {
    const self = this;
    return [
      EditorView.domEventHandlers({
        keydown(event) {
          if ((event.ctrlKey || event.metaKey) && event.shiftKey && (event.key === "V" || event.key === "v")) {
            self.plainNext = Date.now() + 1000;
          }
          return false;
        },
        paste(event, view) {
          return self.paste(event, view);
        },
      }),
      EditorView.updateListener.of((update) => {
        if (this.last && update.docChanged && !update.transactions.some((tr) => tr.isUserEvent("input.paste.rich"))) {
          this.forget();
        }
      }),
    ];
  }

  paste(event, view) {
    const data = event.clipboardData;
    if (!data || view.state.readOnly || Date.now() < this.plainNext) return false;
    const html = data.getData("text/html");
    if (!html) return false;
    const range = view.state.selection.main;
    if (view.state.selection.ranges.length > 1 || inLiteral(view.state, range.from)) return false;
    let result;
    try {
      result = htmlToMarkdown(this.parse(html));
    } catch {
      return false; // whatever went wrong, the browser still pastes the plain text
    }
    if (!result.formatted || !result.markdown) return false;
    event.preventDefault();
    const plain = data.getData("text/plain").replace(/\r\n?/g, "\n");
    const insert = placeBlocks(view.state, range.from, range.to, result.markdown);
    view.dispatch({
      changes: { from: range.from, to: range.to, insert },
      selection: EditorSelection.cursor(range.from + insert.length),
      scrollIntoView: true,
      userEvent: "input.paste.rich",
    });
    this.last = { from: range.from, to: range.from + insert.length, plain };
    this.showNote(view, result.images);
    return true;
  }

  // Replace the last formatted paste with the clipboard's plain text.
  asPlain(view) {
    const last = this.last;
    this.forget();
    if (!last) return;
    view.dispatch({
      changes: { from: last.from, to: last.to, insert: last.plain },
      selection: EditorSelection.cursor(last.from + last.plain.length),
      scrollIntoView: true,
      userEvent: "input.paste",
    });
    view.focus();
  }

  showNote(view, images) {
    this.note?.remove();
    const host = view.dom.closest(".pane") ?? view.dom.parentElement;
    this.note = el("div", { class: "paste-note", role: "status" },
      el("span", { class: "muted" }, t("Pasted with formatting.")),
      el("button", { type: "button", class: "link", onmousedown: (e) => e.preventDefault(),
                     onclick: () => this.asPlain(view),
                     title: t("Ctrl+Shift+V pastes plain text right away") }, t("Paste as plain text")),
      el("button", { type: "button", class: "link paste-note-close", "aria-label": t("Close"),
                     onmousedown: (e) => e.preventDefault(), onclick: () => this.forget() }, "×"));
    host.append(this.note);
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.forget(), 12000);
    if (images) {
      toast(images === 1 ? t("A picture in the pasted text was left out. Paste or drop it on its own to add it.")
        : t("{n} pictures in the pasted text were left out. Paste or drop each one on its own to add it.", { n: images }));
    }
  }

  forget() {
    this.last = null;
    clearTimeout(this.timer);
    this.note?.remove();
    this.note = null;
  }
}

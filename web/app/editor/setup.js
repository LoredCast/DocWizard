// Editor composition (ARCHITECTURE.md §6.3): languages, highlighting, keymaps and options.
import {
  Compartment, EditorState, EditorView, HighlightStyle, Language, autocompletion, closeBrackets, closeBracketsKeymap,
  completionKeymap, defaultKeymap, defineLanguageFacet, drawSelection, dropCursor, foldKeymap, foldService,
  highlightSelectionMatches, highlightSpecialChars, history, historyKeymap, indentOnInput, keymap, markdownParser,
  GFM, languageDataProp, rectangularSelection, searchKeymap, syntaxHighlighting, tags as t,
} from "codemirror";
import { citationCompletions, libraryField } from "./citations.js";
import { markdownKeymap } from "./commands.js";
import { latexLanguage } from "./latex-mode.js";
import { livePreview } from "./live-preview.js";
import { pandocExtensions, pandocTags } from "./pandoc-syntax.js";
import { nextCell, previousCell } from "./tables.js";

const markdownData = defineLanguageFacet({
  commentTokens: { block: { open: "<!--", close: "-->" } },
  autocomplete: citationCompletions,
});

// The document node carries the language data, so citation autocomplete (and comment
// tokens) apply; a bare Language doesn't attach it by itself.
export const markdownLanguage = new Language(
  markdownData,
  markdownParser.configure([GFM, ...pandocExtensions, { props: [languageDataProp.add({ Document: markdownData })] }]),
  [],
  "markdown",
);

const markdownHighlight = HighlightStyle.define([
  { tag: t.processingInstruction, class: "cm-md-mark" },
  { tag: t.contentSeparator, class: "cm-md-mark" },
  { tag: t.emphasis, class: "cm-md-em" },
  { tag: t.strong, class: "cm-md-strong" },
  { tag: t.strikethrough, class: "cm-md-strike" },
  { tag: t.link, class: "cm-md-link" },
  { tag: t.url, class: "cm-md-url" },
  { tag: t.monospace, class: "cm-md-code" },
  { tag: t.quote, class: "cm-md-quote" },
  { tag: t.meta, class: "cm-md-meta" },
  { tag: t.comment, class: "cm-comment" },
  { tag: pandocTags.math, class: "cm-md-math" },
  { tag: pandocTags.frontMatter, class: "cm-md-frontmatter" },
  { tag: pandocTags.attributes, class: "cm-md-meta" },
  { tag: pandocTags.citationKey, class: "cm-md-link" },
]);

const latexHighlight = HighlightStyle.define([
  { tag: t.keyword, class: "cm-latex-cmd" },
  { tag: t.string, class: "cm-md-math" },
  { tag: t.comment, class: "cm-comment" },
  { tag: t.bracket, class: "cm-md-mark" },
]);

// Fold a heading's section: up to the next heading of the same or a higher level.
const HEADING = /^(#{1,6})\s/;
const headingFold = foldService.of((state, lineStart) => {
  const line = state.doc.lineAt(lineStart);
  const m = HEADING.exec(line.text);
  if (!m) return null;
  const level = m[1].length;
  let end = line;
  for (let n = line.number + 1; n <= state.doc.lines; n++) {
    const next = state.doc.line(n);
    const h = HEADING.exec(next.text);
    if (h && h[1].length <= level) break;
    end = next;
  }
  return end.number > line.number ? { from: line.to, to: end.to } : null;
});

export function languageFor(kind) {
  if (kind === "markdown") {
    return [
      markdownLanguage,
      syntaxHighlighting(markdownHighlight),
      headingFold,
      libraryField,
      keymap.of(markdownKeymap),
      // In a table, Tab and Shift+Tab move between cells; elsewhere they keep their usual job.
      keymap.of([{ key: "Tab", run: nextCell }, { key: "Shift-Tab", run: previousCell }]),
    ];
  }
  if (kind === "latex") return [latexLanguage, syntaxHighlighting(latexHighlight)];
  return [];
}

function cspNonce() {
  return document.querySelector('meta[name="csp-nonce"]')?.content ?? "";
}

// Whether the text can be typed in; suggest mode (§8.10) opens it to commenters.
export const access = new Compartment();
export function accessExtensions(readOnly, editable = !readOnly) {
  return [EditorState.readOnly.of(readOnly), EditorView.editable.of(editable)];
}

// Everything except collaboration, which the session adds per version.
export function baseExtensions({ kind, readOnly, live = true, lang = "", reading = false, editable = !readOnly, label = "" }) {
  return [
    EditorView.cspNonce.of(cspNonce()),
    highlightSpecialChars(),
    history(),
    drawSelection(),
    dropCursor(),
    EditorState.allowMultipleSelections.of(true),
    indentOnInput(),
    closeBrackets(),
    rectangularSelection(),
    // Reading (the review view), other occurrences of a selection are only noise.
    reading ? [] : highlightSelectionMatches(),
    autocompletion({ activateOnTyping: true, icons: false }),
    EditorView.lineWrapping,
    access.of(accessExtensions(readOnly, editable)),
    EditorView.contentAttributes.of({ spellcheck: "true", autocorrect: "on", autocapitalize: "sentences",
                                      ...(lang ? { lang } : {}), ...(label ? { "aria-label": label } : {}) }),
    languageFor(kind),
    kind === "markdown" && live ? livePreview({ reading }) : [],
    keymap.of([...closeBracketsKeymap, ...defaultKeymap, ...searchKeymap, ...historyKeymap, ...foldKeymap,
               ...completionKeymap]),
  ];
}

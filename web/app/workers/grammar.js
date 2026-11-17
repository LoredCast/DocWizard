// Grammar and spelling with Harper (ARCHITECTURE.md §6.8), off the main thread. Harper is
// WebAssembly from web/vendor/harper/; the text never leaves the browser.
//
// in:  { id, text, dialect, words }   (words: the person's own dictionary)
// out: { id, matches: [{ from, to, message, title, kind, suggestions: [{ text, kind }] }] }
//      or { id, error }
import { mask, touchesMask } from "./grammar-text.js";

const base = new URL("../../vendor/harper/", import.meta.url);
let harper = null;   // the module, once loaded
let linter = null;
let dialect = null;
let words = "";

async function linterFor(wanted, dictionary) {
  if (!harper) {
    const [mod, bin] = await Promise.all([import(new URL("index.js", base).href), import(new URL("binary.js", base).href)]);
    harper = { ...mod, binary: bin.binary };
  }
  if (!linter) {
    linter = new harper.LocalLinter({ binary: harper.binary, dialect: wanted });
    await linter.setup();
    dialect = wanted;
  } else if (dialect !== wanted) {
    await linter.setDialect(wanted);
    dialect = wanted;
  }
  const key = dictionary.join("\n");
  if (key !== words) {
    await linter.clearWords();
    if (dictionary.length) await linter.importWords(dictionary);
    words = key;
  }
  return linter;
}

// Suggestion kinds in Harper: 0 replace, 1 remove, 2 insert after.
const KINDS = ["replace", "remove", "insert"];

self.onmessage = async (event) => {
  const { id, text, dialect: wanted = 0, words: dictionary = [] } = event.data;
  try {
    const lint = await linterFor(wanted, dictionary);
    const { text: masked, ranges } = mask(text);
    const lints = await lint.lint(masked, { language: "markdown" });
    // harper.js gives spans in UTF-16 units, as the editor counts.
    const matches = lints.map((l) => ({
      from: l.span().start,
      to: l.span().end,
      message: l.message(),
      title: l.lint_kind_pretty(),
      kind: l.lint_kind() === "Spelling" ? "spelling" : "grammar",
      suggestions: l.suggestions().slice(0, 5).map((s) => ({ text: s.get_replacement_text(), kind: KINDS[s.kind()] ?? "replace" })),
    })).filter((m) => m.to > m.from && !touchesMask(ranges, m.from, m.to));
    for (const l of lints) l.free?.();
    self.postMessage({ id, matches });
  } catch (error) {
    self.postMessage({ id, error: String(error?.message ?? error) });
  }
};

// A small StreamLanguage tokenizer for LaTeX documents (ARCHITECTURE.md §6.3).
import { StreamLanguage } from "codemirror";

export const latexLanguage = StreamLanguage.define({
  name: "latex",
  startState: () => ({ math: null }),
  token(stream, state) {
    if (state.math) {
      if (stream.match(state.math)) { state.math = null; return "keyword"; }
      if (stream.match(/^\\[a-zA-Z@]+|^\\./)) return "string";
      stream.next();
      return "string";
    }
    if (stream.match("%")) { stream.skipToEnd(); return "comment"; }
    if (stream.match(/^\\(begin|end)\b/)) return "keyword";
    if (stream.match(/^\\[a-zA-Z@]+\*?/)) return "keyword";
    if (stream.match(/^\\./)) return "keyword";
    if (stream.match("$$")) { state.math = "$$"; return "keyword"; }
    if (stream.match("$")) { state.math = "$"; return "keyword"; }
    if (stream.match("\\[")) { state.math = "\\]"; return "keyword"; }
    if (stream.match(/^[{}[\]]/)) return "bracket";
    stream.eatWhile(/[^\\%${}[\]]/);
    if (stream.current() === "") stream.next();
    return null;
  },
  languageData: { commentTokens: { line: "%" } },
});

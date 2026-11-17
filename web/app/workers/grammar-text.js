// Text helpers for grammar checking (ARCHITECTURE.md §6.8), shared by the editor and the
// Harper worker; nothing here touches the DOM.
//
// What a grammar checker shouldn't read in a Pandoc Markdown document (§6.8): front matter,
// citations, math, raw LaTeX, comments, attributes and div fences. They are blanked out with
// spaces, unit for unit, so offsets into the masked text are offsets into the document.
// Blanks make runs of spaces a checker may point out, so findings that touch a blanked range
// are dropped (see touchesMask).

const FRONT = /^---[ \t]*\r?\n[\s\S]*?\r?\n(?:---|\.\.\.)[ \t]*(?=\r?\n|$)/;
const PATTERNS = [
  /<!--[\s\S]*?-->/g,                          // comments
  /\$\$[\s\S]+?\$\$/g,                         // display math
  /(?<![\\$\w])\$(?![\s$])(?:\\.|[^$\\\n])+?(?<!\s)\$(?![\d$])/g, // inline math
  /\[-?@[^\]\n]*\]/g,                           // [@key, p. 3]
  /(?<![\w.])-?@[\w:.#$%&\-+?<>~/]*\w/g,       // @key in text, @fig:x
  /\\[a-zA-Z]+\*?(?:\[[^\]\n]*\])?(?:\{[^}\n]*\})*/g, // \raw{latex}
  /\{[#.=][^}\n]*\}/g,                         // {#id .class key=value}
  /^[ \t]*:{3,}.*$/gm,                         // ::: div fences
  /^[ \t]*\\newpage[ \t]*$/gm,
];

function blank(s) {
  return s.replace(/[^\n]/g, " ");
}

// { text, ranges }: the masked text and the blanked [from, to) ranges in UTF-16 units, sorted.
export function mask(text) {
  let out = text;
  const ranges = [];
  const front = FRONT.exec(out);
  if (front) {
    out = blank(front[0]) + out.slice(front[0].length);
    ranges.push([0, front[0].length]);
  }
  for (const re of PATTERNS) {
    out = out.replace(re, (match, ...rest) => {
      const at = rest.find((x) => typeof x === "number");
      ranges.push([at, at + match.length]);
      return blank(match);
    });
  }
  ranges.sort((a, b) => a[0] - b[0]);
  return { text: out, ranges };
}

// Whether [from, to) overlaps or borders a blanked range.
export function touchesMask(ranges, from, to) {
  let lo = 0;
  let hi = ranges.length;
  while (lo < hi) { // the first range that ends at or after from
    const mid = (lo + hi) >> 1;
    if (ranges[mid][1] < from) lo = mid + 1; else hi = mid;
  }
  return lo < ranges.length && ranges[lo][0] <= to;
}

const LANG_LINE = /^lang:[ \t]*["']?([A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*)["']?[ \t]*$/m;

// The document's lang: from its front matter, else *fallback* (the project's language).
export function documentLanguage(text, fallback) {
  const front = FRONT.exec(text.slice(0, 20000));
  const m = front && LANG_LINE.exec(front[0]);
  return (m ? m[1] : fallback || "").trim();
}

// Harper's dialects: American, British, Australian, Canadian, Indian.
export function dialectOf(lang) {
  const region = (lang.split("-")[1] || "").toUpperCase();
  return { GB: 1, UK: 1, IE: 1, NZ: 1, AU: 2, CA: 3, IN: 4 }[region] ?? 0;
}

// "harper", "languagetool" or "" for the document's language, the person's choice and what
// this server offers. A document without a language counts as English.
export function checkerFor(grammar, lang) {
  if (!grammar || grammar.choice === "off") return "";
  const english = !lang || /^en\b/i.test(lang);
  if (grammar.choice === "languagetool") return grammar.languagetool ? "languagetool" : "";
  if (grammar.choice === "harper") return grammar.harper && english ? "harper" : "";
  if (english && grammar.harper) return "harper";
  return grammar.languagetool ? "languagetool" : "";
}

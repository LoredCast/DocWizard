// Finding text across a project's documents (ARCHITECTURE.md §8.12). Pure functions, so they
// run the same in the browser and in the tests. Offsets are JavaScript string indexes, which
// are the UTF-16 units the server and the collaboration protocol count in.

export const MAX_MATCHES = 5000;

function escape(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// A global, Unicode-aware pattern for *query*; throws a SyntaxError for a bad regular expression.
export function compile(query, { regexp = false, caseSensitive = false, wholeWord = false } = {}) {
  let source = regexp ? query : escape(query);
  if (wholeWord) source = `(?<![\\p{L}\\p{N}_])(?:${source})(?![\\p{L}\\p{N}_])`;
  return new RegExp(source, `gu${caseSensitive ? "" : "i"}${regexp ? "m" : ""}`);
}

// Every non-empty match: [{from, to, text, groups, named}], at most *limit*.
export function findAll(text, re, limit = MAX_MATCHES) {
  const out = [];
  re.lastIndex = 0;
  let m;
  while ((m = re.exec(text)) !== null) {
    if (m[0] === "") {
      re.lastIndex += text.codePointAt(re.lastIndex) > 0xffff ? 2 : 1;
      if (re.lastIndex > text.length) break;
      continue;
    }
    out.push({ from: m.index, to: m.index + m[0].length, text: m[0], groups: [...m].slice(1), named: m.groups ?? {} });
    if (out.length >= limit) break;
  }
  return out;
}

// The replacement for one match: "$&" the match, "$1"… groups, "$<name>" named groups, "$$" a dollar.
// Plain searches take the replacement as it is.
export function expandReplacement(template, match, regexp = false) {
  if (!regexp) return template;
  return template.replace(/\$(\$|&|<([^>]+)>|(\d{1,2}))/g, (all, what, name, num) => {
    if (what === "$") return "$";
    if (what === "&") return match.text;
    if (name !== undefined) return match.named[name] ?? "";
    let n = Number(num);
    if (n >= 1 && n <= match.groups.length) return match.groups[n - 1] ?? "";
    // "$12" with one group is "$1" then "2", as in String.replace.
    n = Number(num[0]);
    if (num.length === 2 && n >= 1 && n <= match.groups.length) return (match.groups[n - 1] ?? "") + num[1];
    return all;
  });
}

// Line (1-based) and column (1-based, in code points) of *pos*, with that line's bounds.
export function lineAt(text, pos) {
  const lineFrom = text.lastIndexOf("\n", pos - 1) + 1;
  let lineTo = text.indexOf("\n", pos);
  if (lineTo < 0) lineTo = text.length;
  let line = 1;
  for (let i = text.indexOf("\n"); i >= 0 && i < lineFrom; i = text.indexOf("\n", i + 1)) line++;
  return { line, col: [...text.slice(lineFrom, pos)].length + 1, lineFrom, lineTo };
}

// Words around a match on its line: {before, text, after}, trimmed to about *radius* characters a side.
export function context(text, from, to, radius = 40) {
  const { lineFrom, lineTo } = lineAt(text, from);
  let start = Math.max(lineFrom, from - radius);
  let end = Math.min(Math.max(lineTo, to), to + radius);
  if (start > lineFrom) {
    const space = text.indexOf(" ", start);
    if (space >= 0 && space < from) start = space + 1;
  }
  if (end < lineTo) {
    const space = text.lastIndexOf(" ", end);
    if (space > to) end = space;
  }
  return {
    before: (start > lineFrom ? "…" : "") + text.slice(start, from),
    text: text.slice(from, to),
    after: text.slice(to, end) + (end < lineTo ? "…" : ""),
  };
}

// Matches of every document, with what each one would become and where it is.
export function searchDocs(docs, query, options = {}, limit = MAX_MATCHES) {
  if (!query) return { groups: [], total: 0, truncated: false };
  const re = compile(query, options);
  const groups = [];
  let total = 0;
  for (const doc of docs) {
    const found = findAll(doc.text, re, limit - total);
    if (!found.length) continue;
    groups.push({
      doc,
      matches: found.map((m) => {
        // Line numbers counted once per document, not once per match.
        return { ...m, insert: expandReplacement(options.replacement ?? "", m, options.regexp) };
      }),
    });
    total += found.length;
    if (total >= limit) break;
  }
  for (const g of groups) {
    let line = 1, at = 0;
    for (const m of g.matches) {
      for (let i = g.doc.text.indexOf("\n", at); i >= 0 && i < m.from; i = g.doc.text.indexOf("\n", i + 1)) line++;
      at = m.from;
      const lineFrom = g.doc.text.lastIndexOf("\n", m.from - 1) + 1;
      m.line = line;
      m.col = [...g.doc.text.slice(lineFrom, m.from)].length + 1;
      m.context = context(g.doc.text, m.from, m.to);
    }
  }
  return { groups, total, truncated: total >= limit };
}

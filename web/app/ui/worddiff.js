// A small word-level diff for showing suggestions (ARCHITECTURE.md §8.10): which words of
// `before` go and which words of `after` come. Longest common subsequence over word tokens,
// with the common prefix and suffix taken off first, so typical suggestions stay cheap.
const TOKEN = /\s+|[\p{L}\p{N}_]+|[^\s\p{L}\p{N}_]/gu;
const LIMIT = 4_000_000; // cells; beyond that, show the passages whole

export function tokens(text) {
  return text.match(TOKEN) ?? [];
}

// [{op: "eq" | "del" | "ins", text}]
export function wordDiff(before, after) {
  const a = tokens(before);
  const b = tokens(after);
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start++;
  let endA = a.length;
  let endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) { endA--; endB--; }
  const out = [];
  const push = (op, text) => {
    if (!text) return;
    const last = out[out.length - 1];
    if (last && last.op === op) last.text += text;
    else out.push({ op, text });
  };
  push("eq", a.slice(0, start).join(""));
  const x = a.slice(start, endA);
  const y = b.slice(start, endB);
  if (x.length * y.length > LIMIT) {
    push("del", x.join(""));
    push("ins", y.join(""));
  } else {
    // lengths[i][j]: LCS of x[i..] and y[j..]
    const n = x.length;
    const m = y.length;
    const lengths = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        lengths[i][j] = x[i] === y[j] ? lengths[i + 1][j + 1] + 1 : Math.max(lengths[i + 1][j], lengths[i][j + 1]);
      }
    }
    let i = 0;
    let j = 0;
    while (i < n || j < m) {
      if (i < n && j < m && x[i] === y[j]) { push("eq", x[i]); i++; j++; }
      else if (i < n && (j === m || lengths[i + 1][j] >= lengths[i][j + 1])) { push("del", x[i]); i++; }
      else { push("ins", y[j]); j++; }
    }
  }
  push("eq", a.slice(endA).join(""));
  return out;
}

// Elements for a diff: <del> and <ins> for changes, plain text for the rest.
export function diffNodes(before, after, el) {
  return wordDiff(before, after).map((s) => (s.op === "eq" ? el("span", {}, s.text)
    : el(s.op, {}, s.text)));
}

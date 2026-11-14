// Word counts and outline off the main thread (ARCHITECTURE.md §6.6).
// Body words exclude front matter, code, raw blocks, HTML comments, citation keys and math;
// footnotes are counted separately.

function strip(text) {
  let s = text.replace(/\r/g, "");
  s = s.replace(/^---\n[\s\S]*?\n(?:---|\.\.\.)\n/, "");
  s = s.replace(/<!--[\s\S]*?-->/g, " ");
  s = s.replace(/^(```+|~~~+)[^\n]*\n[\s\S]*?^\1[^\n]*$/gm, " ");
  s = s.replace(/\$\$[\s\S]*?\$\$/g, " ");
  s = s.replace(/\$[^$\n]+\$/g, " ");
  s = s.replace(/`[^`\n]*`/g, " ");
  return s;
}

function countWords(text) {
  const s = text
    .replace(/-?@[\p{L}\p{N}_][\p{L}\p{N}_:.#$%&\-+?<>~/]*/gu, " ")
    .replace(/\{[#.][^}]*\}/g, " ")
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/^\s{0,3}(#{1,6}|[-*+]|\d+[.)]|>|:::+.*|\|)/gm, " ")
    .replace(/[*_~`|#>\\[\](){}]/g, " ");
  const m = s.match(/[\p{L}\p{N}][\p{L}\p{N}'’\-]*/gu);
  return m ? m.length : 0;
}

function analyse(text) {
  let body = strip(text);
  let notes = 0;
  // Inline notes ^[…] (one level of nested brackets) and note definitions [^id]: …
  body = body.replace(/\^\[((?:[^[\]]|\[[^\]]*\])*)\]/g, (_, inner) => { notes += countWords(inner); return " "; });
  body = body.replace(/^\[\^[^\]]+\]:[^\n]*(\n {4}[^\n]*)*/gm, (m) => {
    notes += countWords(m.replace(/^\[\^[^\]]+\]:/, ""));
    return " ";
  });
  body = body.replace(/\[\^[^\]]+\]/g, " ");
  const outline = [];
  const lines = text.split("\n");
  let inCode = false;
  for (let i = 0; i < lines.length; i++) {
    if (/^(```|~~~)/.test(lines[i])) inCode = !inCode;
    const m = !inCode && /^(#{1,6})\s+(.*?)\s*(?:\{[^}]*\})?\s*#*\s*$/.exec(lines[i]);
    if (m) outline.push({ level: m[1].length, title: m[2], line: i + 1 });
  }
  return { words: countWords(body), notes, chars: body.replace(/\s/g, "").length, outline };
}

self.onmessage = (event) => {
  const { id, text } = event.data;
  self.postMessage({ id, ...analyse(text) });
};

// Word counts and outline off the main thread (ARCHITECTURE.md §6.6).
// Body words exclude front matter, code, raw blocks, HTML comments, citation keys and math;
// footnotes are counted separately. For screenplays it also lists scenes and characters and
// estimates the page count, following the rules of filters/screenplay.lua.

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

// -- Screenplays ---------------------------------------------------------------
const SCENE = /^(INT\.?\/EXT|EXT\.?\/INT|I\/E|INNEN\/AUSSEN|INT|EXT|EST|INNEN|AUSSEN|AUẞEN)[.\s/]/iu;
// Characters per line at 12 pt Courier, by element, and lines per page (The Hollywood Standard).
const WIDTH = { action: 61, dialogue: 35, parenthetical: 25 };
const LINES_PER_PAGE = 55;

const isUpper = (s) => {
  const letters = s.replace(/\([^)]*\)/g, "").replace(/[^\p{L}]/gu, "");
  return letters !== "" && s.toLocaleUpperCase() === s;
};
const isScene = (s) => (/^\.[\p{L}\p{N}]/u.test(s)) || SCENE.test(s);
const isTransition = (s) => isUpper(s) && (/(TO|AUF):$/.test(s) || /^(FADE OUT|ABBLENDE)/.test(s));
const wrapped = (s, width) => Math.max(1, Math.ceil(s.length / width));

// The name in a character cue: without @, the dual-dialogue ^ and extensions like (V.O.).
export function cueName(line) {
  return line.replace(/^@/, "").replace(/\s*\^\s*$/, "").replace(/\s*\([^)]*\)/g, "").trim();
}

export function analyseScript(text) {
  const lines = text.replace(/\r/g, "").split("\n");
  let start = 0;
  if (lines[0] === "---") {
    const end = lines.findIndex((l, i) => i > 0 && (l === "---" || l === "..."));
    if (end > 0) start = end + 1;
  }
  const scenes = [];
  const counts = new Map();
  let total = 0;
  let inCode = false;
  let block = [];
  const flush = () => {
    if (!block.length) return;
    const first = block[0].text.trim();
    total += 1; // the blank line before each element
    const heading = /^(#{1,6})\s+(.*?)\s*#*\s*$/.exec(first);
    if (heading) {
      if (heading[1].length > 1) scenes.push({ title: heading[2], line: block[0].n });
      total += 1;
    } else if (block.length === 1 && isScene(first)) {
      scenes.push({ title: first.replace(/^\.(?!\.)/, ""), line: block[0].n });
      total += 1;
    } else if (block.length > 1 && (first.startsWith("@") || (isUpper(first) && !isScene(first) && !first.endsWith(":")))) {
      const name = cueName(first);
      if (name) counts.set(name, (counts.get(name) ?? 0) + 1);
      total += 1;
      for (const l of block.slice(1)) {
        const s = l.text.trim();
        total += /^\(.*\)$/.test(s) ? wrapped(s, WIDTH.parenthetical) : wrapped(s, WIDTH.dialogue);
      }
    } else {
      for (const l of block) total += wrapped(l.text.trim(), WIDTH.action);
    }
    block = [];
  };
  for (let i = start; i < lines.length; i++) {
    const line = lines[i];
    if (/^(```|~~~)/.test(line)) inCode = !inCode;
    if (inCode) continue;
    if (!line.trim() || /^:::/.test(line)) { flush(); continue; }
    if (/^<!--.*-->$/.test(line.trim())) continue;
    block.push({ text: line, n: i + 1 });
  }
  flush();
  const characters = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([n]) => n);
  const pages = total ? Math.max(1, Math.round((total / LINES_PER_PAGE) * 10) / 10) : 0;
  return { scenes, characters, pages };
}

export function analyse(text, script = false) {
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
  const result = { words: countWords(body), notes, chars: body.replace(/\s/g, "").length, outline };
  if (script) {
    // Acts (#) stay in the outline; scene headings, written either way, join it in order.
    const sp = analyseScript(text);
    const scenes = new Set(sp.scenes.map((s) => s.line));
    result.outline = [...outline.filter((h) => h.level === 1 && !scenes.has(h.line)),
                      ...sp.scenes.map((s) => ({ level: 2, title: s.title, line: s.line, scene: true }))]
      .sort((a, b) => a.line - b.line);
    result.script = { pages: sp.pages, scenes: sp.scenes.length, characters: sp.characters };
  }
  return result;
}

if (typeof self !== "undefined" && "onmessage" in self) {
  self.onmessage = (event) => {
    const { id, text, script } = event.data;
    self.postMessage({ id, ...analyse(text, script) });
  };
}

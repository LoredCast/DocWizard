// Slides in a Markdown deck, read from the source the way filters/slides.lua reads the document:
// headings at the slide level start slides, higher headings start section slides, --- starts a
// slide without a title, ::: notes holds speaker notes. Used by the slide sorter and the
// presenter view; the numbers match the slide-N ids of the web deck.

const FENCE = /^ {0,3}(`{3,}|~{3,})/;
const ATX = /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*#*[ \t]*$/;
const RULE = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
const SETEXT = /^ {0,3}(=+|-+)[ \t]*$/;
const DIV_OPEN = /^ {0,3}(:{3,})[ \t]*(\{[^}]*\}|[\w-]+)/;
const DIV_CLOSE = /^ {0,3}(:{3,})[ \t]*$/;
const PAUSE = /^ {0,3}\. \. \.[ \t]*$/;

function lineStarts(text) {
  const starts = [0];
  for (let i = 0; i < text.length; i++) if (text.charCodeAt(i) === 10) starts.push(i + 1);
  return starts;
}

function frontMatter(lines) {
  if (!/^---[ \t]*$/.test(lines[0] ?? "")) return { end: 0, meta: {} };
  for (let i = 1; i < lines.length; i++) {
    if (/^(---|\.\.\.)[ \t]*$/.test(lines[i])) {
      const meta = {};
      for (const line of lines.slice(1, i)) {
        const m = /^([\w-]+):[ \t]*(.*)$/.exec(line);
        if (m) meta[m[1]] = m[2].replace(/^["']|["']$/g, "");
      }
      return { end: i + 1, meta };
    }
  }
  return { end: 0, meta: {} };
}

// Text without Markdown marks, for cards and titles.
export function plain(s) {
  return s.replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1").replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[@[^\]]*\]/g, "").replace(/\{[^}]*\}/g, "").replace(/[*_`~^]+/g, "")
    .replace(/^\s*([-+*]|\d+[.)])\s+/, "").replace(/^\s*>\s?/, "").trim();
}

// Top-level blocks we care about, one entry per line: { line, kind, level, title }.
function scan(lines, from) {
  const items = [];
  let fence = null;
  let depth = 0; // fenced divs
  let notes = 0; // depth of a notes div, 0 if none open
  let prevText = false;
  for (let i = from; i < lines.length; i++) {
    const line = lines[i];
    if (fence) {
      if (line.trimStart().startsWith(fence)) fence = null;
      items.push({ line: i, kind: notes ? "note" : "text" });
      prevText = false;
      continue;
    }
    const f = FENCE.exec(line);
    if (f) {
      fence = f[1];
      items.push({ line: i, kind: notes ? "note" : "text" });
      prevText = false;
      continue;
    }
    const close = DIV_CLOSE.exec(line);
    if (close && depth > 0) {
      if (notes === depth) {
        notes = 0;
        items.push({ line: i, kind: "notes-close" });
      } else {
        items.push({ line: i, kind: notes ? "note" : "text" });
      }
      depth -= 1;
      prevText = false;
      continue;
    }
    const open = DIV_OPEN.exec(line);
    if (open) {
      depth += 1;
      const isNotes = /^notes$|\.notes\b/.test(open[2].replace(/^\{|\}$/g, "").trim());
      if (depth === 1 && isNotes && !notes) {
        notes = depth;
        items.push({ line: i, kind: "notes-open" });
      } else {
        items.push({ line: i, kind: notes ? "note" : "text" });
      }
      prevText = false;
      continue;
    }
    if (depth > 0) {
      items.push({ line: i, kind: notes ? "note" : "text", inDiv: true });
      prevText = line.trim() !== "";
      continue;
    }
    const atx = ATX.exec(line);
    if (atx) {
      items.push({ line: i, kind: "heading", level: atx[1].length, title: plain(atx[2] ?? "") });
      prevText = false;
      continue;
    }
    const setext = SETEXT.exec(line);
    if (setext && prevText && items.length && items[items.length - 1].kind === "text") {
      // The line before is the title.
      const title = items.pop();
      items.push({ line: title.line, kind: "heading", level: setext[1][0] === "=" ? 1 : 2,
                   title: plain(lines[title.line]), underline: i });
      prevText = false;
      continue;
    }
    if (RULE.test(line) && !prevText) {
      items.push({ line: i, kind: "rule" });
      continue;
    }
    if (PAUSE.test(line)) {
      items.push({ line: i, kind: "pause" });
      prevText = false;
      continue;
    }
    items.push({ line: i, kind: line.trim() === "" ? "blank" : "text" });
    prevText = line.trim() !== "";
  }
  return items;
}

// The slide level: front matter or option, else the highest heading level followed by content.
function slideLevel(items, set) {
  const n = Number.parseInt(set, 10);
  if (n >= 1 && n <= 6) return n;
  let level = null;
  for (let i = 0; i < items.length; i++) {
    if (items[i].kind !== "heading") continue;
    let j = i + 1;
    while (j < items.length && items[j].kind === "blank") j++;
    const next = items[j];
    if (next && next.kind !== "heading" && (level === null || items[i].level < level)) level = items[i].level;
  }
  return level ?? 2;
}

/** Parse a deck. Returns { level, meta, bodyStart, slides: [...] }; each slide has
 * n (its number in the deck), kind ("title", "section", "slide", "references"), title,
 * from/to (character offsets of its source; none for the title and reference slides),
 * line (0-based first line), lines (body text, plain) and notes (plain). */
export function parseSlides(text, options = {}) {
  const lines = text.split("\n");
  const starts = lineStarts(text);
  const fm = frontMatter(lines);
  const items = scan(lines, fm.end);
  const level = slideLevel(items, fm.meta["slide-level"] ?? options["slide-level"]);
  const slides = [];
  if (fm.meta.title) {
    slides.push({ kind: "title", title: plain(fm.meta.title), lines: [fm.meta.subtitle, fm.meta.author]
      .filter(Boolean).map((x) => plain(x.replace(/^\[|\]$/g, ""))), notes: "", from: null, to: null, line: 0 });
  }
  let current = null;
  const open = (kind, title, line) => {
    current = { kind, title, line, from: starts[line], to: text.length, lines: [], notes: [] };
    slides.push(current);
  };
  for (const item of items) {
    if (item.kind === "heading" && item.level <= level) {
      open(item.level < level ? "section" : "slide", item.title, item.line);
    } else if (item.kind === "rule") {
      open("slide", "", item.line);
    } else if (item.kind === "note") {
      if (!current) open("slide", "", item.line);
      const s = lines[item.line].trim();
      if (s) current.notes.push(s);
    } else if (item.kind === "notes-open") {
      if (!current) open("slide", "", item.line);
    } else if (item.kind === "text" || item.kind === "heading") {
      const raw = lines[item.line].trim();
      let s = item.kind === "heading" ? item.title : plain(raw);
      if (!current && !s) continue;
      if (!current) open("slide", "", item.line);
      if (/^:{3,}|^[-:|+ =]+$/.test(raw)) continue; // div fences, table rules
      if (raw.startsWith("|")) s = raw.split("|").map(plain).filter(Boolean).join(" · ");
      if (s) current.lines.push(s);
    }
  }
  // Each slide's source runs to the next one's start.
  const own = slides.filter((s) => s.from !== null);
  for (let i = 0; i < own.length - 1; i++) own[i].to = own[i + 1].from;
  for (const s of own) s.notes = s.notes.join("\n");
  if (/\[[^\]\n]*@[\w:.#$%&+?<>~/-]|(^|[\s(])@[A-Za-z_][\w:.#$%&+?<>~/-]*/m.test(text.slice(starts[fm.end] ?? 0))
      && !/\{#refs[\s}]/.test(text)) {
    slides.push({ kind: "references", title: "", lines: [], notes: "", from: null, to: null, line: lines.length - 1 });
  }
  slides.forEach((s, i) => { s.n = i + 1; });
  return { level, meta: fm.meta, bodyStart: starts[fm.end] ?? text.length, slides };
}

/** The text with the slide at index *from* (in deck.slides) moved before the slide at *to*
 * (or to the end when *to* is past the last movable slide). Returns null when nothing moves. */
export function moveSlide(text, deck, from, to) {
  const movable = deck.slides.filter((s) => s.from !== null);
  const src = movable.indexOf(deck.slides[from]);
  if (src < 0) return null;
  const target = deck.slides[to];
  let dest = target && target.from !== null ? movable.indexOf(target) : movable.length;
  if (dest < 0) dest = target?.kind === "title" ? 0 : movable.length;
  if (dest === src || dest === src + 1) return null;
  const head = text.slice(0, movable[0].from);
  const tail = ""; // the last slide runs to the end of the text
  let chunks = movable.map((s) => text.slice(s.from, s.to));
  // Every chunk ends with a blank line, so slides stay apart wherever they go.
  chunks = chunks.map((c) => c.replace(/\n*$/, "\n\n"));
  const [moved] = chunks.splice(src, 1);
  chunks.splice(dest > src ? dest - 1 : dest, 0, moved);
  let body = chunks.join("");
  // Keep the text's own ending (one newline, or none).
  const ending = /\n*$/.exec(text)[0];
  body = body.replace(/\n*$/, ending.length ? "\n" : "");
  return head + body + tail;
}

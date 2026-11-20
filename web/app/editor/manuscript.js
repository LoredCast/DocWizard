// The writing studio's models (ARCHITECTURE.md §6.3): the corkboard's lanes and cards, a
// screenplay's production breakdown, its speeches (for the line highlighter and the table read)
// and the Normseite estimate. Plain functions over the text, without the DOM, so the editor, the
// stats worker and the Node tests share them. Offsets are string indices (UTF-16), as in CodeMirror.
import { cueName, isScene, isTransition, isUpper } from "../workers/stats.js";

const HEADING = /^(#{1,6})\s+(.*?)\s*(?:\{[^}]*\})?\s*#*\s*$/;
// A scene break between scenes of prose: ***, * * *, ___ or --- on a line of its own.
const BREAK = /^\s{0,3}(?:(?:\*\s*){3,}|(?:_\s*){3,}|(?:-\s*){3,})$/;
const FENCE = /^(```|~~~)/;

// The text's lines with their offsets; where the body starts (after the front matter).
export function lines(text) {
  const out = [];
  let from = 0;
  for (const line of text.split("\n")) {
    out.push({ text: line, from, to: from + line.length, n: out.length + 1 });
    from += line.length + 1;
  }
  let start = 0;
  if (out.length > 1 && out[0].text === "---") {
    const end = out.findIndex((l, i) => i > 0 && (l.text === "---" || l.text === "..."));
    if (end > 0) start = end + 1;
  }
  return { lines: out, start };
}

// Plain words of a passage of Markdown, for card summaries and spoken text.
export function plain(text) {
  return text
    .replace(/<!--[\s\S]*?-->/g, " ")
    .replace(/\^\[(?:[^[\]]|\[[^\]]*\])*\]/g, " ")
    .replace(/\[\^[^\]]+\]/g, "")
    .replace(/\[-?@[^\]]*\]/g, "")
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\{[#.][^}]*\}/g, "")
    .replace(/^\s{0,3}(#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s?|:::+.*$)/gm, "")
    .replace(/(\*\*|__|\*|_|~~|`)(?=\S)([\s\S]*?\S)\1/g, "$2")
    .replace(/\\([\\`*_{}[\]()#+\-.!$@^~|<>])/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
}

function countWords(text) {
  const m = plain(text).match(/[\p{L}\p{N}][\p{L}\p{N}'’-]*/gu);
  return m ? m.length : 0;
}

function summary(text, max = 160) {
  const s = plain(text);
  if (s.length <= max) return s;
  const cut = s.slice(0, max);
  const space = cut.lastIndexOf(" ");
  return `${cut.slice(0, space > max / 2 ? space : max).replace(/[,;:.\s]+$/, "")}…`;
}

const blank = (l) => !l.text.trim();

// -- Corkboard -----------------------------------------------------------------
// The board has a lane for each chapter (a level 1 heading; an act in a screenplay) and a card
// for each scene in it: a lower heading, a scene heading in a screenplay, or the text after a
// scene break (***). Text before a chapter's first scene is a card of its own, without a title.
// Text before the first chapter is the opening lane, which has no heading and stays first.
export function board(text, { script = false } = {}) {
  const { lines: ls, start } = lines(text);
  const bodyStart = start < ls.length ? ls[start].from : text.length;
  const lanes = [];
  let lane = { title: null, line: 0, from: bodyStart, headTo: null, cards: [] };
  lanes.push(lane);
  let card = null;
  let breakText = null;
  let inCode = false;
  const close = (at) => {
    if (card) {
      card.to = at;
      card = null;
    }
  };
  for (let i = start; i < ls.length; i++) {
    const l = ls[i];
    if (FENCE.test(l.text)) inCode = !inCode;
    if (inCode && !FENCE.test(l.text)) {
      if (!card) card = openCard(lane, "body", null, l, null);
      continue;
    }
    const h = HEADING.exec(l.text);
    const prevBlank = i === start || blank(ls[i - 1]);
    const nextBlank = i + 1 >= ls.length || blank(ls[i + 1]);
    if (h && h[1].length === 1) {
      close(l.from);
      lane.to = l.from;
      lane = { title: h[2], line: l.n, from: l.from, headTo: null, cards: [] };
      lanes.push(lane);
      continue;
    }
    if (h || (script && prevBlank && nextBlank && isScene(l.text.trim()))) {
      close(l.from);
      if (lane.headTo === null) lane.headTo = l.from;
      card = openCard(lane, h ? "heading" : "scene", h ? h[2] : l.text.trim().replace(/^\.(?!\.)/, ""), l, null);
      continue;
    }
    if (!script && BREAK.test(l.text) && prevBlank) {
      close(l.from);
      if (lane.headTo === null) lane.headTo = l.from;
      breakText ??= l.text.trim();
      card = openCard(lane, "break", null, l, l.text.trim());
      continue;
    }
    if (blank(l)) continue;
    if (!card) {
      if (lane.headTo === null) lane.headTo = l.from;
      card = openCard(lane, "body", null, l, null);
    }
  }
  close(text.length);
  lane.to = text.length;
  for (const ln of lanes) {
    if (ln.headTo === null) ln.headTo = ln.to;
    for (const c of ln.cards) finishCard(text, c);
  }
  const out = lanes.filter((ln, i) => i > 0 || ln.cards.length || text.slice(ln.from, ln.to).trim());
  let n = 0;
  for (const ln of out) for (const c of ln.cards) c.id = n++;
  return { lanes: out, breakText: breakText ?? "***", start: bodyStart, end: text.length };
}

function openCard(lane, kind, title, line, sep) {
  const card = { kind, title, line: line.n, from: line.from, to: null, sep };
  lane.cards.push(card);
  return card;
}

function finishCard(text, c) {
  let body = text.slice(c.from, c.to);
  if (c.kind === "break") body = body.replace(/^[^\n]*\n?/, ""); // the break line itself
  else if (c.kind === "heading" || c.kind === "scene") body = body.replace(/^[^\n]*\n?/, "");
  c.text = text.slice(c.from, c.to).replace(/\s+$/, "");
  // The card's own text, without the break before it: what moves with the card.
  c.own = c.kind === "break" ? c.text.replace(/^[^\n]*\n\s*/, "") : c.text;
  c.words = countWords(body);
  c.synopsis = summary(body);
}

// The text of one lane with its cards in the given order. A card without a heading of its own
// gets a scene break before it unless it opens the lane.
function laneText(lane, cards, breakText, last) {
  const head = lane.title === null ? lane.headText : `${lane.headText.replace(/\s+$/, "")}\n\n`;
  const parts = cards.map((c, i) => {
    const titled = c.kind === "heading" || c.kind === "scene";
    return !titled && i > 0 ? `${c.sep ?? breakText}\n\n${c.own}` : c.own;
  });
  const body = parts.join("\n\n");
  if (!body) return last ? head.replace(/\s+$/, "\n") : head;
  return `${head}${body}${last ? "\n" : "\n\n"}`;
}

// Move a card (by id) to a lane (by index into board.lanes) before the card at *index* of that
// lane (or at its end). Returns one change {from, to, insert} for CodeMirror, or null.
export function moveCard(text, model, id, toLane, index) {
  const lanes = model.lanes.map((ln) => ({ ...ln, headText: text.slice(ln.from, ln.headTo), list: [...ln.cards] }));
  const fromLane = lanes.findIndex((ln) => ln.list.some((c) => c.id === id));
  if (fromLane < 0 || toLane < 0 || toLane >= lanes.length) return null;
  const src = lanes[fromLane].list;
  const at = src.findIndex((c) => c.id === id);
  const [card] = src.splice(at, 1);
  const dest = lanes[toLane].list;
  let i = Math.max(0, Math.min(index ?? dest.length, dest.length));
  if (fromLane === toLane && index !== null && index !== undefined && index > at) i -= 1;
  dest.splice(i, 0, card);
  return rewrite(text, model, lanes, Math.min(fromLane, toLane), Math.max(fromLane, toLane));
}

// Move a whole chapter (a lane with a heading) before the lane at *index*.
export function moveLane(text, model, from, index) {
  const lanes = model.lanes.map((ln) => ({ ...ln, headText: text.slice(ln.from, ln.headTo), list: [...ln.cards] }));
  if (lanes[from]?.title === null) return null;
  const order = [...lanes];
  const [lane] = order.splice(from, 1);
  let i = Math.max(0, Math.min(index, order.length));
  if (order[0]?.title === null) i = Math.max(1, i); // the opening stays first
  order.splice(i, 0, lane);
  const first = Math.min(from, i);
  const last = Math.max(from, i);
  const changed = order.slice(first, last + 1);
  return rewrite(text, model, lanes, first, last, changed);
}

function rewrite(text, model, lanes, first, last, ordered = null) {
  const span = ordered ?? lanes.slice(first, last + 1);
  const from = model.lanes[first].from;
  const to = model.lanes[last].to;
  const atEnd = last === lanes.length - 1;
  let insert = span.map((ln, k) => laneText(ln, ln.list, model.breakText, atEnd && k === span.length - 1)).join("");
  if (atEnd && !text.endsWith("\n")) insert = insert.replace(/\n+$/, "");
  return minimal(text, from, to, insert);
}

// The smallest change that turns text[from, to) into insert: common start and end are kept,
// so comments and cursors in untouched text stay where they are.
export function minimal(text, from, to, insert) {
  const old = text.slice(from, to);
  if (old === insert) return null;
  let a = 0;
  while (a < old.length && a < insert.length && old[a] === insert[a]) a++;
  let b = 0;
  while (b < old.length - a && b < insert.length - a && old[old.length - 1 - b] === insert[insert.length - 1 - b]) b++;
  return { from: from + a, to: to - b, insert: insert.slice(a, insert.length - b) };
}

// -- Screenplays -----------------------------------------------------------------
// The elements of a screenplay, following filters/screenplay.lua: acts, scenes, action, speeches
// (a cue, parentheticals and dialogue), transitions and centred text, with their lines.
export function scriptElements(text) {
  const { lines: ls, start } = lines(text);
  const out = [];
  let block = [];
  let inCode = false;
  const flush = () => {
    if (!block.length) return;
    const first = block[0].text.trim();
    const h = HEADING.exec(first);
    const line = block[0].n;
    const endLine = block[block.length - 1].n;
    if (h) {
      out.push({ type: h[1].length === 1 ? "act" : "scene", title: h[2], line, endLine: line });
      if (block.length > 1) out.push({ type: "action", line: block[1].n, endLine, text: block.slice(1).map((l) => l.text).join("\n") });
    } else if (block.length === 1 && isScene(first)) {
      out.push({ type: "scene", title: first.replace(/^\.(?!\.)/, ""), line, endLine });
    } else if (block.length > 1 && (first.startsWith("@") || (isUpper(first) && !isScene(first) && !first.endsWith(":")))) {
      const speech = { type: "speech", name: cueName(first), line, endLine, parts: [] };
      for (const l of block.slice(1)) {
        const s = l.text.trim();
        speech.parts.push({ type: /^\(.*\)$/.test(s) ? "parenthetical" : "dialogue", line: l.n, text: s });
      }
      out.push(speech);
    } else if (block.length === 1 && (isTransition(first) || /^>[^<]*$/.test(first))) {
      out.push({ type: "transition", line, endLine, text: first.replace(/^>\s*/, "") });
    } else if (/^>.*<$/.test(first)) {
      out.push({ type: "centered", line, endLine, text: block.map((l) => l.text.trim().replace(/^>\s*|\s*<$/g, "")).join("\n") });
    } else {
      out.push({ type: "action", line, endLine, text: block.map((l) => l.text).join("\n") });
    }
    block = [];
  };
  for (let i = start; i < ls.length; i++) {
    const l = ls[i];
    if (FENCE.test(l.text)) { inCode = !inCode; flush(); continue; }
    if (inCode) continue;
    if (!l.text.trim() || /^:::/.test(l.text)) { flush(); continue; }
    if (/^<!--.*-->$/.test(l.text.trim())) continue;
    block.push(l);
  }
  flush();
  return out;
}

const WIDTH = { action: 61, dialogue: 35, parenthetical: 25 };
const LINES_PER_PAGE = 55;
const wrapped = (s, width) => Math.max(1, Math.ceil(s.length / width));
const SCENE_PREFIX = /^(INT\.?\/EXT\.?|EXT\.?\/INT\.?|I\/E\.?|INNEN\/AUSSEN\.?|INT\.?|EXT\.?|EST\.?|INNEN\.?|AUSSEN\.?|AUẞEN\.?)\s*/iu;

// "INT. HARBOUR OFFICE - NIGHT" → INT, HARBOUR OFFICE, NIGHT. Scene numbers (#12#) are dropped.
export function parseSceneHeading(title) {
  const s = title.replace(/\s*#[^#\s]+#\s*$/, "").trim();
  const m = SCENE_PREFIX.exec(s);
  let place = m ? s.slice(m[0].length) : s;
  const setting = m ? m[1].replace(/\.$/, "").toUpperCase().replace("AUẞEN", "AUSSEN") : "";
  let time = "";
  const dash = place.search(/\s+[-–—]\s+(?!.*\s[-–—]\s)/);
  if (dash >= 0) {
    time = place.slice(dash).replace(/^\s+[-–—]\s+/, "").trim();
    place = place.slice(0, dash).trim();
  }
  return { setting, location: place.replace(/[.\s]+$/, "").toLocaleUpperCase(), time: time.toLocaleUpperCase() };
}

function elementLines(e) {
  if (e.type === "speech") {
    return 2 + e.parts.reduce((n, p) => n + wrapped(p.text, p.type === "parenthetical" ? WIDTH.parenthetical : WIDTH.dialogue), 0);
  }
  if (e.type === "scene" || e.type === "act" || e.type === "transition") return 2;
  return 1 + (e.text ?? "").split("\n").reduce((n, l) => n + wrapped(l.trim(), WIDTH.action), 0);
}

// Scenes with their place, time, length in eighths of a page and cast; the cast with their
// speeches and dialogue words; the locations with their scenes. As a production office reads it.
export function breakdown(text) {
  const elements = scriptElements(text);
  const scenes = [];
  let scene = null;
  let act = null;
  for (const e of elements) {
    if (e.type === "act") { act = e.title; continue; }
    if (e.type === "scene") {
      scene = { n: scenes.length + 1, title: e.title, line: e.line, act, ...parseSceneHeading(e.title),
                lines: 0, cast: [], speeches: 0, words: 0 };
      scenes.push(scene);
    }
    if (!scene) continue;
    scene.lines += elementLines(e);
    if (e.type === "speech" && e.name) {
      if (!scene.cast.includes(e.name)) scene.cast.push(e.name);
      scene.speeches += 1;
    }
  }
  const cast = new Map();
  const places = new Map();
  for (const s of scenes) {
    s.eighths = Math.max(1, Math.round((s.lines / LINES_PER_PAGE) * 8));
    const key = s.location || s.title.toLocaleUpperCase();
    const loc = places.get(key) ?? { name: key, settings: [], times: [], scenes: [], eighths: 0 };
    if (s.setting && !loc.settings.includes(s.setting)) loc.settings.push(s.setting);
    if (s.time && !loc.times.includes(s.time)) loc.times.push(s.time);
    loc.scenes.push(s.n);
    loc.eighths += s.eighths;
    places.set(key, loc);
  }
  let sceneNo = 0;
  for (const e of elements) {
    if (e.type === "scene") sceneNo += 1;
    if (e.type !== "speech" || !e.name) continue;
    const c = cast.get(e.name) ?? { name: e.name, speeches: 0, words: 0, scenes: [], first: sceneNo || null };
    c.speeches += 1;
    c.words += e.parts.filter((p) => p.type === "dialogue").reduce((n, p) => n + countWords(p.text), 0);
    if (sceneNo && !c.scenes.includes(sceneNo)) c.scenes.push(sceneNo);
    cast.set(e.name, c);
  }
  return {
    scenes,
    cast: [...cast.values()].sort((a, b) => b.speeches - a.speeches || b.words - a.words || a.name.localeCompare(b.name)),
    locations: [...places.values()].sort((a, b) => b.scenes.length - a.scenes.length || a.name.localeCompare(b.name)),
    eighths: scenes.reduce((n, s) => n + s.eighths, 0),
  };
}

// The lines one character speaks: their cues, parentheticals and dialogue, as [first, last] line pairs.
export function speechLines(text, name) {
  const want = name.toLocaleUpperCase();
  return scriptElements(text).filter((e) => e.type === "speech" && e.name.toLocaleUpperCase() === want)
    .map((e) => [e.line, e.endLine]);
}

// What a table read speaks, from the element at or after *fromLine*: scene headings, action and
// transitions by the narrator, dialogue by its character. Parentheticals are left out.
export function readingScript(text, fromLine = 1, { action = true } = {}) {
  const out = [];
  for (const e of scriptElements(text)) {
    if (e.endLine < fromLine) continue;
    if (e.type === "speech") {
      const words = e.parts.filter((p) => p.type === "dialogue").map((p) => plain(p.text)).join(" ").trim();
      if (words) out.push({ who: e.name, text: words, line: e.line, endLine: e.endLine });
    } else if (action || e.type === "scene" || e.type === "act") {
      const words = plain(e.title ?? e.text ?? "");
      if (words) out.push({ who: null, text: words, line: e.line, endLine: e.endLine, kind: e.type });
    }
  }
  return out;
}

// -- Normseite -------------------------------------------------------------------
// Normseiten as German publishers count them: lines of at most 60 characters, 30 to a page,
// paragraphs starting on a new line, chapters (level 1 headings) on a new page.
export function normseiten(text, { width = 60, perPage = 30 } = {}) {
  const { lines: ls, start } = lines(text);
  let pages = 0;
  let used = 0;
  let inCode = false;
  let para = [];
  const wrap = (s) => {
    let n = 0;
    let len = 0;
    for (const word of s.split(/\s+/).filter(Boolean)) {
      if (len === 0) { len = word.length; n = 1; continue; }
      if (len + 1 + word.length > width) { n += 1 + Math.floor(word.length / width); len = word.length % width; }
      else len += 1 + word.length;
    }
    return n;
  };
  const add = (n) => { used += n; };
  const flush = () => {
    if (!para.length) return;
    const s = plain(para.join(" "));
    if (s) add(wrap(s));
    para = [];
  };
  const newPage = () => {
    if (used > 0) pages += Math.ceil(used / perPage);
    used = 0;
  };
  for (let i = start; i < ls.length; i++) {
    const l = ls[i].text;
    if (FENCE.test(l)) inCode = !inCode;
    if (inCode) continue;
    const h = HEADING.exec(l);
    if (h) {
      flush();
      if (h[1].length === 1) newPage();
      add(2);
      continue;
    }
    if (BREAK.test(l)) { flush(); add(1); continue; }
    if (!l.trim()) { flush(); continue; }
    para.push(l.trim());
  }
  flush();
  const total = pages + used / perPage;
  return Math.round(total * 10) / 10;
}

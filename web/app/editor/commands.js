// Markdown commands: list continuation, formatting toggles, blocks and insertions (§6.3).
import { EditorSelection, startCompletion, syntaxTree } from "codemirror";

const LIST = /^(\s*)([-*+]|(\d+)([.)]))(\s+)(\[[ xX]\]\s+)?/;
const TASK_LINE = /^(\s*)([-*+]\s+\[[ xX]\]\s+)/;
const BULLET_LINE = /^(\s*)([-*+]\s+)/;
const ORDERED_LINE = /^(\s*)(\d+[.)]\s+)/;
const HEADING_LINE = /^(\s{0,3})(#{1,6})(\s+)/;
const QUOTE_LINE = /^(\s*)(>\s?)/;
const DELIM_MARKS = new Set(["EmphasisMark", "StrikethroughMark", "CodeMark", "MathMark"]);

export function continueList(view) {
  const { state } = view;
  if (state.selection.ranges.length !== 1 || !state.selection.main.empty) return false;
  const pos = state.selection.main.head;
  const line = state.doc.lineAt(pos);
  const m = LIST.exec(line.text);
  if (!m || pos < line.from + m[0].length) return false;
  const rest = line.text.slice(m[0].length);
  if (!rest.trim()) {
    // An empty item ends the list.
    view.dispatch({ changes: { from: line.from, to: line.to, insert: "" }, userEvent: "delete" });
    return true;
  }
  let marker = m[2];
  if (m[3]) marker = `${Number(m[3]) + 1}${m[4]}`;
  const task = m[6] ? "[ ] " : "";
  const insert = `\n${m[1]}${marker}${m[5]}${task}`;
  view.dispatch({
    changes: { from: pos, insert },
    selection: { anchor: pos + insert.length },
    scrollIntoView: true,
    userEvent: "input",
  });
  return true;
}

function findEnclosing(state, from, to, nodeType) {
  const tree = syntaxTree(state);
  const probes = from === to ? [tree.resolveInner(from, -1), tree.resolveInner(from, 1)]
    : [tree.resolveInner(from, 1)];
  for (const start of probes) {
    for (let cur = start; cur; cur = cur.parent) {
      if (cur.name !== nodeType) continue;
      const inside = from === to ? (cur.from < from && from < cur.to) : (cur.from <= from && to <= cur.to);
      if (!inside) continue;
      const open = cur.firstChild;
      const close = cur.lastChild;
      if (open && close && open !== close && DELIM_MARKS.has(open.name) && DELIM_MARKS.has(close.name)) {
        return { node: cur, open, close };
      }
    }
  }
  return null;
}

function emptyPairAround(state, pos, mark) {
  if (pos < mark.length) return false;
  const before = state.sliceDoc(pos - mark.length, pos);
  const after = state.sliceDoc(pos, pos + mark.length);
  if (before !== mark || after !== mark) return false;
  if (mark === "*") {
    const prev2 = pos >= 2 ? state.sliceDoc(pos - 2, pos) : "";
    const next2 = state.sliceDoc(pos, pos + 2);
    if (prev2 === "**" || next2 === "**") return false;
  }
  return true;
}

export function toggleWrap(mark, nodeType) {
  return (view) => {
    const { state } = view;
    const tr = state.changeByRange((range) => {
      if (nodeType) {
        const hit = findEnclosing(state, range.from, range.to, nodeType);
        if (hit) {
          const { node, open, close } = hit;
          // At the end of a non-empty inline span, exit past the closing mark.
          if (range.empty && range.from === close.from && close.from > open.to) {
            return { range: EditorSelection.cursor(close.to) };
          }
          const openLen = open.to - open.from;
          const changes = [{ from: open.from, to: open.to }, { from: close.from, to: close.to }];
          if (range.empty) {
            const pos = Math.max(open.from, range.from - openLen);
            return { changes, range: EditorSelection.cursor(pos) };
          }
          if (range.from <= open.to && range.to >= close.from) {
            const end = Math.max(open.from, close.from - openLen);
            return { changes, range: EditorSelection.range(open.from, end) };
          }
          const a = Math.max(open.from, range.from - openLen);
          const b = Math.max(a, range.to - openLen);
          return { changes, range: EditorSelection.range(a, b) };
        }
      }
      if (range.empty) {
        if (emptyPairAround(state, range.from, mark)) {
          return {
            changes: { from: range.from - mark.length, to: range.to + mark.length },
            range: EditorSelection.cursor(range.from - mark.length),
          };
        }
        return {
          changes: { from: range.from, insert: mark + mark },
          range: EditorSelection.cursor(range.from + mark.length),
        };
      }
      const raw = state.sliceDoc(range.from, range.to);
      const lead = raw.match(/^\s*/)[0].length;
      const trail = raw.match(/\s*$/)[0].length;
      const a = range.from + lead;
      const b = range.to - trail;
      if (a >= b) return { range };
      const inner = state.sliceDoc(a, b);
      if (!nodeType && inner.length >= 2 * mark.length && inner.startsWith(mark) && inner.endsWith(mark)) {
        return {
          changes: [{ from: a, to: a + mark.length }, { from: b - mark.length, to: b }],
          range: EditorSelection.range(a, b - 2 * mark.length),
        };
      }
      if (!nodeType && a >= mark.length
          && state.sliceDoc(a - mark.length, a) === mark && state.sliceDoc(b, b + mark.length) === mark) {
        return {
          changes: [{ from: a - mark.length, to: a }, { from: b, to: b + mark.length }],
          range: EditorSelection.range(a - mark.length, b - mark.length),
        };
      }
      return {
        changes: [{ from: a, insert: mark }, { from: b, insert: mark }],
        range: EditorSelection.range(a + mark.length, b + mark.length),
      };
    });
    view.dispatch(state.update(tr, { userEvent: "input", scrollIntoView: true }));
    return true;
  };
}

function selectedLines(state) {
  const seen = new Set();
  const lines = [];
  for (const r of state.selection.ranges) {
    const start = state.doc.lineAt(r.from).number;
    let end = state.doc.lineAt(r.to).number;
    if (r.to > r.from && r.to === state.doc.line(end).from) end -= 1;
    for (let n = start; n <= end; n++) {
      if (!seen.has(n)) {
        seen.add(n);
        lines.push(state.doc.line(n));
      }
    }
  }
  return lines;
}

function targetLines(lines) {
  const nonEmpty = lines.filter((l) => l.text.trim().length > 0);
  return nonEmpty.length ? nonEmpty : lines;
}

function applyLineChanges(view, changes) {
  if (!changes.length) return false;
  const cs = view.state.changes(changes);
  const selection = view.state.selection.map(cs, 1);
  view.dispatch({ changes: cs, selection, userEvent: "input", scrollIntoView: true });
  return true;
}

function stripListPrefix(text) {
  const m = TASK_LINE.exec(text) || BULLET_LINE.exec(text) || ORDERED_LINE.exec(text);
  return m ? [m[1].length, m[1].length + m[2].length] : null;
}

export function cycleHeading(view) {
  const lines = targetLines(selectedLines(view.state));
  const first = HEADING_LINE.exec(lines[0].text);
  const current = first ? first[2].length : 0;
  const next = current >= 3 ? 0 : current + 1;
  const changes = [];
  for (const line of lines) {
    const m = HEADING_LINE.exec(line.text);
    const insert = next ? `${"#".repeat(next)} ` : "";
    if (m) {
      changes.push({ from: line.from + m[1].length, to: line.from + m[1].length + m[2].length + m[3].length, insert });
    } else if (insert) {
      const indent = line.text.match(/^\s*/)[0].length;
      changes.push({ from: line.from + indent, to: line.from + indent, insert });
    }
  }
  return applyLineChanges(view, changes);
}

export function toggleQuote(view) {
  const lines = targetLines(selectedLines(view.state));
  const allQuoted = lines.every((l) => QUOTE_LINE.test(l.text));
  const changes = [];
  for (const line of lines) {
    const m = QUOTE_LINE.exec(line.text);
    if (allQuoted) {
      if (m) changes.push({ from: line.from + m[1].length, to: line.from + m[0].length, insert: "" });
    } else if (!m) {
      const indent = line.text.match(/^\s*/)[0].length;
      changes.push({ from: line.from + indent, insert: "> " });
    }
  }
  return applyLineChanges(view, changes);
}

function toggleList(kind) {
  const test = kind === "task" ? TASK_LINE : kind === "ordered" ? ORDERED_LINE : BULLET_LINE;
  return (view) => {
    const lines = targetLines(selectedLines(view.state));
    const allMatch = lines.every((l) => test.test(l.text) && (kind !== "bullet" || !TASK_LINE.test(l.text)));
    const changes = [];
    let index = 1;
    for (const line of lines) {
      const span = stripListPrefix(line.text);
      if (allMatch) {
        if (span) changes.push({ from: line.from + span[0], to: line.from + span[1], insert: "" });
      } else {
        const marker = kind === "task" ? "- [ ] " : kind === "ordered" ? `${index++}. ` : "- ";
        if (span) {
          changes.push({ from: line.from + span[0], to: line.from + span[1], insert: marker });
        } else {
          const indent = line.text.match(/^\s*/)[0].length;
          changes.push({ from: line.from + indent, insert: marker });
        }
      }
    }
    return applyLineChanges(view, changes);
  };
}

export const toggleBold = toggleWrap("**", "StrongEmphasis");
export const toggleItalic = toggleWrap("*", "Emphasis");
export const toggleStrike = toggleWrap("~~", "Strikethrough");
export const toggleCode = toggleWrap("`", "InlineCode");
export const toggleInlineMath = toggleWrap("$", "InlineMath");
export const toggleBulletList = toggleList("bullet");
export const toggleOrderedList = toggleList("ordered");
export const toggleTaskList = toggleList("task");

export function insertLink(view) {
  const { state } = view;
  const range = state.selection.main;
  const tree = syntaxTree(state);
  for (let cur = tree.resolveInner(range.from, 1); cur; cur = cur.parent) {
    if (cur.name === "Link" && cur.from <= range.from && range.to <= cur.to) {
      const marks = [];
      for (let c = cur.firstChild; c; c = c.nextSibling) {
        if (c.name === "LinkMark") marks.push(c);
      }
      if (marks.length >= 2) {
        const textFrom = marks[0].to, textTo = marks[1].from;
        view.dispatch({
          changes: [{ from: marks[0].from, to: marks[0].to }, { from: marks[1].from, to: cur.to }],
          selection: EditorSelection.range(marks[0].from, marks[0].from + Math.max(0, textTo - textFrom)),
          scrollIntoView: true,
          userEvent: "input",
        });
        return true;
      }
      break;
    }
  }
  const raw = state.sliceDoc(range.from, range.to);
  if (!range.empty) {
    const lead = raw.match(/^\s*/)[0].length;
    const trail = raw.match(/\s*$/)[0].length;
    const a = range.from + lead;
    const b = range.to - trail;
    if (a < b) {
      const inner = state.sliceDoc(a, b);
      if (/^https?:\/\/\S+$/i.test(inner)) {
        view.dispatch({
          changes: { from: a, to: b, insert: `[](${inner})` },
          selection: EditorSelection.cursor(a + 1),
          scrollIntoView: true,
          userEvent: "input",
        });
        return true;
      }
      const insert = `[${inner}](url)`;
      view.dispatch({
        changes: { from: a, to: b, insert },
        selection: EditorSelection.range(a + inner.length + 3, a + inner.length + 6),
        scrollIntoView: true,
        userEvent: "input",
      });
      return true;
    }
  }
  view.dispatch({
    changes: { from: range.from, to: range.to, insert: "[](url)" },
    selection: EditorSelection.cursor(range.from + 1),
    scrollIntoView: true,
    userEvent: "input",
  });
  return true;
}

export function insertCitation(view) {
  const { state } = view;
  const range = state.selection.main;
  const selected = range.empty ? "" : state.sliceDoc(range.from, range.to).trim().replace(/^@/, "");
  const insert = `[@${selected}]`;
  const cursor = range.from + 2 + selected.length;
  view.dispatch({
    changes: { from: range.from, to: range.to, insert },
    selection: EditorSelection.cursor(cursor),
    scrollIntoView: true,
    userEvent: "input",
  });
  startCompletion(view);
  return true;
}

export function insertFootnote(view) {
  const { state } = view;
  const range = state.selection.main;
  const tree = syntaxTree(state);
  for (let cur = tree.resolveInner(range.from, 1); cur; cur = cur.parent) {
    if (cur.name === "InlineNote" && cur.from <= range.from && range.to <= cur.to) {
      const innerFrom = cur.from + 2, innerTo = Math.max(innerFrom, cur.to - 1);
      view.dispatch({
        changes: [{ from: cur.from, to: innerFrom }, { from: innerTo, to: cur.to }],
        selection: EditorSelection.range(cur.from, cur.from + (innerTo - innerFrom)),
        scrollIntoView: true,
        userEvent: "input",
      });
      return true;
    }
  }
  const inner = range.empty ? "" : state.sliceDoc(range.from, range.to);
  view.dispatch({
    changes: { from: range.from, to: range.to, insert: `^[${inner}]` },
    selection: EditorSelection.cursor(range.from + 2 + inner.length),
    scrollIntoView: true,
    userEvent: "input",
  });
  return true;
}

export function activeFormatting(state) {
  const active = {
    bold: false, italic: false, strike: false, code: false, math: false,
    heading: 0, quote: false, bullet: false, ordered: false, task: false,
    link: false, footnote: false, citation: false,
  };
  const { head, empty } = state.selection.main;
  const tree = syntaxTree(state);
  const probes = empty ? [tree.resolveInner(head, -1), tree.resolveInner(head, 1)] : [tree.resolveInner(head, 1)];
  for (const start of probes) {
    for (let cur = start; cur; cur = cur.parent) {
      if (empty && !(cur.from <= head && head <= cur.to)) continue;
      switch (cur.name) {
        case "StrongEmphasis": active.bold = true; break;
        case "Emphasis": active.italic = true; break;
        case "Strikethrough": active.strike = true; break;
        case "InlineCode": active.code = true; break;
        case "InlineMath":
        case "DisplayMath": active.math = true; break;
        case "Blockquote": active.quote = true; break;
        case "Link": active.link = true; break;
        case "InlineNote": active.footnote = true; break;
        case "Citation": active.citation = true; break;
        default:
          if (/^ATXHeading[1-6]$/.test(cur.name)) active.heading = Number(cur.name.slice(-1));
      }
    }
  }
  const text = state.doc.lineAt(head).text;
  if (TASK_LINE.test(text)) active.task = true;
  else if (BULLET_LINE.test(text)) active.bullet = true;
  else if (ORDERED_LINE.test(text)) active.ordered = true;
  if (!active.heading) {
    const hm = HEADING_LINE.exec(text);
    if (hm) active.heading = hm[2].length;
  }
  if (!active.quote && QUOTE_LINE.test(text)) active.quote = true;
  return active;
}

export const markdownKeymap = [
  { key: "Enter", run: continueList },
  { key: "Mod-b", run: toggleBold },
  { key: "Mod-i", run: toggleItalic },
  { key: "Mod-Shift-x", run: toggleStrike },
];

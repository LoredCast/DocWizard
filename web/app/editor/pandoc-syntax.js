// @lezer/markdown extensions for pandoc Markdown (ARCHITECTURE.md §6.3):
// YAML front matter, citations, inline and display math, inline footnotes,
// fenced div markers and attributes.
import { Tag, tags as t } from "codemirror";

export const pandocTags = {
  math: Tag.define(),
  citation: Tag.define(),
  citationKey: Tag.define(),
  frontMatter: Tag.define(t.meta),
  attributes: Tag.define(t.meta),
};

const DOLLAR = 36, AT = 64, BRACKET = 91, CARET = 94, BRACE = 123;
const KEY_START = /[\p{L}\p{N}_]/u;
const KEY_BODY = /^[\p{L}\p{N}_][\p{L}\p{N}_:.#$%&\-+?<>~/]*/u;

function citationKey(text) {
  // Pandoc allows internal punctuation, but not at the end of a key.
  const m = KEY_BODY.exec(text);
  if (!m) return null;
  return m[0].replace(/[:.#$%&\-+?<>~/]+$/, "");
}

const FrontMatter = {
  defineNodes: [
    { name: "FrontMatter", block: true, style: pandocTags.frontMatter },
    { name: "FrontMatterMark", style: t.processingInstruction },
  ],
  parseBlock: [{
    name: "FrontMatter",
    before: "HorizontalRule",
    parse(cx, line) {
      if (cx.lineStart !== 0 || !/^---\s*$/.test(line.text)) return false;
      const start = cx.lineStart;
      const children = [cx.elt("FrontMatterMark", start, start + 3)];
      let end = start + line.text.length;
      let closed = false;
      while (cx.nextLine()) {
        end = cx.lineStart + line.text.length;
        if (/^(---|\.\.\.)\s*$/.test(line.text)) {
          children.push(cx.elt("FrontMatterMark", cx.lineStart, cx.lineStart + 3));
          closed = true;
          break;
        }
      }
      if (closed) cx.nextLine();
      cx.addElement(cx.elt("FrontMatter", start, end, children));
      return true;
    },
  }],
};

const Citations = {
  defineNodes: [
    { name: "Citation", style: pandocTags.citation },
    { name: "CitationKey", style: pandocTags.citationKey },
    { name: "CitationMark", style: t.processingInstruction },
  ],
  parseInline: [{
    name: "Citation",
    before: "Link",
    parse(cx, next, pos) {
      if (next === BRACKET) {
        const rest = cx.slice(pos, Math.min(cx.end, pos + 2000));
        const m = /^\[([^[\]\n]*)\]/.exec(rest);
        if (!m || !/(^|[\s;])-?@[\p{L}\p{N}_]/u.test(m[1])) return -1;
        if (rest[m[0].length] === "(" || rest[m[0].length] === "[") return -1; // a link
        const children = [cx.elt("CitationMark", pos, pos + 1)];
        const inner = m[1];
        const re = /(^|[\s;])(-?)@/gu;
        let k;
        while ((k = re.exec(inner))) {
          const at = k.index + k[1].length + k[2].length;
          const key = citationKey(inner.slice(at + 1));
          if (key) children.push(cx.elt("CitationKey", pos + 1 + at, pos + 2 + at + key.length));
        }
        const end = pos + m[0].length;
        children.push(cx.elt("CitationMark", end - 1, end));
        return cx.addElement(cx.elt("Citation", pos, end, children));
      }
      if (next === AT) {
        const before = pos > cx.offset ? cx.slice(pos - 1, pos) : "";
        if (before && /[\p{L}\p{N}_]/u.test(before)) return -1; // an email address
        if (!KEY_START.test(cx.slice(pos + 1, pos + 2))) return -1;
        const key = citationKey(cx.slice(pos + 1, Math.min(cx.end, pos + 200)));
        if (!key) return -1;
        const end = pos + 1 + key.length;
        return cx.addElement(cx.elt("Citation", pos, end, [cx.elt("CitationKey", pos, end)]));
      }
      return -1;
    },
  }],
};

const MathSyntax = {
  defineNodes: [
    { name: "InlineMath", style: pandocTags.math },
    { name: "DisplayMath", style: pandocTags.math },
    { name: "MathMark", style: t.processingInstruction },
  ],
  parseInline: [{
    name: "Math",
    before: "Emphasis",
    parse(cx, next, pos) {
      if (next !== DOLLAR) return -1;
      if (cx.char(pos + 1) === DOLLAR) {
        const rest = cx.slice(pos + 2, cx.end);
        const idx = rest.indexOf("$$");
        if (idx < 0) return -1;
        const end = pos + 2 + idx + 2;
        return cx.addElement(cx.elt("DisplayMath", pos, end,
          [cx.elt("MathMark", pos, pos + 2), cx.elt("MathMark", end - 2, end)]));
      }
      // Inline: no space after the opening $, none before the closing $, no digit after it.
      const first = cx.slice(pos + 1, pos + 2);
      if (!first || /\s/.test(first)) return -1;
      const rest = cx.slice(pos + 1, Math.min(cx.end, pos + 2000));
      for (let i = 0; i < rest.length; i++) {
        const ch = rest[i];
        if (ch === "\\") { i++; continue; }
        if (ch === "\n" && rest[i + 1] === "\n") return -1;
        if (ch === "$" && i > 0 && !/\s/.test(rest[i - 1]) && !/\d/.test(rest[i + 1] ?? "")) {
          const end = pos + 1 + i + 1;
          return cx.addElement(cx.elt("InlineMath", pos, end,
            [cx.elt("MathMark", pos, pos + 1), cx.elt("MathMark", end - 1, end)]));
        }
      }
      return -1;
    },
  }],
};

const InlineNotes = {
  defineNodes: [
    { name: "InlineNote", style: t.special(t.content) },
    { name: "NoteMark", style: t.processingInstruction },
  ],
  parseInline: [{
    name: "InlineNote",
    before: "Link",
    parse(cx, next, pos) {
      if (next !== CARET || cx.char(pos + 1) !== BRACKET) return -1;
      let depth = 0;
      for (let p = pos + 1; p < cx.end; p++) {
        const c = cx.char(p);
        if (c === 92) { p++; continue; } // backslash
        if (c === BRACKET) depth++;
        else if (c === 93 && --depth === 0) {
          return cx.addElement(cx.elt("InlineNote", pos, p + 1,
            [cx.elt("NoteMark", pos, pos + 2), cx.elt("NoteMark", p, p + 1)]));
        }
      }
      return -1;
    },
  }],
};

const Attributes = {
  defineNodes: [{ name: "Attributes", style: pandocTags.attributes }],
  parseInline: [{
    name: "Attributes",
    parse(cx, next, pos) {
      if (next !== BRACE) return -1;
      const m = /^\{[#.=]?[\p{L}_-][^{}\n]*\}/u.exec(cx.slice(pos, Math.min(cx.end, pos + 300)));
      if (!m || !/^\{(\s*([#.][\w:-]+|[\w-]+=("[^"]*"|\S+)|=\w+)\s*)+\}$/.test(m[0])) return -1;
      return cx.addElement(cx.elt("Attributes", pos, pos + m[0].length));
    },
  }],
};

const FencedDivs = {
  defineNodes: [{ name: "FencedDivMark", block: true, style: t.processingInstruction }],
  parseBlock: [{
    name: "FencedDiv",
    before: "HorizontalRule",
    parse(cx, line) {
      if (!/^:{3,}/.test(line.text)) return false;
      cx.addElement(cx.elt("FencedDivMark", cx.lineStart, cx.lineStart + line.text.length));
      cx.nextLine();
      return true;
    },
  }],
};

export const pandocExtensions = [FrontMatter, Citations, MathSyntax, InlineNotes, Attributes, FencedDivs];

// Formatted text pasted from Word, Google Docs, LibreOffice or a web page (ARCHITECTURE.md §6.3):
// the clipboard's HTML becomes Pandoc Markdown with its headings, emphasis, lists, links and
// tables, instead of plain text. A plain function on a DOM tree, so it runs (and is tested)
// on any tree of nodes with nodeType, nodeName, childNodes, getAttribute and nodeValue.

const BLOCKS = new Set(["ADDRESS", "ARTICLE", "ASIDE", "BLOCKQUOTE", "BODY", "CAPTION", "CENTER", "DD", "DIV", "DL",
  "DT", "FIGCAPTION", "FIGURE", "FOOTER", "H1", "H2", "H3", "H4", "H5", "H6", "HEADER", "HR", "LI", "MAIN", "NAV", "OL",
  "P", "PRE", "SECTION", "TABLE", "TBODY", "TD", "TFOOT", "TH", "THEAD", "TR", "UL"]);
const SKIP = new Set(["BUTTON", "HEAD", "IFRAME", "INPUT", "LINK", "META", "NOSCRIPT", "OBJECT", "SCRIPT", "SELECT",
  "STYLE", "SVG", "TEMPLATE", "TEXTAREA", "TITLE", "VIDEO", "AUDIO", "CANVAS"]);
const ELEMENT = 1;
const TEXT = 3;

// Inline marks are written as private-use sentinels first, so that touching marks can be merged
// (<b>a</b><b>b</b> is **ab**, not **a****b**) and spaces moved outside them before they become
// Markdown. Each mark has one letter.
const OPEN = "";
const CLOSE = "";
const MARKS = { b: "**", i: "*", s: "~~", p: "^", d: "~" };

function styleOf(node) {
  const out = {};
  for (const part of (node.getAttribute?.("style") ?? "").split(";")) {
    const at = part.indexOf(":");
    if (at > 0) out[part.slice(0, at).trim().toLowerCase()] = part.slice(at + 1).trim().toLowerCase();
  }
  return out;
}

const isBlock = (node) => node.nodeType === ELEMENT && BLOCKS.has(node.nodeName);
const hasBlock = (node) => [...node.childNodes].some((c) => c.nodeType === ELEMENT && (isBlock(c) || hasBlock(c)));

function hasDescendant(node, names) {
  for (const c of node.childNodes) {
    if (c.nodeType === ELEMENT && (names.has(c.nodeName) || hasDescendant(c, names))) return true;
  }
  return false;
}

function textOf(node) {
  if (node.nodeType === TEXT) return node.nodeValue ?? "";
  if (node.nodeType !== ELEMENT || SKIP.has(node.nodeName)) return "";
  if (node.nodeName === "BR") return "\n";
  return [...node.childNodes].map(textOf).join("");
}

// Characters that would turn plain text into Markdown, escaped. Line starts are handled per block.
export function escapeText(text, { table = false } = {}) {
  let out = text
    .replace(/\\/g, "\\\\")
    .replace(/[*`]/g, "\\$&")
    .replace(/(^|[^\p{L}\p{N}])_|_(?=$|[^\p{L}\p{N}])/gu, (m) => m.replace("_", "\\_"))
    .replace(/\$(?=\S)/g, "\\$")
    .replace(/(^|[^\p{L}\p{N}.\-_+])@(?=[\p{L}\p{N}_])/gu, "$1\\@")
    .replace(/\^(?=\[)/g, "\\^")
    .replace(/~(?=\S)/g, "\\~")
    .replace(/<(?=[A-Za-z/!?])/g, "\\<")
    .replace(/\](?=[([{])/g, "\\]")
    .replace(/\[(?=\^)/g, "\\[");
  if (table) out = out.replace(/\|/g, "\\|");
  return out;
}

// The start of a paragraph line that Markdown would read as a heading, list item or quote.
function escapeLineStart(line) {
  return line
    .replace(/^(#{1,6})(?=\s|$)/, "\\$1")
    .replace(/^([-+*])(?=\s)/, "\\$1")
    .replace(/^(\d+)([.)])(?=\s)/, "$1\\$2")
    .replace(/^>/, "\\>")
    .replace(/^([-=*_]){3,}\s*$/, (m) => `\\${m}`)
    .replace(/^:(?=\s)/, "\\:")
    .replace(/^\|/, "\\|");
}

// Google's redirect links (https://www.google.com/url?q=…) lead to their target.
function cleanHref(href) {
  let url = (href ?? "").trim();
  try {
    const u = new URL(url);
    if (/(^|\.)google\.[a-z.]+$/.test(u.hostname) && u.pathname === "/url" && u.searchParams.get("q")) {
      url = u.searchParams.get("q");
    }
  } catch { /* relative or broken: dropped below */ }
  return /^(https?:|mailto:)/i.test(url) ? url : null;
}

function mergeMarks(text) {
  let prev;
  let out = text;
  do {
    prev = out;
    out = out
      .replace(/(.)\1/g, "") // empty
      .replace(/(.)\1/g, "") // touching: one span
      .replace(/(.)([ \t]+)/g, "$2$1") // spaces go outside the marks
      .replace(/([ \t]+)(.)/g, "$2$1");
  } while (out !== prev);
  return out;
}

function finishMarks(text) {
  return mergeMarks(text)
    // Sub- and superscripts can't hold spaces.
    .replace(/([pd])([^]*)\1/g, (_m, k, inner) => `${MARKS[k]}${inner.replace(/ /g, "\\ ")}${MARKS[k]}`)
    .replace(/[](.)/g, (_m, k) => MARKS[k] ?? "");
}

function codeSpan(text) {
  const runs = text.match(/`+/g) ?? [];
  const fence = "`".repeat(Math.max(0, ...runs.map((r) => r.length)) + 1);
  const pad = text.startsWith("`") || text.endsWith("`") ? " " : "";
  return `${fence}${pad}${text}${pad}${fence}`;
}

function isMono(style, name) {
  return name === "CODE" || name === "TT" || name === "KBD" || name === "SAMP"
    || /\b(mono|courier|consolas|menlo|monaco)/.test(style["font-family"] ?? "");
}

class Converter {
  constructor() {
    this.formatted = false; // whether anything beyond plain paragraphs came out
    this.images = 0; // pictures left out
  }

  // -- Inline -------------------------------------------------------------
  inline(node, ctx) {
    if (node.nodeType === TEXT) {
      const text = (node.nodeValue ?? "").replace(/[ \t\n\r\f ]+/g, " ");
      return ctx.code ? text : escapeText(text, ctx);
    }
    if (node.nodeType !== ELEMENT || SKIP.has(node.nodeName)) return "";
    const name = node.nodeName;
    const style = styleOf(node);
    if ((style["mso-list"] ?? "") === "ignore" || style.display === "none") return "";
    if (name === "BR") return ctx.table ? " " : "";
    if (name === "IMG") {
      this.images += 1;
      return "";
    }
    if (!ctx.code && isMono(style, name) && !ctx.pre) {
      const text = textOf(node).replace(/\s+/g, " ");
      if (!text.trim()) return text;
      this.formatted = true;
      return codeSpan(text);
    }
    if (name === "A") {
      const inner = this.children(node, ctx).trim();
      const href = cleanHref(node.getAttribute("href"));
      if (!href || !inner || ctx.link) return inner;
      this.formatted = true;
      const plain = finishMarks(inner).replace(/\\(.)/g, "$1");
      if (plain === href || `mailto:${plain}` === href) return `<${plain}>`;
      return `[${inner}](${href.replace(/[()\s]/g, (c) => `%${c.charCodeAt(0).toString(16).toUpperCase().padStart(2, "0")}`)})`;
    }
    if (name === "SUP" && [...node.childNodes].every((c) => c.nodeType === ELEMENT && c.nodeName === "A"
        && (c.getAttribute("href") ?? "").startsWith("#"))) {
      return ""; // a web page's footnote reference: [1] linking further down the page
    }
    const weight = style["font-weight"];
    const bold = name === "B" || name === "STRONG" || /^(bold|bolder|[6-9]00)$/.test(weight ?? "");
    const notBold = /^(normal|lighter|[1-4]00)$/.test(weight ?? "");
    const italic = name === "I" || name === "EM" || name === "CITE" || name === "DFN" || style["font-style"] === "italic";
    const notItalic = style["font-style"] === "normal";
    const strike = name === "S" || name === "DEL" || name === "STRIKE"
      || /line-through/.test(`${style["text-decoration"] ?? ""} ${style["text-decoration-line"] ?? ""}`);
    const sup = name === "SUP" || style["vertical-align"] === "super";
    const sub = name === "SUB" || style["vertical-align"] === "sub";
    const next = { ...ctx };
    const wrap = [];
    // Google Docs wraps a whole copy in <b style="font-weight:normal">; inner spans say what's bold.
    if (bold && !notBold && !ctx.b && !ctx.heading) { next.b = true; wrap.push("b"); }
    if (notBold && ctx.b) next.b = false;
    if (italic && !notItalic && !ctx.i) { next.i = true; wrap.push("i"); }
    if (strike && !ctx.s) { next.s = true; wrap.push("s"); }
    if (sup && !ctx.p) { next.p = true; wrap.push("p"); }
    if (sub && !ctx.d) { next.d = true; wrap.push("d"); }
    const inner = this.children(node, next);
    if (!wrap.length || !inner.trim()) return inner;
    this.formatted = true;
    return wrap.map((k) => OPEN + k).join("") + inner + wrap.reverse().map((k) => CLOSE + k).join("");
  }

  children(node, ctx) {
    return [...node.childNodes].map((c) => this.inline(c, ctx)).join("");
  }

  // A paragraph's text from inline Markdown with sentinels: marks finished, lines escaped.
  paragraph(raw) {
    const text = finishMarks(raw).replace(/ {2,}/g, " ").trim();
    if (!text.replace(//g, "").trim()) return "";
    return text.split(/ ? ?/).map((l) => escapeLineStart(l.trim())).filter((l, i, all) => l || i < all.length - 1)
      .join("\\\n").replace(/\\\n$/, "");
  }

  // -- Blocks -------------------------------------------------------------
  // The blocks of a node's children: runs of inline content become paragraphs.
  blocks(node, ctx) {
    const out = [];
    let run = "";
    const flush = () => {
      const p = this.paragraph(run);
      if (p) out.push(p);
      run = "";
    };
    const kids = [...node.childNodes];
    for (let i = 0; i < kids.length; i++) {
      const c = kids[i];
      if (c.nodeType === ELEMENT && wordListLevel(c) !== null) {
        flush();
        const items = [];
        while (i < kids.length) {
          const k = kids[i];
          if (k.nodeType === TEXT && !(k.nodeValue ?? "").trim()) { i += 1; continue; }
          const level = k.nodeType === ELEMENT ? wordListLevel(k) : null;
          if (level === null) break;
          items.push({ level, ordered: wordListOrdered(k), text: this.paragraph(this.children(k, ctx)) });
          i += 1;
        }
        i -= 1;
        out.push(flatList(items));
        this.formatted = true;
      } else if (c.nodeType === ELEMENT && !SKIP.has(c.nodeName) && (isBlock(c) || hasBlock(c))) {
        flush();
        out.push(...this.block(c, ctx));
      } else {
        run += this.inline(c, ctx);
      }
    }
    flush();
    return out.filter(Boolean);
  }

  block(node, ctx) {
    const name = node.nodeName;
    const style = styleOf(node);
    if (style.display === "none") return [];
    if (/^H[1-6]$/.test(name)) {
      const text = finishMarks(this.children(node, { ...ctx, heading: true })).replace(//g, " ")
        .replace(/\s+/g, " ").trim();
      if (!text) return [];
      this.formatted = true;
      return [`${"#".repeat(Number(name[1]))} ${text}`];
    }
    if (name === "UL" || name === "OL") return [this.list(node, ctx)].filter(Boolean);
    if (name === "BLOCKQUOTE") {
      const inner = this.blocks(node, ctx);
      if (!inner.length) return [];
      this.formatted = true;
      return [inner.join("\n\n").split("\n").map((l) => (l ? `> ${l}` : ">")).join("\n")];
    }
    if (name === "PRE") {
      const text = textOf(node).replace(/ /g, " ").replace(/\n+$/, "");
      if (!text.trim()) return [];
      this.formatted = true;
      const runs = text.match(/`{3,}/g) ?? [];
      const fence = "`".repeat(Math.max(2, ...runs.map((r) => r.length)) + 1);
      return [`${fence}\n${text}\n${fence}`];
    }
    if (name === "HR") {
      this.formatted = true;
      return ["* * *"];
    }
    if (name === "TABLE") return this.table(node, ctx);
    // An inline element around blocks, such as Google Docs' <b style="font-weight:normal"> around
    // a whole copy: the blocks inside say what's bold.
    return this.blocks(node, ctx);
  }

  list(node, ctx, depth = 0) {
    const ordered = node.nodeName === "OL";
    let n = Number(node.getAttribute("start")) || 1;
    const items = [];
    let loose = false;
    for (const c of node.childNodes) {
      if (c.nodeType !== ELEMENT) continue;
      if (c.nodeName === "UL" || c.nodeName === "OL") {
        // Google Docs nests a list straight inside the list: it belongs to the item before.
        const sub = this.list(c, ctx, depth + 1);
        if (!sub) continue;
        if (items.length) items[items.length - 1].push(sub);
        else items.push([sub]);
        continue;
      }
      if (c.nodeName !== "LI") continue;
      const parts = [];
      let run = "";
      const flush = () => {
        const p = this.paragraph(run);
        if (p) parts.push(p);
        run = "";
      };
      for (const k of c.childNodes) {
        if (k.nodeType === ELEMENT && (k.nodeName === "UL" || k.nodeName === "OL")) {
          flush();
          const sub = this.list(k, ctx, depth + 1);
          if (sub) parts.push(sub);
        } else if (k.nodeType === ELEMENT && (isBlock(k) || hasBlock(k))) {
          flush();
          parts.push(...this.block(k, ctx));
        } else {
          run += this.inline(k, ctx);
        }
      }
      flush();
      const paragraphs = parts.filter((p) => !/^(\s*)([-*+]|\d+[.)]) /.test(p));
      if (paragraphs.length > 1) loose = true;
      items.push(parts);
    }
    if (!items.length) return "";
    this.formatted = true;
    const out = items.map((parts) => {
      const marker = ordered ? `${n++}. ` : "- ";
      const pad = " ".repeat(marker.length);
      const body = parts.length ? parts : [""];
      const text = body.map((p, i) => {
        const nested = /^(\s*)([-*+]|\d+[.)]) /.test(p) && i > 0;
        const sep = i === 0 ? "" : nested && !loose ? "\n" : "\n\n";
        return sep + p.split("\n").map((l, j) => (i === 0 && j === 0 ? l : l ? pad + l : l)).join("\n");
      }).join("");
      return marker + text;
    });
    return out.join(loose ? "\n\n" : "\n");
  }

  table(node, ctx) {
    // Layout tables (web pages) and tables with lists or headings inside: their content, in order.
    if (hasDescendant(node, new Set(["TABLE", "UL", "OL", "PRE", "H1", "H2", "H3", "H4", "H5", "H6", "BLOCKQUOTE"]))) {
      const out = [];
      for (const row of rowsOf(node)) for (const cell of row) out.push(...this.blocks(cell, ctx));
      return out;
    }
    const rows = rowsOf(node).map((cells) => {
      const out = [];
      for (const cell of cells) {
        const text = this.paragraph(this.children(cell, { ...ctx, table: true })).replace(/\\\n/g, " ");
        out.push(text);
        const span = Math.min(20, Number(cell.getAttribute("colspan")) || 1);
        for (let k = 1; k < span; k++) out.push("");
      }
      return out;
    }).filter((r) => r.length);
    if (!rows.length) return [];
    const width = Math.max(...rows.map((r) => r.length));
    if (width === 1 && rows.length === 1) return [rows[0][0]].filter(Boolean);
    this.formatted = true;
    for (const r of rows) while (r.length < width) r.push("");
    const widths = Array.from({ length: width }, (_, i) => Math.max(3, ...rows.map((r) => r[i].length)));
    const line = (r) => `| ${r.map((c, i) => c.padEnd(widths[i])).join(" | ")} |`;
    const rule = `| ${widths.map((w) => "-".repeat(w)).join(" | ")} |`;
    return [[line(rows[0]), rule, ...rows.slice(1).map(line)].join("\n")];
  }
}

function rowsOf(table) {
  const rows = [];
  const walk = (node) => {
    for (const c of node.childNodes) {
      if (c.nodeType !== ELEMENT) continue;
      if (c.nodeName === "TR") rows.push([...c.childNodes].filter((k) => k.nodeType === ELEMENT && (k.nodeName === "TD" || k.nodeName === "TH")));
      else if (c.nodeName === "THEAD" || c.nodeName === "TBODY" || c.nodeName === "TFOOT") walk(c);
    }
  };
  walk(table);
  return rows;
}

// Word's lists are paragraphs with mso-list:l0 level2 lfo1, their bullet in a span to ignore.
function wordListLevel(node) {
  if (node.nodeName !== "P") return null;
  const m = /level(\d+)/.exec(styleOf(node)["mso-list"] ?? "");
  return m ? Math.max(1, Number(m[1])) : null;
}

function wordListOrdered(node) {
  let marker = "";
  const find = (n) => {
    for (const c of n.childNodes ?? []) {
      if (c.nodeType !== ELEMENT) continue;
      if ((styleOf(c)["mso-list"] ?? "") === "ignore") { marker = textOf(c).replace(/\s| /g, ""); return true; }
      if (find(c)) return true;
    }
    return false;
  };
  find(node);
  return /^(\(?[0-9]{1,3}|[a-zA-Z]|[ivxlcdm]{1,5})[.)]$/i.test(marker);
}

function flatList(items) {
  const counters = [];
  return items.map((item) => {
    counters.length = item.level;
    counters[item.level - 1] = (counters[item.level - 1] ?? 0) + 1;
    const indent = "    ".repeat(item.level - 1);
    const marker = item.ordered ? `${counters[item.level - 1]}. ` : "- ";
    return indent + marker + item.text.replace(/\n/g, `\n${indent}${" ".repeat(marker.length)}`);
  }).join("\n");
}

// Copies from code editors (VS Code puts white-space:pre on a div around everything) stay plain.
function fromCodeEditor(body) {
  const first = [...body.childNodes].find((c) => c.nodeType === ELEMENT && c.nodeName !== "META"
    && !(c.nodeType === TEXT && !(c.nodeValue ?? "").trim()));
  return Boolean(first && /^pre/.test(styleOf(first)["white-space"] ?? ""));
}

// The Markdown for a pasted HTML body, and whether it carried any formatting.
export function htmlToMarkdown(body) {
  const conv = new Converter();
  if (fromCodeEditor(body)) return { markdown: "", formatted: false, images: 0 };
  const markdown = conv.blocks(body, {}).join("\n\n").replace(/\n{3,}/g, "\n\n").trim();
  return { markdown, formatted: conv.formatted, images: conv.images };
}

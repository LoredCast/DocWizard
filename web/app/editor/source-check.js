// Checking quotations against their sources (ARCHITECTURE.md §10.5). Pure functions, shared by
// the source clips, the "Verify quotes" pass and the tests.
import { citationAt, findInPages, locatorOf, normalizeText, pageLabel } from "./cite-tools.js";

// The first page a locator names: "p. 42" → "42", "pp. 42–45" → "42", "S. iv" → "iv".
export function locatorPage(locator) {
  const loc = (locator ?? "").replace(/^\s*,/, "").trim();
  // Chapters, sections, paragraphs, lines and the like aren't pages.
  if (/^(?!pp?\b|pages?\b|S\b|Seiten?\b)[\p{L}§¶]{1,}\.?\s/u.test(loc) && !/^[ivxlcdm]+$/i.test(loc)) return null;
  const m = /(?:^|[\s.,])((?:\d+)|(?:[ivxlcdm]+))(?:\s*[-–]\s*(?:\d+|[ivxlcdm]+))?\s*(?:f{1,2}\.?)?\s*$/i
    .exec((locator ?? "").replace(/^\s*,/, " ").trim().replace(/^(pp?|S|Seiten?|pages?)\.?\s*/i, " "));
  return m ? m[1] : null;
}

// The PDF page (1-based) that carries printed page *printed*, the inverse of pageLabel().
export function pageIndexFor(printed, labels, offset, numPages) {
  if (!printed) return null;
  if (offset !== null && offset !== undefined && offset !== "") {
    const n = Number(printed);
    if (!Number.isInteger(n)) return null;
    const index = n - Number(offset);
    return index >= 1 && index <= numPages ? index : null;
  }
  if (Array.isArray(labels)) {
    const i = labels.findIndex((l) => l && l.toLowerCase() === String(printed).toLowerCase());
    if (i >= 0) return i + 1;
  }
  const n = Number(printed);
  return Number.isInteger(n) && n >= 1 && n <= numPages ? n : null;
}

// Every citation with the quotation before it: [{quote, from, to, citeFrom, citeTo, key, locator}].
// Quotations are text in quotation marks ending at most a few words before the citation.
export function quotations(text) {
  const out = [];
  const quote = /"([^"\n]{12,800})"|“([^”“\n]{12,800})[”“]|„([^“”\n]{12,800})[“”]|»([^«»\n]{12,800})«|«([^«»\n]{12,800})»/g;
  let m;
  while ((m = quote.exec(text))) {
    const end = m.index + m[0].length;
    // The citation follows within a short stretch, in the same sentence.
    const after = text.slice(end, end + 120);
    const at = /^[\s,;:.]*(?:\([^)]*\)\s*)?(?:[^.!?\n[]{0,40}?)?\[/.exec(after);
    if (!at) continue;
    const citation = citationAt(text, end + at[0].length);
    if (!citation || !citation.cites.length) continue;
    const cite = citation.cites.find((c) => !/^(fig|tbl|eq|sec):/.test(c.key));
    if (!cite) continue;
    out.push({
      quote: (m.slice(1).find(Boolean) ?? "").replace(/\s+/g, " ").trim(),
      from: m.index, to: end, citeFrom: citation.from, citeTo: citation.to,
      key: cite.key, locator: locatorOf(cite), cite,
    });
  }
  return out;
}

// Where *quote* is in a PDF's normalized page texts, against the page the citation names:
// {status: "verified" | "other-page" | "partial" | "not-found", page, snippet}.
export function verifyQuote(pages, quote, expected, labels = null, offset = null) {
  const cleaned = quote.replace(/\[(?:…|\.\.\.)\]|…|\.\.\./g, " ");
  const hits = findInPages(pages, cleaned, 20);
  if (!hits.length) return { status: "not-found", page: null, snippet: "" };
  const best = hits.find((h) => h.page === expected) ?? hits[0];
  const printed = pageLabel(best.page, labels, offset);
  if (best.partial) return { status: "partial", page: best.page, printed, snippet: best.snippet };
  if (expected && best.page !== expected) {
    return { status: "other-page", page: best.page, printed, snippet: best.snippet };
  }
  return { status: "verified", page: best.page, printed, snippet: best.snippet };
}

// About *radius* characters of a page's text around the quotation, or its beginning:
// {before, match, after}, all plain text.
export function excerpt(pageText, quote = "", radius = 280) {
  const text = pageText.replace(/\s+/g, " ").trim();
  if (quote) {
    const norm = normalizeText(text);
    const q = normalizeText(quote.replace(/\[(?:…|\.\.\.)\]|…/g, " ")).split(" ").slice(0, 8).join(" ");
    const at = q.length > 4 ? norm.indexOf(q) : -1;
    if (at >= 0 && norm.length === text.toLowerCase().length) {
      // Normalizing kept the length (no ligatures or hyphenation): offsets carry over.
      const len = Math.min(normalizeText(quote).length, text.length - at);
      return { before: (at > radius ? "…" : "") + text.slice(Math.max(0, at - radius), at),
               match: text.slice(at, at + len),
               after: text.slice(at + len, at + len + radius) + (at + len + radius < text.length ? "…" : "") };
    }
  }
  return { before: "", match: "", after: text.slice(0, radius * 2) + (text.length > radius * 2 ? "…" : "") };
}

// How a selection from a source goes into the text: “passage” [@key, p. N].
export function quoteMarkup(passage, key, locator, lang = "") {
  const clean = passage.replace(/(\p{L})-\s*\n\s*(\p{Ll})/gu, "$1$2").replace(/\s+/g, " ").trim();
  const de = /^de/i.test(lang);
  const open = de ? "„" : "“";
  const close = de ? "“" : "”";
  return `${open}${clean}${close} [@${key}${locator ? `, ${locator}` : ""}]`;
}

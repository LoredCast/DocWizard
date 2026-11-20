// The math palette (ARCHITECTURE.md §6.3): equations without knowing LaTeX. Fractions, roots,
// sums, matrices, aligned equations, Greek letters, operators and arrows, each a button that
// shows how it prints (Temml) and inserts its LaTeX with the first part to fill in selected.
// Outside math, an inline piece goes in as $…$ and an equation block as $$…$$ on its own lines.
import { EditorSelection, syntaxTree } from "codemirror";
import { el, t } from "../ui/dom.js";
import { loadTemml, renderMath } from "./math.js";

// ‹…› marks what is selected after inserting; block: an equation of its own.
export const MATH_GROUPS = [
  { id: "structures", label: "Structures", items: [
    { label: "Fraction", tex: "\\frac{‹a›}{b}", sample: "\\frac{a}{b}" },
    { label: "Square root", tex: "\\sqrt{‹x›}", sample: "\\sqrt{x}" },
    { label: "Root", tex: "\\sqrt[n]{‹x›}", sample: "\\sqrt[n]{x}" },
    { label: "Power", tex: "‹x›^{2}", sample: "x^{2}" },
    { label: "Subscript", tex: "‹x›_{i}", sample: "x_{i}" },
    { label: "Sum", tex: "\\sum_{‹i=1›}^{n}", sample: "\\sum_{i=1}^{n}" },
    { label: "Product", tex: "\\prod_{‹i=1›}^{n}", sample: "\\prod_{i=1}^{n}" },
    { label: "Integral", tex: "\\int_{‹a›}^{b} f(x)\\,dx", sample: "\\int_{a}^{b} f(x)\\,dx" },
    { label: "Limit", tex: "\\lim_{‹x \\to \\infty›}", sample: "\\lim_{x \\to \\infty}" },
    { label: "Derivative", tex: "\\frac{d‹f›}{dx}", sample: "\\frac{df}{dx}" },
    { label: "Partial derivative", tex: "\\frac{\\partial ‹f›}{\\partial x}", sample: "\\frac{\\partial f}{\\partial x}" },
    { label: "Binomial", tex: "\\binom{‹n›}{k}", sample: "\\binom{n}{k}" },
    { label: "Parentheses", tex: "\\left( ‹x› \\right)", sample: "\\left( \\frac{a}{b} \\right)" },
    { label: "Absolute value", tex: "\\lvert ‹x› \\rvert", sample: "\\lvert x \\rvert" },
    { label: "Vector", tex: "\\vec{‹v›}", sample: "\\vec{v}" },
    { label: "Mean (bar)", tex: "\\bar{‹x›}", sample: "\\bar{x}" },
    { label: "Estimate (hat)", tex: "\\hat{‹\\beta›}", sample: "\\hat{\\beta}" },
    { label: "Text in math", tex: "\\text{‹if ›}", sample: "x \\text{ if } y" },
  ] },
  { id: "equations", label: "Equations", items: [
    { label: "Equation", tex: "‹E› = mc^2", sample: "E = mc^2", block: true },
    { label: "Aligned equations", tex: "\\begin{aligned}\n‹a› &= b + c \\\\\n  &= d\n\\end{aligned}",
      sample: "\\begin{aligned} a &= b + c \\\\ &= d \\end{aligned}", block: true },
    { label: "Cases", tex: "f(x) = \\begin{cases}\n‹1› & \\text{if } x > 0 \\\\\n0 & \\text{otherwise}\n\\end{cases}",
      sample: "f(x) = \\begin{cases} 1 & x > 0 \\\\ 0 & \\text{else} \\end{cases}", block: true },
    { label: "Matrix 2 × 2", tex: "\\begin{pmatrix}\n‹a› & b \\\\\nc & d\n\\end{pmatrix}",
      sample: "\\begin{pmatrix} a & b \\\\ c & d \\end{pmatrix}", block: true },
    { label: "Matrix 3 × 3", tex: "\\begin{pmatrix}\n‹a› & b & c \\\\\nd & e & f \\\\\ng & h & i\n\\end{pmatrix}",
      sample: "\\begin{pmatrix} a & b & c \\\\ d & e & f \\\\ g & h & i \\end{pmatrix}", block: true },
    { label: "Determinant", tex: "\\begin{vmatrix}\n‹a› & b \\\\\nc & d\n\\end{vmatrix}",
      sample: "\\begin{vmatrix} a & b \\\\ c & d \\end{vmatrix}", block: true },
    { label: "Mean", tex: "‹\\bar{x}› = \\frac{1}{n} \\sum_{i=1}^{n} x_i",
      sample: "\\bar{x} = \\frac{1}{n} \\sum_{i=1}^{n} x_i", block: true },
    { label: "Quadratic formula", tex: "‹x› = \\frac{-b \\pm \\sqrt{b^2 - 4ac}}{2a}",
      sample: "x = \\frac{-b \\pm \\sqrt{b^2-4ac}}{2a}", block: true },
  ] },
  { id: "greek", label: "Greek letters", items: [
    ..."alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi pi rho sigma tau upsilon phi chi psi omega"
      .split(" ").map((n) => ({ label: n, tex: `\\${n}`, sample: `\\${n}` })),
    ..."Gamma Delta Theta Lambda Xi Pi Sigma Phi Psi Omega"
      .split(" ").map((n) => ({ label: n, tex: `\\${n}`, sample: `\\${n}` })),
    { label: "varepsilon", tex: "\\varepsilon", sample: "\\varepsilon" },
    { label: "varphi", tex: "\\varphi", sample: "\\varphi" },
  ] },
  { id: "symbols", label: "Operators and relations", items: [
    ["times", "\\times"], ["divided by", "\\div"], ["plus or minus", "\\pm"], ["dot", "\\cdot"],
    ["not equal", "\\neq"], ["approximately", "\\approx"], ["equivalent", "\\equiv"], ["proportional", "\\propto"],
    ["less or equal", "\\leq"], ["greater or equal", "\\geq"], ["much less", "\\ll"], ["much greater", "\\gg"],
    ["infinity", "\\infty"], ["partial", "\\partial"], ["nabla", "\\nabla"], ["degree", "^{\\circ}"],
    ["element of", "\\in"], ["not element of", "\\notin"], ["subset", "\\subseteq"], ["union", "\\cup"],
    ["intersection", "\\cap"], ["empty set", "\\emptyset"], ["for all", "\\forall"], ["exists", "\\exists"],
    ["and", "\\land"], ["or", "\\lor"], ["not", "\\neg"], ["dots", "\\ldots"],
    ["real numbers", "\\mathbb{R}"], ["natural numbers", "\\mathbb{N}"], ["integers", "\\mathbb{Z}"],
    ["expected value", "\\mathbb{E}"],
  ].map(([label, tex]) => ({ label, tex, sample: tex })) },
  { id: "arrows", label: "Arrows", items: [
    ["to", "\\to"], ["implies", "\\Rightarrow"], ["if and only if", "\\Leftrightarrow"], ["maps to", "\\mapsto"],
    ["left", "\\leftarrow"], ["up", "\\uparrow"], ["down", "\\downarrow"], ["long right", "\\longrightarrow"],
  ].map(([label, tex]) => ({ label, tex, sample: tex })) },
];

// The snippet without its ‹› marks, and where the marked part is.
export function parseSnippet(tex) {
  const open = tex.indexOf("‹");
  const close = tex.indexOf("›");
  if (open < 0 || close < open) return { text: tex, select: [tex.length, tex.length] };
  const text = tex.slice(0, open) + tex.slice(open + 1, close) + tex.slice(close + 1);
  return { text, select: [open, close - 1] };
}

function mathNodeAt(state, pos) {
  for (const side of [-1, 1]) {
    for (let cur = syntaxTree(state).resolveInner(pos, side); cur; cur = cur.parent) {
      if (cur.name === "InlineMath" || cur.name === "DisplayMath") return cur;
    }
  }
  return null;
}

// What inserting *item* at the selection changes: inside math only the LaTeX; outside, the
// LaTeX in $…$, or in $$…$$ on lines of its own for an equation (block items, or *display*).
export function mathInsertion(state, item, { display = null } = {}) {
  const range = state.selection.main;
  const { text, select } = parseSnippet(item.tex);
  const node = mathNodeAt(state, range.from);
  const inside = node && node.from < range.from && range.to < node.to;
  let insert = text;
  let offset = 0;
  if (!inside) {
    const block = display ?? Boolean(item.block);
    if (block) {
      const before = state.sliceDoc(Math.max(0, range.from - 2), range.from);
      const after = state.sliceDoc(range.to, range.to + 2);
      const lead = range.from === 0 || before.endsWith("\n\n") ? "" : before.endsWith("\n") ? "\n" : "\n\n";
      const trail = range.to === state.doc.length || after.startsWith("\n\n") ? "" : after.startsWith("\n") ? "\n" : "\n\n";
      insert = `${lead}$$\n${text}\n$$${trail}`;
      offset = lead.length + 3;
    } else {
      insert = `$${text}$`;
      offset = 1;
    }
  }
  const from = range.from + offset + select[0];
  const to = range.from + offset + select[1];
  return { changes: { from: range.from, to: range.to, insert }, selection: EditorSelection.range(from, to) };
}

export function insertMath(view, item, options) {
  const spec = mathInsertion(view.state, item, options);
  view.dispatch({ ...spec, scrollIntoView: true, userEvent: "input" });
}

// The palette's content: a search field, the groups as tabs and a grid of buttons.
export function mathPalette({ onPick }) {
  const tabs = el("div", { class: "dw-math-tabs", role: "tablist", "aria-label": t("Kinds of math") });
  const grid = el("div", { class: "dw-math-grid", role: "group" });
  const search = el("input", { type: "search", class: "dw-math-search", placeholder: t("Search: fraction, alpha, matrix…"),
                               "aria-label": t("Search math"), autocomplete: "off", spellcheck: "false" });
  const display = el("input", { type: "checkbox", class: "dw-math-display" });
  let current = MATH_GROUPS[0].id;
  const all = MATH_GROUPS.flatMap((g) => g.items.map((item) => ({ ...item, group: g.id })));

  const show = () => {
    const q = search.value.trim().toLowerCase();
    const items = q ? all.filter((i) => i.label.toLowerCase().includes(q) || i.tex.toLowerCase().includes(q))
      : all.filter((i) => i.group === current);
    grid.replaceChildren(...items.map((item) => {
      const face = el("span", { class: "dw-math-face" }, item.sample);
      const button = el("button", { type: "button", class: "dw-math-item", title: t(item.label),
                                    "aria-label": t(item.label),
                                    onclick: () => onPick(item, display.checked ? true : null) }, face);
      loadTemml().then((temml) => {
        if (temml) face.replaceChildren(renderMath(item.sample, false));
      });
      return button;
    }));
    if (!items.length) grid.append(el("p", { class: "muted" }, t("Nothing found.")));
    for (const b of tabs.children) b.setAttribute("aria-selected", String(!q && b.dataset.group === current));
  };
  for (const g of MATH_GROUPS) {
    tabs.append(el("button", { type: "button", role: "tab", "data-group": g.id, "aria-selected": "false",
                               onclick: () => { current = g.id; search.value = ""; show(); } }, t(g.label)));
  }
  search.addEventListener("input", show);
  search.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      grid.querySelector("button")?.click();
    }
  });
  show();
  return el("div", { class: "dw-math-palette" }, search, tabs, grid,
    el("label", { class: "choice dw-math-mode" }, display, t("As an equation on its own line")),
    el("p", { class: "muted hint" }, t("Pick a piece to insert it; the selected part is yours to type over. Inside $…$ only the formula is added.")));
}

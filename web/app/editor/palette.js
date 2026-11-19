// The command palette (ARCHITECTURE.md §6.3): Ctrl/Cmd+K in the editor opens one list of
// everything there is to go to or do (the project's documents, the document's headings, page
// setup, actions, formatting and insert tools), filtered as you type. Arrow keys choose, Enter runs.
//
// - "#" lists only headings, ">" only actions; ":12" goes to line 12.
// - With nothing typed, the commands used last come first.
//
// No imports, so the matching can be tested in Node.

const RECENT_KEY = "dw.palette.recent";
const MAX_SHOWN = 60;

// Lower case without accents, one character for one, so match positions are those of the text.
export function fold(text) {
  let out = "";
  for (const ch of String(text)) {
    const base = ch.normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase();
    out += (base[0] ?? ch).padEnd(ch.length, " ");
  }
  return out;
}

const isBoundary = (text, i) => i === 0 || /[\s\-_/.:([]/.test(text[i - 1])
  || (text[i - 1] === text[i - 1].toLowerCase() && text[i] !== text[i].toLowerCase());

// How well *query* matches *text*: null for no match, else {score, at} with the positions of the
// matched characters. Each word of the query must appear in order as a subsequence; whole words,
// word starts and runs of characters count more, and so does an early match.
export function score(query, text) {
  const q = fold(query).trim();
  if (!q) return { score: 0, at: [] };
  const t = fold(text);
  // A plain substring is the best kind of match.
  const sub = t.indexOf(q);
  if (sub >= 0) {
    const at = [...Array(q.length).keys()].map((k) => sub + k);
    return { score: 1000 - sub + (isBoundary(text, sub) ? 300 : 0) + (sub === 0 ? 200 : 0) - t.length / 10, at };
  }
  let total = 0;
  const at = [];
  let from = 0;
  for (const word of q.split(/\s+/)) {
    // Each word: a substring if it is one, else a subsequence that prefers word starts.
    const w = t.indexOf(word, from);
    if (w >= 0) {
      for (let k = 0; k < word.length; k++) at.push(w + k);
      total += 200 + (isBoundary(text, w) ? 100 : 0) - w;
      from = w + word.length;
      continue;
    }
    let pos = from;
    let run = 0;
    for (const ch of word) {
      let found = -1;
      // A word start further on beats the next occurrence in the middle of a word.
      for (let i = pos; i < t.length; i++) {
        if (t[i] === ch && isBoundary(text, i)) { found = i; break; }
      }
      const next = t.indexOf(ch, pos);
      if (next >= 0 && (found < 0 || next === pos)) found = next;
      if (found < 0) return null;
      run = found === pos && at.length ? run + 1 : 0;
      total += 10 + run * 15 + (isBoundary(text, found) ? 30 : 0) - Math.min(20, found - pos);
      at.push(found);
      pos = found + 1;
    }
    from = pos;
  }
  return { score: total - t.length / 10, at };
}

// The items matching *query*, best first: [{item, at}]. An item matches on its label, or less
// well on its group, detail and keywords.
export function rank(items, query, limit = MAX_SHOWN) {
  const out = [];
  for (const item of items) {
    const onLabel = score(query, item.label);
    const extra = [item.group, item.detail, item.keywords].filter(Boolean).join(" ");
    const onExtra = onLabel ? null : score(query, `${item.label} ${extra}`);
    const best = onLabel ?? (onExtra ? { score: onExtra.score - 400, at: [] } : null);
    if (best) out.push({ item, at: best.at, score: best.score + (item.boost ?? 0) });
  }
  out.sort((a, b) => b.score - a.score);
  return out.slice(0, limit);
}

export class CommandPalette {
  // *sources*: () => items, each {id, label, group, detail?, keywords?, hint?, kind?, run}. kind is
  // "heading" or "action" for the # and > filters. *goToLine*(n) for ":n". *labels*: interface text.
  constructor({ sources, goToLine = null, labels = {} }) {
    this.sources = sources;
    this.goToLine = goToLine;
    this.labels = { placeholder: "Go to a file or heading, or type a command…", empty: "Nothing matches.",
                    line: "Go to line {n}", recent: "Recent", title: "Command palette", ...labels };
    this.dialog = null;
    this.results = [];
    this.active = 0;
  }

  get isOpen() { return Boolean(this.dialog?.open); }

  build() {
    const dialog = document.createElement("dialog");
    dialog.className = "palette";
    dialog.setAttribute("aria-label", this.labels.title);
    const input = document.createElement("input");
    input.type = "text";
    input.className = "palette-input";
    input.placeholder = this.labels.placeholder;
    input.autocomplete = "off";
    input.spellcheck = false;
    // The palette is for typing: its field takes the focus on touch screens too (ui/keyboard.js).
    input.autofocus = true;
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-expanded", "true");
    input.setAttribute("aria-controls", "palette-list");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-label", this.labels.title);
    const list = document.createElement("ul");
    list.className = "palette-list";
    list.id = "palette-list";
    list.setAttribute("role", "listbox");
    const empty = document.createElement("p");
    empty.className = "palette-empty muted";
    empty.textContent = this.labels.empty;
    empty.hidden = true;
    dialog.append(input, list, empty);
    document.body.append(dialog);
    input.addEventListener("input", () => this.update());
    input.addEventListener("keydown", (event) => this.onKey(event));
    list.addEventListener("mousemove", (event) => {
      const li = event.target.closest("li[data-index]");
      if (li && Number(li.dataset.index) !== this.active) this.select(Number(li.dataset.index), false);
    });
    list.addEventListener("click", (event) => {
      const li = event.target.closest("li[data-index]");
      if (li) this.run(Number(li.dataset.index));
    });
    // A click on the backdrop closes it.
    dialog.addEventListener("click", (event) => { if (event.target === dialog) this.close(); });
    dialog.addEventListener("close", () => { this.restore?.focus?.({ preventScroll: true }); });
    Object.assign(this, { dialog, input, list, empty });
  }

  open(text = "") {
    if (!this.dialog) this.build();
    if (this.isOpen) {
      this.input.select();
      return;
    }
    this.restore = document.activeElement;
    this.items = this.sources();
    this.input.value = text;
    this.dialog.showModal();
    this.input.focus();
    this.update();
  }

  close() {
    if (this.isOpen) this.dialog.close();
  }

  toggle() {
    if (this.isOpen) this.close();
    else this.open();
  }

  recent() {
    try { return JSON.parse(localStorage.getItem(RECENT_KEY) ?? "[]"); } catch { return []; }
  }

  remember(id) {
    if (!id) return;
    const list = [id, ...this.recent().filter((x) => x !== id)].slice(0, 8);
    try { localStorage.setItem(RECENT_KEY, JSON.stringify(list)); } catch { /* private window */ }
  }

  update() {
    const raw = this.input.value;
    let query = raw.trim();
    let items = this.items;
    if (query.startsWith("#")) {
      items = items.filter((i) => i.kind === "heading");
      query = query.slice(1);
    } else if (query.startsWith(">")) {
      items = items.filter((i) => i.kind === "action");
      query = query.slice(1);
    }
    let results;
    const line = /^:(\d+)$/.exec(query);
    if (line && this.goToLine) {
      const n = Number(line[1]);
      results = [{ item: { id: "", label: this.labels.line.replace("{n}", String(n)), group: "",
                           run: () => this.goToLine(n) }, at: [] }];
    } else if (!query.trim()) {
      // Nothing typed: the ones used last, then everything in its order.
      const recent = this.recent();
      const byId = new Map(items.map((i) => [i.id, i]));
      const first = recent.map((id) => byId.get(id)).filter(Boolean)
        .map((item) => ({ item, at: [], recent: true }));
      const seen = new Set(first.map((r) => r.item.id));
      results = [...first, ...items.filter((i) => !seen.has(i.id)).map((item) => ({ item, at: [] }))]
        .slice(0, MAX_SHOWN * 2);
    } else {
      results = rank(items, query);
    }
    this.results = results;
    this.render();
    this.select(0, true);
  }

  render() {
    const nodes = this.results.map((r, i) => {
      const li = document.createElement("li");
      li.id = `palette-item-${i}`;
      li.dataset.index = String(i);
      li.setAttribute("role", "option");
      const label = document.createElement("span");
      label.className = "palette-label";
      label.append(...highlight(r.item.label, r.at));
      li.append(label);
      if (r.item.detail) {
        const detail = document.createElement("span");
        detail.className = "palette-detail muted";
        detail.textContent = r.item.detail;
        li.append(detail);
      }
      const side = document.createElement("span");
      side.className = "palette-group muted";
      side.textContent = [r.recent ? this.labels.recent : r.item.group, r.item.hint].filter(Boolean).join(" · ");
      li.append(side);
      return li;
    });
    this.list.replaceChildren(...nodes);
    this.empty.hidden = nodes.length > 0;
    this.list.hidden = nodes.length === 0;
  }

  select(index, scroll = true) {
    const n = this.results.length;
    if (!n) {
      this.input.removeAttribute("aria-activedescendant");
      return;
    }
    this.active = (index + n) % n;
    for (const li of this.list.children) li.setAttribute("aria-selected", String(Number(li.dataset.index) === this.active));
    const li = this.list.children[this.active];
    this.input.setAttribute("aria-activedescendant", li.id);
    if (scroll) li.scrollIntoView({ block: "nearest" });
  }

  onKey(event) {
    const page = Math.max(1, Math.floor(this.list.clientHeight / 32) - 1);
    if (event.key === "ArrowDown" || (event.ctrlKey && event.key === "n")) this.select(this.active + 1);
    else if (event.key === "ArrowUp" || (event.ctrlKey && event.key === "p")) this.select(this.active - 1);
    else if (event.key === "PageDown") this.select(Math.min(this.results.length - 1, this.active + page));
    else if (event.key === "PageUp") this.select(Math.max(0, this.active - page));
    else if (event.key === "Enter") this.run(this.active);
    else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") this.close();
    else return;
    event.preventDefault();
  }

  run(index) {
    const r = this.results[index];
    if (!r) return;
    this.remember(r.item.id);
    this.restore = null; // the command decides where the focus goes
    this.dialog.close();
    // After the dialog is gone, so dialogs and popovers the command opens get the focus.
    setTimeout(() => r.item.run(), 0);
  }
}

// The label with the matched characters in <mark>.
function highlight(text, at) {
  if (!at?.length) return [text];
  const marks = new Set(at);
  const out = [];
  let buf = "";
  let on = false;
  [...text].map((ch, k, all) => [ch, all.slice(0, k).join("").length]).forEach(([ch, i]) => {
    const m = marks.has(i);
    if (m !== on && buf) {
      out.push(on ? Object.assign(document.createElement("mark"), { textContent: buf }) : buf);
      buf = "";
    }
    on = m;
    buf += ch;
  });
  if (buf) out.push(on ? Object.assign(document.createElement("mark"), { textContent: buf }) : buf);
  return out;
}

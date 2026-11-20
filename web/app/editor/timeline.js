// The drafting timeline (ARCHITECTURE.md §8.5): a slider over the document's history that shows
// the text at each point, with what was written since the point before marked in its author's
// color. Play steps through it; the arrow keys move the slider one point at a time.
import { api, el, t, toast } from "../ui/dom.js";

const PLAY_MS = 700;

export async function openTimeline({ docId, name }) {
  let data;
  try {
    data = await api("GET", `/api/documents/${docId}/timeline`);
  } catch (err) {
    toast(err.message, { error: true });
    return;
  }
  const points = data.points;
  const fmt = new Intl.DateTimeFormat(document.documentElement.lang || undefined, { dateStyle: "medium", timeStyle: "short" });
  const parseDate = (iso) => new Date(iso.endsWith("Z") ? iso : `${iso}Z`);
  // A color per person, from the presence colors.
  const people = [...new Set([...data.people.map((p) => p.name), ...points.flatMap((p) => p.authors)])];
  const color = (who) => `var(--peer-${Math.max(0, people.indexOf(who)) % 8})`;

  const dialog = el("dialog", { class: "wide timeline-dialog", "aria-labelledby": "timeline-title" });
  const slider = el("input", { type: "range", min: "0", max: String(points.length - 1), step: "1",
    value: String(points.length - 1), "aria-label": t("Point in the document's history") });
  const when = el("p", { class: "timeline-when", "aria-live": "polite" });
  const text = el("pre", { class: "timeline-text", tabindex: "0", "aria-label": t("The text at this point") });
  const play = el("button", { type: "button", "aria-pressed": "false" }, t("Play"));
  const prev = el("button", { type: "button", "aria-label": t("Previous point") }, "←");
  const next = el("button", { type: "button", "aria-label": t("Next point") }, "→");
  const legend = el("ul", { class: "timeline-people" }, data.people.map((p) => el("li", {},
    el("span", { class: "dot", style: `--peer: ${color(p.name)}`, "aria-hidden": "true" }),
    t("{name}: {inserted} characters written, {deleted} removed",
      { name: p.name, inserted: p.inserted.toLocaleString(), deleted: p.deleted.toLocaleString() }))));

  const cache = new Map();
  let shown = -1;
  let timer = null;
  let playing = null;

  function label(p) {
    const parts = [p.kind === "now" ? t("Now") : fmt.format(parseDate(p.at))];
    if (p.label) parts.push(p.label);
    if (p.authors.length) parts.push(p.authors.join(", "));
    return parts.join(" · ");
  }

  async function load(i) {
    if (!cache.has(i)) {
      const since = i > 0 ? `?since=${points[i - 1].version}` : "";
      cache.set(i, api("GET", `/api/documents/${docId}/timeline/${points[i].version}${since}`).catch((err) => {
        cache.delete(i);
        throw err;
      }));
    }
    return cache.get(i);
  }

  async function show(i) {
    const p = points[i];
    slider.setAttribute("aria-valuetext", label(p));
    when.textContent = `${t("{n} of {total}", { n: i + 1, total: points.length })}: ${label(p)}`;
    prev.disabled = i === 0;
    next.disabled = i === points.length - 1;
    let out;
    try {
      out = await load(i);
    } catch (err) {
      text.textContent = err.message;
      return;
    }
    if (Number(slider.value) !== i) return;
    shown = i;
    const parts = [];
    let at = 0;
    for (const m of out.marks) {
      if (m.from < at) continue;
      parts.push(out.text.slice(at, m.from));
      parts.push(el("mark", { class: "timeline-ins", style: `--peer: ${color(m.author)}`,
        title: m.author ? t("Written by {name}", { name: m.author }) : t("Written since the point before") },
        out.text.slice(m.from, m.to)));
      at = m.to;
    }
    parts.push(out.text.slice(at));
    text.replaceChildren(...parts);
    text.querySelector("mark")?.scrollIntoView({ block: "center" });
    // The next point is fetched ahead, so playing doesn't wait.
    if (i + 1 < points.length) load(i + 1).catch(() => {});
  }

  function go(i) {
    slider.value = String(Math.max(0, Math.min(points.length - 1, i)));
    clearTimeout(timer);
    timer = setTimeout(() => show(Number(slider.value)), 80);
  }

  function stop() {
    clearInterval(playing);
    playing = null;
    play.textContent = t("Play");
    play.setAttribute("aria-pressed", "false");
  }

  play.addEventListener("click", () => {
    if (playing) {
      stop();
      return;
    }
    if (Number(slider.value) >= points.length - 1) go(0);
    play.textContent = t("Pause");
    play.setAttribute("aria-pressed", "true");
    playing = setInterval(() => {
      const i = Number(slider.value);
      if (i >= points.length - 1) stop();
      else if (shown === i) go(i + 1);
    }, PLAY_MS);
  });
  slider.addEventListener("input", () => { stop(); go(Number(slider.value)); });
  prev.addEventListener("click", () => { stop(); go(Number(slider.value) - 1); });
  next.addEventListener("click", () => { stop(); go(Number(slider.value) + 1); });

  dialog.append(
    el("h2", { id: "timeline-title" }, t("Drafting timeline")),
    el("p", { class: "muted" }, t("How {name} came about. Kept: every edit of the last days, older history as snapshots.", { name })),
    el("div", { class: "timeline-controls" }, prev, slider, next, play),
    when, text,
    data.people.length ? legend : null,
    el("div", { class: "buttons" }, el("button", { type: "button", onclick: () => dialog.close() }, t("Close"))));
  dialog.addEventListener("close", () => { stop(); dialog.remove(); });
  document.body.append(dialog);
  dialog.showModal();
  slider.focus();
  show(points.length - 1);
}

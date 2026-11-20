// The writing helper (ARCHITECTURE.md §8.10): the server's language model proposes a change to a
// passage, and the proposal arrives as an ordinary suggestion to accept or reject. The text is
// never changed here.
import { api, el, t, toast } from "../ui/dom.js";

const TASKS = [
  ["proofread", "Correct spelling and grammar"],
  ["clearer", "Make it clearer"],
  ["shorter", "Make it shorter"],
  ["simpler", "Use simpler language"],
  ["formal", "Make it more formal"],
  ["custom", "Something else"],
];

// The selection, or else the paragraph at the cursor.
function passage(state) {
  const sel = state.selection.main;
  if (!sel.empty) return { from: sel.from, to: sel.to };
  const doc = state.doc;
  let first = doc.lineAt(sel.head).number;
  let last = first;
  if (!doc.line(first).text.trim()) return null;
  while (first > 1 && doc.line(first - 1).text.trim()) first--;
  while (last < doc.lines && doc.line(last + 1).text.trim()) last++;
  return { from: doc.line(first).from, to: doc.line(last).to };
}

export function openAssist({ view, session, docId, maxChars = 6000, onSuggested }) {
  const range = passage(view.state);
  if (!range) {
    toast(t("Select a passage, or put the cursor in a paragraph, for the helper to work on."));
    return;
  }
  const text = view.state.sliceDoc(range.from, range.to);
  if (text.length > maxChars) {
    toast(t("Select at most {n} characters at a time.", { n: maxChars.toLocaleString() }), { error: true });
    return;
  }
  const dialog = el("dialog", { class: "assist-dialog", "aria-labelledby": "assist-title" });
  const custom = el("input", { type: "text", name: "instruction", maxlength: "500", disabled: "",
    placeholder: t("For example: use British spelling") });
  const status = el("p", { role: "status", class: "muted" });
  const form = el("form", { method: "dialog" },
    el("h2", { id: "assist-title" }, t("Writing helper")),
    el("blockquote", { class: "assist-passage" }, text.length > 400 ? `${text.slice(0, 400)}…` : text),
    el("fieldset", {}, el("legend", {}, t("What should it do?")),
      ...TASKS.map(([value, label], i) => el("label", { class: "choice" },
        el("input", { type: "radio", name: "task", value, ...(i === 0 ? { checked: "" } : {}) }), t(label)))),
    el("label", {}, t("Instruction"), custom),
    el("p", { class: "muted hint" }, t("The answer becomes a suggestion that you or an editor can accept or reject.")),
    status,
    el("div", { class: "buttons" },
      el("button", { type: "button", onclick: () => dialog.close() }, t("Cancel")),
      el("button", { type: "submit", class: "primary", value: "ask" }, t("Suggest a change"))));
  form.addEventListener("change", () => {
    const task = form.elements.task.value;
    custom.disabled = task !== "custom";
    custom.required = task === "custom";
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = form.querySelector('button[type="submit"]');
    submit.disabled = true;
    status.textContent = t("Asking the helper…");
    try {
      for (let i = 0; i < 50 && session.unconfirmed() > 0; i++) await new Promise((r) => setTimeout(r, 100));
      // The passage may have moved while waiting: find it again from the cursor's place.
      const mapped = session.toSynced([[range.from, range.to]]);
      if (!mapped) throw new Error(t("The document changed. Try again."));
      const out = await api("POST", `/api/documents/${docId}/assist`, {
        text, from: mapped[0][0], to: mapped[0][1], version: session.version(),
        task: form.elements.task.value, instruction: custom.value.trim(),
      });
      dialog.close();
      if (out.unchanged) toast(t("The helper found nothing to change."));
      else {
        toast(t("The helper's suggestion is in the comments."));
        onSuggested?.(out);
      }
    } catch (err) {
      status.textContent = err.message;
      submit.disabled = false;
    }
  });
  dialog.append(form);
  dialog.addEventListener("close", () => { dialog.remove(); view.focus(); });
  document.body.append(dialog);
  dialog.showModal();
}

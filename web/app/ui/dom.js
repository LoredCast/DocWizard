// Small DOM and fetch helpers shared by every page.

import { currentOwner, forgetAccount } from "../store/idb.js";

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function csrfToken() {
  return document.querySelector('meta[name="csrf-token"]')?.content ?? "";
}

export async function api(method, url, body, { raw = false } = {}) {
  const headers = { "X-CSRF-Token": csrfToken() };
  let payload = body;
  if (body !== undefined && !raw) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const res = await fetch(url, { method, headers, body: payload, credentials: "same-origin" });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const message = data?.error?.message ?? `The request failed (${res.status}).`;
    throw new Error(message);
  }
  return data;
}

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

let toastTimer = null;
export function toast(message, { error = false } = {}) {
  document.querySelector(".toast")?.remove();
  clearTimeout(toastTimer);
  const node = el("div", { class: error ? "toast error" : "toast", role: error ? "alert" : "status" }, message);
  if (error) {
    node.append(" ", el("button", { type: "button", class: "link", onclick: () => node.remove() }, "Dismiss"));
  } else {
    toastTimer = setTimeout(() => node.remove(), 4000);
  }
  document.body.append(node);
}

export function confirmDialog(message, actionLabel = "Delete") {
  return new Promise((resolve) => {
    const finish = (value) => { dialog.close(); resolve(value); };
    const dialog = el("dialog", {},
      el("p", {}, message),
      el("div", { class: "buttons" },
        el("button", { type: "button", onclick: () => finish(false) }, "Cancel"),
        el("button", { type: "button", class: "danger", onclick: () => finish(true) }, actionLabel)));
    dialog.addEventListener("close", () => { dialog.remove(); resolve(false); });
    document.body.append(dialog);
    dialog.showModal();
  });
}

// Shared behaviour: dialogs, clickable rows, confirmations.
export function wireCommon(root = document) {
  registerServiceWorker();
  root.addEventListener("click", (event) => {
    const open = event.target.closest("[data-open-dialog]");
    if (open) {
      document.getElementById(open.dataset.openDialog)?.showModal();
      return;
    }
    const close = event.target.closest("[data-close-dialog]");
    if (close) {
      close.closest("dialog")?.close();
      return;
    }
    const row = event.target.closest("tr[data-href]");
    if (row && !event.target.closest("a, button, input")) {
      location.href = row.dataset.href;
    }
  });
  root.addEventListener("focusin", (event) => {
    if (event.target.matches?.("[data-select-on-focus]")) event.target.select();
  });
  root.addEventListener("submit", async (event) => {
    const form = event.target;
    if (form.getAttribute("action") === "/logout" && !form.dataset.forgotten) {
      // Nothing of this account stays in the browser, except edits still on their way.
      event.preventDefault();
      await forgetAccount();
      form.dataset.forgotten = "1";
      form.requestSubmit();
      return;
    }
    const ask = event.submitter?.dataset.confirm ?? form.dataset.confirm;
    if (ask && !form.dataset.confirmed) {
      event.preventDefault();
      const submitter = event.submitter;
      if (await confirmDialog(ask)) {
        form.dataset.confirmed = "1";
        form.requestSubmit(submitter?.form === form ? submitter : undefined);
        delete form.dataset.confirmed;
      }
    }
  });
}

// Signed-in pages install the service worker that keeps the app usable offline (§6.5).
function registerServiceWorker() {
  if (!currentOwner() || !document.querySelector('meta[name="dw-user"]')) return;
  if (!("serviceWorker" in navigator)) return;
  navigator.serviceWorker.register("/sw.js").catch(() => { /* e.g. an untrusted certificate */ });
}

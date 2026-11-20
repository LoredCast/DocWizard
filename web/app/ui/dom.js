// Small DOM and fetch helpers shared by every page.

import { currentOwner, forgetAccount } from "../store/idb.js";
import { wireKeyboard } from "./keyboard.js";

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function csrfToken() {
  return document.querySelector('meta[name="csrf-token"]')?.content ?? "";
}

// Interface strings (§21): the page carries its language's catalog; English is the source text.
let catalog = null;
export function t(message, values = {}) {
  if (catalog === null) {
    try { catalog = JSON.parse(document.getElementById("i18n")?.textContent ?? "{}"); } catch { catalog = {}; }
  }
  const text = catalog[message] ?? message;
  return text.replace(/\{(\w+)\}/g, (m, name) => (name in values ? String(values[name]) : m));
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
    const message = data?.error?.message ?? t("The request failed ({status}).", { status: res.status });
    const error = new Error(message);
    error.code = data?.error?.code;
    error.status = res.status;
    throw error;
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
    node.append(" ", el("button", { type: "button", class: "link", onclick: () => node.remove() }, t("Dismiss")));
  } else {
    toastTimer = setTimeout(() => node.remove(), 4000);
  }
  document.body.append(node);
}

export function confirmDialog(message, actionLabel = t("Delete")) {
  return new Promise((resolve) => {
    const finish = (value) => { dialog.close(); resolve(value); };
    const dialog = el("dialog", {},
      el("p", {}, message),
      el("div", { class: "buttons" },
        el("button", { type: "button", onclick: () => finish(false) }, t("Cancel")),
        el("button", { type: "button", class: "danger", onclick: () => finish(true) }, actionLabel)));
    dialog.addEventListener("close", () => { dialog.remove(); resolve(false); });
    document.body.append(dialog);
    dialog.showModal();
  });
}

// Menus inside horizontal-scroll tables (project files, admin tables) would be clipped by their
// overflow container if left position: absolute; float them over the viewport anchored to <summary>.
function hasOverflowAncestor(node) {
  for (let p = node.parentElement; p && p !== document.body && p !== document.documentElement; p = p.parentElement) {
    const s = getComputedStyle(p);
    if (s.overflowX !== "visible" || s.overflowY !== "visible") return true;
  }
  return false;
}

function clearFloating(details) {
  delete details.dataset.floating;
  const body = details.querySelector(":scope > .menu-body");
  if (!body) return;
  for (const prop of ["position", "top", "left", "right", "bottom", "maxHeight", "overflowY", "zIndex"]) {
    body.style[prop] = "";
  }
}

function placeMenu(details) {
  const summary = details.querySelector(":scope > summary");
  const body = details.querySelector(":scope > .menu-body");
  if (!summary || !body) return;
  const r = summary.getBoundingClientRect();
  const vv = window.visualViewport;
  const vw = vv?.width ?? window.innerWidth;
  const vh = vv?.height ?? window.innerHeight;
  const vLeft = vv?.offsetLeft ?? 0;
  const vTop = vv?.offsetTop ?? 0;
  const pad = 8;
  if ((r.bottom < vTop || r.top > vTop + vh || r.right < vLeft || r.left > vLeft + vw)
      && !details.contains(document.activeElement)) {
    details.open = false;
    return;
  }
  body.style.position = "fixed";
  body.style.right = "auto";
  body.style.bottom = "auto";
  body.style.zIndex = "30";
  body.style.maxHeight = `${Math.max(120, vh - 2 * pad)}px`;
  body.style.overflowY = "";
  const bw = body.offsetWidth;
  const bh = body.offsetHeight;
  const left = Math.max(vLeft + pad, Math.min(r.right - bw, vLeft + vw - bw - pad));
  const spaceBelow = vTop + vh - r.bottom - pad - 4;
  const spaceAbove = r.top - vTop - pad - 4;
  let top;
  let avail;
  if (bh <= spaceBelow || spaceBelow >= spaceAbove) {
    avail = Math.max(120, spaceBelow);
    top = Math.max(vTop + pad, Math.min(r.bottom + 4, vTop + vh - Math.min(bh, avail) - pad));
  } else {
    avail = Math.max(120, spaceAbove);
    top = Math.max(vTop + pad, r.top - Math.min(bh, avail) - 4);
  }
  body.style.maxHeight = `${avail}px`;
  body.style.overflowY = bh > avail ? "auto" : "";
  body.style.left = `${Math.round(left)}px`;
  body.style.top = `${Math.round(top)}px`;
}

function wireFloatingMenus(root) {
  document.documentElement.dataset.js = "1";
  const floating = new Set();
  const reposition = (event) => {
    for (const d of [...floating]) {
      if (!d.isConnected || !d.open) {
        floating.delete(d);
        continue;
      }
      if (event?.type === "scroll" && event.target instanceof Node && d.contains(event.target)) continue;
      placeMenu(d);
    }
  };
  root.addEventListener("toggle", (event) => {
    const details = event.target;
    if (!(details instanceof HTMLDetailsElement) || !details.matches("details.menu")) return;
    if (!details.open) {
      floating.delete(details);
      clearFloating(details);
      return;
    }
    if (hasOverflowAncestor(details)) {
      details.dataset.floating = "1";
      floating.add(details);
      placeMenu(details);
    }
  }, true);
  window.addEventListener("scroll", reposition, { capture: true, passive: true });
  window.addEventListener("resize", reposition, { passive: true });
  window.visualViewport?.addEventListener("resize", reposition);
  window.visualViewport?.addEventListener("scroll", reposition);
}

// Shared behaviour: dialogs, clickable rows, confirmations.
export function wireCommon(root = document) {
  registerServiceWorker();
  wireNetState();
  wireKeyboard();
  wireFloatingMenus(root);
  root.addEventListener("click", (event) => {
    for (const d of root.querySelectorAll("details.menu[open]")) {
      if (!d.contains(event.target)) d.open = false;
    }
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
    if (row && !event.target.closest("a, button, input, details, label, form")) {
      location.href = row.dataset.href;
    }
  });
  root.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      for (const d of root.querySelectorAll("details.menu[open]")) d.open = false;
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

// The header says when the connection is gone (the editor has its own save state). Once it's
// back, edits made offline are sent, also for documents that aren't open (§6.5).
function wireNetState() {
  const slot = document.getElementById("net-state");
  if (slot && !document.getElementById("save-state")) {
    const update = () => { slot.hidden = navigator.onLine; };
    window.addEventListener("online", update);
    window.addEventListener("offline", update);
    update();
  }
  if (!csrfToken() || !document.querySelector('meta[name="dw-user"]')) return; // offline shells, logged out
  document.addEventListener("dw:outbox", ({ detail }) => {
    if (!detail.sent) return;
    let message = detail.sent === 1 ? t("Edits made offline were sent for 1 document.")
      : t("Edits made offline were sent for {n} documents.", { n: detail.sent });
    if (detail.conflicts) {
      message += " " + (detail.conflicts === 1 ? t("1 passage needs a look.")
        : t("{n} passages need a look.", { n: detail.conflicts }));
    }
    toast(message);
  });
  import("../store/outbox.js").then((m) => m.startBackgroundSync()).catch(() => { /* best effort */ });
}

// Signed-in pages install the service worker that keeps the app usable offline (§6.5).
function registerServiceWorker() {
  if (!currentOwner() || !document.querySelector('meta[name="dw-user"]')) return;
  if (!("serviceWorker" in navigator)) return;
  navigator.serviceWorker.register("/sw.js").catch(() => { /* e.g. an untrusted certificate */ });
}

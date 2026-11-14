// Temml widgets for math, loaded the first time a document contains math (§6.3).
let temmlPromise = null;
let temml = null;
const listeners = new Set();

export function loadTemml() {
  if (!temmlPromise) {
    temmlPromise = import("temml").then((mod) => {
      temml = mod.default ?? mod;
      for (const fn of listeners) fn();
      listeners.clear();
      return temml;
    }).catch(() => null);
  }
  return temmlPromise;
}

export function temmlReady() {
  return temml;
}

export function onTemmlReady(fn) {
  if (temml) fn();
  else listeners.add(fn);
}

export function renderMath(tex, display) {
  const span = document.createElement("span");
  span.className = display ? "cm-math-widget cm-math-block" : "cm-math-widget";
  try {
    temml.render(tex, span, { displayMode: display, throwOnError: true });
  } catch {
    span.textContent = tex;
    span.classList.add("cm-cite-unknown");
  }
  return span;
}

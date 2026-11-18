// The on-screen keyboard on phones and tablets (ARCHITECTURE.md §25): it comes up only when
// someone taps into a field, and what they type into stays in view above it.
//
// - A dialog that opens doesn't put the focus in its first text field (which would bring the
//   keyboard up over the dialog) unless that field asks for it with autofocus.
// - While the keyboard is up, the page is as high as the space above it (--vvh, for browsers that
//   don't resize the page themselves), <html> has the class keyboard-open, and the field being
//   typed into scrolls into view. The editor keeps its cursor in view on "dw:keyboard".

const coarse = () => window.matchMedia("(pointer: coarse)").matches;
const TYPING = 'input:not([type="checkbox"], [type="radio"], [type="button"], [type="submit"], [type="file"], [type="range"], [type="color"]), textarea, [contenteditable="true"]';

let lastTap = null;

function guardDialogFocus() {
  document.addEventListener("pointerdown", (event) => { lastTap = event.target; }, true);
  document.addEventListener("focusin", (event) => {
    if (!coarse()) return;
    const field = event.target;
    if (!field.matches?.(TYPING) || field.hasAttribute("autofocus")) return;
    const dialog = field.closest("dialog[open]");
    if (!dialog) return;
    // A tap on the field (or its label) is a wish to type; anything else is the dialog opening.
    if (lastTap && (field.contains(lastTap) || lastTap.closest?.("label")?.contains(field))) return;
    if (dialog.dataset.settled) return;
    if (!dialog.hasAttribute("tabindex")) dialog.setAttribute("tabindex", "-1");
    dialog.focus({ preventScroll: true });
  }, true);
  // Once a dialog has been open for a moment, focusing its fields from code is deliberate.
  new MutationObserver((records) => {
    for (const r of records) {
      const dialog = r.target;
      if (dialog.open) setTimeout(() => { if (dialog.open) dialog.dataset.settled = "1"; }, 400);
      else delete dialog.dataset.settled;
    }
  }).observe(document.documentElement, { subtree: true, attributes: true, attributeFilter: ["open"] });
}

function trackViewport() {
  const vv = window.visualViewport;
  if (!vv) return;
  const root = document.documentElement;
  let wasOpen = false;
  const update = () => {
    root.style.setProperty("--vvh", `${Math.round(vv.height)}px`);
    // A keyboard takes a good part of the screen; a hidden browser bar doesn't.
    const open = coarse() && window.screen.height - vv.height > 0.3 * window.screen.height
      && document.activeElement?.matches?.(TYPING);
    root.classList.toggle("keyboard-open", Boolean(open));
    if (open && !wasOpen) reveal(document.activeElement);
    wasOpen = Boolean(open);
  };
  vv.addEventListener("resize", update);
  document.addEventListener("focusout", () => setTimeout(update, 50));
  update();
}

// Brings the field being typed into back into view once the keyboard is up.
function reveal(field) {
  if (!field || field === document.body) return;
  requestAnimationFrame(() => {
    if (field.closest(".cm-editor")) {
      document.dispatchEvent(new CustomEvent("dw:keyboard"));
    } else {
      field.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  });
}

let wired = false;
export function wireKeyboard() {
  if (wired) return;
  wired = true;
  guardDialogFocus();
  trackViewport();
  // A field tapped while the keyboard is already up scrolls into view as well.
  document.addEventListener("focusin", (event) => {
    if (coarse() && document.documentElement.classList.contains("keyboard-open") && event.target.matches?.(TYPING)) {
      reveal(event.target);
    }
  });
}

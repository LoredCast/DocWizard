// The project page's Sync section (ARCHITECTURE.md §12): Nextcloud Login Flow v2,
// other WebDAV servers, and sync now / pause / disconnect.
import { $, api, confirmDialog, t, toast } from "./ui/dom.js";

export function wireSync(section) {
  const base = `/api/projects/${section.dataset.project}/sync`;

  section.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-sync-action]");
    if (!button) return;
    const action = button.dataset.syncAction;
    try {
      if (action === "run") {
        button.disabled = true;
        $("#sync-state").textContent = t("Syncing…");
        const { report, target } = await api("POST", `${base}/run`);
        if (report.error) throw new Error(target.lastError ?? report.error);
      } else if (action === "pause" || action === "resume") {
        await api("PATCH", base, { paused: action === "pause" });
      } else if (action === "disconnect") {
        if (!await confirmDialog(t("Stop syncing this project? Files stay where they are on both sides."), t("Disconnect"))) return;
        await api("DELETE", base);
      }
      location.reload();
    } catch (err) {
      button.disabled = false;
      toast(err.message, { error: true });
      $("#sync-state").textContent = err.message;
    }
  });

  $("#sync-nextcloud", section)?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target;
    const note = $("#sync-flow", section);
    // Opened right away, inside the click, so pop-up blockers allow it.
    const popup = window.open("about:blank", "_blank");
    try {
      const { flow, login } = await api("POST", `${base}/nextcloud`,
        { server: form.elements.server.value, remotePath: form.elements.remotePath.value });
      if (popup) popup.location = login;
      note.hidden = false;
      note.replaceChildren(t("Log in to Nextcloud in the new tab and grant access.") + " ",
        Object.assign(document.createElement("a"), { href: login, target: "_blank", rel: "noopener", textContent: t("Open the login page") }));
      for (let i = 0; i < 600; i++) {
        await new Promise((r) => setTimeout(r, 2000));
        const res = await api("GET", `${base}/nextcloud/${flow}`);
        if (res.done) { location.reload(); return; }
      }
      note.textContent = t("The login expired. Try again.");
    } catch (err) {
      popup?.close();
      toast(err.message, { error: true });
    }
  });

  $("#sync-webdav", section)?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const f = event.target.elements;
    try {
      await api("PUT", base, { kind: f.kind.value, url: f.url.value, remotePath: f.remotePath.value,
                               username: f.username.value, password: f.password.value });
      location.reload();
    } catch (err) {
      toast(err.message, { error: true });
    }
  });
}

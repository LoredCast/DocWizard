// Page bootstrapping for server-rendered pages (home, project, settings).
import { markPinned, putRecent } from "./store/idb.js";
import { $, $$, api, t, toast, wireCommon } from "./ui/dom.js";

wireCommon();

// The home page's recent list is mirrored for the offline home page (§6.5).
const recentData = $("#recent-data");
if (recentData) {
  const items = JSON.parse(recentData.textContent);
  putRecent(items);
  markPinned(new Set(items.filter((d) => d.pinned).map((d) => d.docId)));
}

for (const form of $$("form[data-upload]")) {
  // Choosing files uploads them; there is no second button to press.
  form.querySelector('input[type="file"]')?.addEventListener("change", () => form.requestSubmit());
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const project = form.dataset.upload;
    const input = form.querySelector('input[type="file"]');
    const status = document.querySelector("[data-upload-status]");
    const files = [...input.files];
    if (!files.length) return;
    try {
      for (const [i, file] of files.entries()) {
        if (status) status.textContent = t("Uploading {i} of {n}: {name}", { i: i + 1, n: files.length, name: file.name });
        const path = file.name.split("/").map(encodeURIComponent).join("/");
        const result = await api("PUT", `/api/projects/${project}/assets/${path}`, file, { raw: true });
        // One imported Word file opens right away, with its comments and changes (§8.11).
        if (files.length === 1 && result?.imported) {
          location.href = `/p/${project}/${result.path.split("/").map(encodeURIComponent).join("/")}`;
          return;
        }
      }
      location.reload();
    } catch (err) {
      input.value = ""; // so the same file can be chosen again
      if (status) status.textContent = "";
      toast(err.message, { error: true });
    }
  });
}

if ($("#sync")) import("./sync.js").then((m) => m.wireSync($("#sync")));

// Page bootstrapping for server-rendered pages (home, project, settings).
import { markPinned, putRecent } from "./store/idb.js";
import { $, $$, api, toast, wireCommon } from "./ui/dom.js";

wireCommon();

// The home page's recent list is mirrored for the offline home page (§6.5).
const recentData = $("#recent-data");
if (recentData) {
  const items = JSON.parse(recentData.textContent);
  putRecent(items);
  markPinned(new Set(items.filter((d) => d.pinned).map((d) => d.docId)));
}

for (const form of $$("form[data-upload]")) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const project = form.dataset.upload;
    const input = form.querySelector('input[type="file"]');
    const status = document.querySelector("[data-upload-status]");
    const files = [...input.files];
    if (!files.length) return;
    try {
      for (const [i, file] of files.entries()) {
        if (status) status.textContent = `Uploading ${i + 1} of ${files.length}: ${file.name}`;
        const path = file.name.split("/").map(encodeURIComponent).join("/");
        await api("PUT", `/api/projects/${project}/assets/${path}`, file, { raw: true });
      }
      location.reload();
    } catch (err) {
      if (status) status.textContent = "";
      toast(err.message, { error: true });
    }
  });
}

if ($("#sync")) import("./sync.js").then((m) => m.wireSync($("#sync")));

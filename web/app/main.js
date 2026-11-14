// Page bootstrapping for server-rendered pages (home, project, settings).
import { $$, api, toast, wireCommon } from "./ui/dom.js";

wireCommon();

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

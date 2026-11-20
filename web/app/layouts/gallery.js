// The layout gallery: make a copy of a layout, start a new one, delete your own (ARCHITECTURE.md §9.3).
import { $$, api, t, toast, wireCommon } from "../ui/dom.js";

wireCommon();

for (const button of $$("[data-new-layout]")) {
  button.addEventListener("click", async () => {
    const from = button.dataset.newLayout;
    const name = button.dataset.name ? t("Copy of {name}", { name: button.dataset.name }) : t("New layout");
    button.disabled = true;
    try {
      const layout = await api("POST", "/api/layouts", { name, extends: from });
      location.href = `/layouts/${encodeURIComponent(layout.id)}`;
    } catch (err) {
      toast(err.message, { error: true });
      button.disabled = false;
    }
  });
}

for (const button of $$("[data-delete-layout]")) {
  button.addEventListener("click", async () => {
    const question = t("Delete the layout “{name}”? Projects that use it switch to the default layout.",
                       { name: button.dataset.name });
    if (!confirm(question)) return;
    try {
      await api("DELETE", `/api/layouts/${encodeURIComponent(button.dataset.deleteLayout)}`);
      button.closest("li")?.remove();
      toast(t("Layout deleted."));
    } catch (err) {
      toast(err.message, { error: true });
    }
  });
}

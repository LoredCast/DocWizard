// Browser smoke tests (§20): headless Chromium at phone and desktop sizes against a real server
// with the render worker. Run with `node --test tests/e2e/*.test.mjs` (needs Python with the app's
// dependencies, pandoc, Typst and the Node "playwright" package with Chromium).
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdirSync } from "node:fs";
import path from "node:path";
import { after, afterEach, before, describe, test } from "node:test";

import { accessible } from "./a11y.mjs";
import { startServer } from "./server.mjs";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const VIEWPORTS = [
  { name: "phone", viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true },
  { name: "desktop", viewport: { width: 1280, height: 800 } },
];
const PASSWORD = process.env.E2E_PASSWORD ?? ["a", "long", "enough", "password"].join(" ");
const QUOTA = 300_000; // bytes per account, small enough to fill in a test

let server;
let browser;

before(async () => {
  server = await startServer({ DOCWIZARD_LIMITS_DEFAULT_QUOTA_BYTES: String(QUOTA) });
  browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
});

after(async () => {
  await browser?.close();
  await server?.stop();
});

// A page that records failed asset requests and page errors.
async function openPage(vp) {
  const context = await browser.newContext({
    viewport: vp.viewport, isMobile: vp.isMobile ?? false, hasTouch: vp.hasTouch ?? false,
    deviceScaleFactor: 1, baseURL: server.base,
  });
  const page = await context.newPage();
  page.problems = [];
  const asset = (req) => ["script", "stylesheet", "image", "font"].includes(req.resourceType());
  page.on("requestfailed", (req) => {
    // Navigations a later one replaced (an iframe reloading, a page left) report ERR_ABORTED.
    if (req.failure()?.errorText.includes("ERR_ABORTED")) return;
    if (asset(req)) page.problems.push(`failed: ${req.url()} ${req.failure()?.errorText}`);
  });
  page.on("response", (res) => {
    if (asset(res.request()) && res.status() >= 400) page.problems.push(`${res.status()}: ${res.url()}`);
  });
  page.on("pageerror", (err) => page.problems.push(`page error: ${err.message}`));
  return page;
}

async function noOverflow(page, where) {
  const { scroll, width } = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth, width: window.innerWidth,
  }));
  assert.ok(scroll <= width, `${where}: the page is ${scroll}px wide in a ${width}px window`);
}

function noProblems(page) {
  assert.deepEqual(page.problems, [], "failed requests or page errors");
}

// The confirmation dialog the app shows instead of window.confirm().
async function confirm(page) {
  await page.locator("dialog[open] button.danger").click();
}

for (const vp of VIEWPORTS) {
  describe(`smoke at ${vp.name} size (${vp.viewport.width}×${vp.viewport.height})`, () => {
    const username = `e2e-${vp.name}`;
    let page;
    let projectUrl;

    before(async () => {
      page = await openPage(vp);
    });

    after(async () => {
      await page?.context().close();
    });

    // CI keeps a screenshot of each step (E2E_SCREENSHOTS names the folder).
    afterEach(async (t) => {
      const dir = process.env.E2E_SCREENSHOTS;
      if (!dir || !page) return;
      mkdirSync(dir, { recursive: true });
      const name = `${vp.name}-${t.name}`.replace(/[^a-z0-9]+/gi, "-").toLowerCase();
      await page.screenshot({ path: path.join(dir, `${name}.png`), fullPage: true }).catch(() => {});
    });

    test("register, log out and log in again", async () => {
      await page.goto("/register");
      await noOverflow(page, "register");
      await accessible(page, "register");
      await page.fill('input[name="username"]', username);
      await page.fill('input[name="password"]', PASSWORD);
      await page.click('form[action="/register"] button[type="submit"]');
      await page.waitForURL(`${server.base}/`);
      await noOverflow(page, "home");
      await accessible(page, "home");

      await page.locator("details.account-menu summary").click();
      await page.click('form[action="/logout"] button');
      await page.waitForURL(/\/login/);
      await noOverflow(page, "login");
      await accessible(page, "login");
      await page.fill('input[name="username"]', username);
      await page.fill('input[name="password"]', PASSWORD);
      await page.click('form[action="/login"] button[type="submit"]');
      await page.waitForURL(`${server.base}/`);
      await page.goto("/settings");
      await noOverflow(page, "settings");
      await accessible(page, "settings");
      await page.goto("/");
      noProblems(page);
    });

    test("create a project, then create, rename and delete a file", async () => {
      await page.click('[data-open-dialog="new-project"]');
      await page.fill('dialog[open] input[name="name"]', `Thesis ${vp.name}`);
      await page.click('dialog[open] button[type="submit"]');
      // A new project opens its main document.
      await page.waitForURL(/\/p\/[^/]+\/.+\.md$/);
      await page.waitForSelector(".cm-content");
      projectUrl = page.url().replace(/(\/p\/[^/]+)\/.*$/, "$1");
      await page.goto(projectUrl);
      await noOverflow(page, "project");
      await accessible(page, "project");

      await page.fill('input[name="path"][placeholder]', "notes.md");
      await page.click('button:text("Create document")');
      await page.waitForURL(/notes\.md/);
      await page.goto(projectUrl);
      const row = (name) => page.locator("tbody tr", { has: page.locator(`a:text-is("${name}")`) });
      assert.equal(await row("notes.md").count(), 1);

      await row("notes.md").locator("details.file-menu summary").click();
      // The file edit menu floats above the table's scroll container, completely visible without scrolling it.
      const menuBox = await row("notes.md").locator("details.file-menu .menu-body").boundingBox();
      assert.ok(menuBox && menuBox.height > 60 && menuBox.x >= 0 && menuBox.y >= 0
        && menuBox.x + menuBox.width <= vp.viewport.width && menuBox.y + menuBox.height <= vp.viewport.height,
        `file menu should be fully visible in viewport, got ${JSON.stringify(menuBox)}`);
      await row("notes.md").locator('input[name="new_path"]').fill("chapter-one.md");
      await row("notes.md").locator('button:text("Rename")').click();
      await page.waitForLoadState("load");
      await row("chapter-one.md").waitFor();
      assert.equal(await row("notes.md").count(), 0);

      await row("chapter-one.md").locator("details.file-menu summary").click();
      await row("chapter-one.md").locator('button:text("Delete")').click();
      await confirm(page);
      await page.waitForLoadState("load");
      await page.waitForFunction(() => ![...document.querySelectorAll("tbody a")].some((a) => a.textContent === "chapter-one.md"));
      // It is in the trash, ready to restore.
      assert.ok(await page.locator("#trash").getByText("chapter-one.md").isVisible());
      await noOverflow(page, "project after delete");
      noProblems(page);
    });

    test("home: tags, archive and the filter bar", async () => {
      await page.goto("/");
      await page.locator("#home-filter").waitFor();
      const row = page.locator("#projects-table tbody tr").first();
      await row.locator("[data-organize]").click();
      await page.fill("#organize-tags", "thesis, 2026");
      await page.locator("#organize-form button[type=submit]").click();
      await row.locator("a.tag", { hasText: "#thesis" }).waitFor();
      await noOverflow(page, "home with tags");
      // Filtering is instant and kept in the address.
      await page.fill('#home-filter input[name="q"]', "no such project");
      await page.locator("#projects-none").waitFor();
      assert.ok(page.url().includes("q=no+such+project"));
      await page.fill('#home-filter input[name="q"]', "");
      await page.locator('#home-filter select[name="tag"]').selectOption("thesis");
      assert.ok(await row.isVisible());
      // Archived projects leave the list, and come back under Archived.
      await page.locator('#home-filter select[name="tag"]').selectOption("");
      await row.locator("[data-organize]").click();
      await page.check("#organize-archived");
      await page.locator("#organize-form button[type=submit]").click();
      await page.locator("#projects-none").waitFor();
      await page.check('#home-filter input[name="archived"]');
      await page.locator("#projects-table tbody tr:not([hidden])").first().waitFor();
      await page.locator("#projects-table tbody tr:not([hidden]) [data-organize]").first().click();
      await page.uncheck("#organize-archived");
      await page.locator("#organize-form button[type=submit]").click();
      await page.uncheck('#home-filter input[name="archived"]');
      await page.locator("#projects-table tbody tr:not([hidden])").first().waitFor();
      await noOverflow(page, "home");
      noProblems(page);
    });

    test("formatting toolbar", async () => {
      await page.goto(projectUrl);
      await page.locator("tbody a", { hasText: /\.md$/ }).first().click();
      await page.waitForSelector(".cm-content");
      await noOverflow(page, "editor");
      await accessible(page, "editor");
      const editor = page.locator(".cm-content");
      await editor.click();
      await page.keyboard.press("ControlOrMeta+End");
      await page.keyboard.press("Enter");
      await page.keyboard.type("strong words");
      await page.keyboard.down("Shift");
      for (let i = 0; i < "words".length; i++) await page.keyboard.press("ArrowLeft");
      await page.keyboard.up("Shift");
      const bold = page.locator('#format-bar [data-cmd="bold"]');
      await bold.scrollIntoViewIfNeeded();
      await bold.click();
      await page.waitForFunction(() => document.querySelector(".cm-content").textContent.includes("strong **words**"));
      assert.equal(await bold.getAttribute("aria-pressed"), "true");
      await page.locator('#format-bar [data-cmd="undo"]').click();
      await page.waitForFunction(() => !document.querySelector(".cm-content").textContent.includes("**words**"));
      // (On phones the save state sits in the Actions menu.)
      await page.waitForFunction(() => document.querySelector("#save-state").textContent.trim() === "Saved", null,
                                 { timeout: 15_000 });
      noProblems(page);
    });

    test("tables show as tables, and plain text editing shows the Markdown", async () => {
      await page.goto(projectUrl);
      await page.fill('input[name="path"][placeholder]', "tables.md");
      await page.click('button:text("Create document")');
      await page.waitForURL(/tables\.md/);
      await page.waitForSelector(".cm-content");
      await page.locator(".cm-content").click();
      await page.keyboard.press("ControlOrMeta+End");
      await page.keyboard.insertText("| City | Bridges | Pipes (km) | Roads (km) | Budget per year (million EUR) |\n"
        + "|------|--------:|-----------:|-----------:|------------------------------:|\n"
        + "| Berlin | 960 | 9,600 | 5,400 | 1,200 |\n\nAfter the table.\n");
      // The cursor is below the table, so the table shows as one, inside the width of the window.
      await page.locator(".cm-table-view td", { hasText: "Berlin" }).waitFor();
      await noOverflow(page, "editor with a table");
      const raw = page.locator('#format-bar [data-cmd="raw"]');
      await raw.scrollIntoViewIfNeeded();
      await raw.click();
      assert.equal(await raw.getAttribute("aria-pressed"), "true");
      assert.equal(await page.locator(".cm-table-view").count(), 0, "plain text shows the table's Markdown");
      await raw.click();
      await page.locator(".cm-table-view").waitFor();
      // The table is edited as a table: a click puts the caret in the cell, with the table tools;
      // typing, Tab, Enter and the table bar change the Markdown, which never shows.
      const source = () => page.evaluate(async () => {
        const { EditorView } = await import("codemirror");
        const text = EditorView.findFromDOM(document.querySelector(".cm-editor")).state.doc.toString();
        return text.slice(text.indexOf("| City"), text.indexOf("\n\nAfter"));
      });
      const berlin = page.locator(".cm-table-view td", { hasText: "Berlin" });
      if (vp.hasTouch) await berlin.tap();
      else await berlin.click();
      await page.locator("#table-bar:not([hidden])").waitFor();
      assert.equal(await page.evaluate(() => document.activeElement.textContent), "Berlin");
      await page.keyboard.press("End");
      await page.keyboard.type(" | Mitte");
      await page.keyboard.press("Tab");
      await page.keyboard.type("961");
      assert.match(await source(), /\| Berlin \\\| Mitte +\| +961 \|/);
      await page.keyboard.press("Enter"); // the last row: a new one
      await page.keyboard.type("12");
      const addColumn = page.locator('#table-bar [data-table="column"]');
      if (vp.hasTouch) await addColumn.tap();
      else await addColumn.click();
      await page.keyboard.type("New");
      const lines = (await source()).split("\n");
      assert.equal(lines.length, 4);
      assert.match(lines[3], /^\| +\| +12 \| New +\| +\| +\| +\| *$/);
      assert.equal(await page.locator(".cm-table-view").count(), 1, "the table stays a table");
      // Undo works in a cell; Escape goes back to the text below the table, with its columns padded.
      await page.keyboard.press("ControlOrMeta+z");
      assert.equal(await page.evaluate(() => document.activeElement.textContent), "");
      await page.keyboard.press("Escape");
      await page.waitForFunction(() => document.activeElement.classList.contains("cm-content"));
      await page.locator("#table-bar[hidden]").waitFor({ state: "attached" });
      const padded = (await source()).split("\n");
      assert.ok(padded.every((l) => l.length === padded[0].length), "the columns are padded again");
      // The Insert dropdown wraps long item hints cleanly without horizontal overflow.
      const insertBtn = page.locator('[data-cmd="insert"]');
      if (await insertBtn.isVisible()) {
        await insertBtn.click();
        await page.locator(".dw-insert-menu").waitFor();
        const menuCheck = await page.evaluate(() => {
          const pop = document.querySelector(".dw-popover");
          const rect = pop.getBoundingClientRect();
          const buttons = [...pop.querySelectorAll(".dw-insert-menu button")];
          const btnOverflow = buttons.some((b) => b.scrollWidth > b.clientWidth + 1);
          return {
            popOverflow: pop.scrollWidth > pop.clientWidth + 1,
            btnOverflow,
            withinViewport: rect.left >= -1 && rect.right <= window.innerWidth + 1,
          };
        });
        assert.ok(!menuCheck.popOverflow && !menuCheck.btnOverflow && menuCheck.withinViewport,
          `Insert dropdown overflows: ${JSON.stringify(menuCheck)}`);
        await page.keyboard.press("Escape");
      }
      noProblems(page);
    });

    test("preview: HTML and PDF, with the phone's Edit/Preview switch", async () => {
      const modeSwitch = page.locator(".mode-switch");
      if (vp.isMobile) {
        assert.ok(await modeSwitch.isVisible(), "the Edit/Preview switch shows on phones");
        assert.ok(!(await page.locator("#preview-pane").isVisible()));
        await page.click("#mode-preview");
        assert.ok(!(await page.locator(".editor-pane").isVisible()), "the editor hides behind the preview");
      } else {
        assert.ok(!(await modeSwitch.isVisible()));
      }
      await page.locator("#preview-pane").waitFor();
      // HTML: the preview origin serves the page into the sandboxed frame.
      await page.waitForFunction(() => document.querySelector("#preview")?.src.includes("/_preview/"), null,
                                 { timeout: 60_000 });
      const frame = page.frameLocator("#preview");
      await frame.locator("body").getByText(/\S/).first().waitFor({ timeout: 30_000 });
      await noOverflow(page, "HTML preview");
      // The editor stays locked to the viewport (no scrolling below the footer). On mobile, Page setup
      // lives in the Actions menu so the preview bar stays uncluttered; on desktop it stays in the bar.
      const layoutCheck = await page.evaluate(() => {
        const previewOpts = document.querySelector(".preview-options");
        const optsRect = previewOpts ? previewOpts.getBoundingClientRect() : null;
        const bar = document.getElementById("statusbar").getBoundingClientRect();
        return {
          scrollH: document.documentElement.scrollHeight, innerH: window.innerHeight,
          optsVisible: Boolean(optsRect && optsRect.width > 0 && optsRect.height > 0),
          optsRight: optsRect?.right ?? 0, innerW: window.innerWidth, barH: bar.height,
        };
      });
      assert.ok(layoutCheck.scrollH <= layoutCheck.innerH,
        `editor overflows vertically: scrollHeight=${layoutCheck.scrollH} > innerHeight=${layoutCheck.innerH}`);
      if (vp.isMobile) {
        assert.ok(!layoutCheck.optsVisible, "Page setup button should be moved to Actions on phones");
      } else {
        assert.ok(layoutCheck.optsVisible && layoutCheck.optsRight <= layoutCheck.innerW + 1,
          `Page setup overflows horizontally: right=${layoutCheck.optsRight} > innerWidth=${layoutCheck.innerW}`);
      }
      assert.ok(layoutCheck.barH <= 36, `editor statusbar wrapped to multiple lines: height=${layoutCheck.barH}`);

      await page.locator('input[name="preview-target"][value="pdf"]').check();
      await page.locator(".pdf-viewer canvas").first().waitFor({ timeout: 90_000 });
      await noOverflow(page, "PDF preview");
      // The PDF bar: the text layer is there to select, zoom steps, the page count, find.
      await page.locator(".pdf-sheet .textLayer span").first().waitFor({ timeout: 30_000 });
      assert.ok(await page.locator("#pdf-bar").isVisible());
      assert.equal(await page.locator("[data-pdf-zoom]").inputValue(), "fit");
      await page.click("[data-pdf-zoom-in]");
      await page.waitForFunction(() => document.querySelector("[data-pdf-zoom]").value !== "fit");
      await page.locator("[data-pdf-zoom]").selectOption("fit");
      assert.match(await page.locator("[data-pdf-pages]").textContent(), /^\d+$/);
      await page.click("[data-pdf-open-find]");
      await page.fill("[data-pdf-find]", "after the table");
      await page.waitForFunction(() => /\d+ of \d+/.test(document.querySelector("[data-pdf-find-count]").textContent),
                                 null, { timeout: 15_000 });
      assert.ok(await page.locator(".sheet-mark.find.current").count() >= 1);
      await page.keyboard.press("Escape");
      assert.equal(await page.locator(".sheet-mark.find").count(), 0);
      await noOverflow(page, "PDF preview with its bar");
      await accessible(page, "editor with the PDF preview");

      if (vp.isMobile) {
        await page.click("#mode-edit");
        assert.ok(await page.locator(".editor-pane").isVisible());
        assert.ok(!(await page.locator("#preview-pane").isVisible()));
      }
      noProblems(page);
    });

    test("command palette", async () => {
      // The editor from the last test; on phones from the Actions menu, elsewhere Ctrl/Cmd+K.
      if (vp.isMobile) {
        await page.click("#more-actions");
        await page.click("#open-palette");
      } else {
        await page.locator(".cm-content").click();
        await page.keyboard.press("ControlOrMeta+K");
      }
      const input = page.locator("dialog.palette[open] .palette-input");
      await input.waitFor();
      await noOverflow(page, "command palette");
      await input.fill("page setup");
      assert.match(await page.locator(".palette-list li[aria-selected=true]").textContent(), /Page setup/);
      await page.keyboard.press("Enter");
      await page.locator("#options-dialog[open]").waitFor();
      await page.locator("#options-dialog [data-close-dialog]").click();
      // ">" lists the actions only.
      if (vp.isMobile) {
        await page.click("#more-actions");
        await page.click("#open-palette");
      } else {
        await page.keyboard.press("ControlOrMeta+K");
      }
      await input.fill(">print");
      assert.match(await page.locator(".palette-list li[aria-selected=true]").textContent(), /Download or print/);
      await page.keyboard.press("Escape");
      assert.equal(await page.locator("dialog.palette[open]").count(), 0);
      noProblems(page);
    });

    test("storage quota reached", async () => {
      await page.goto(projectUrl);
      await page.locator('form[data-upload] input[type="file"]').setInputFiles({
        name: "big-scan.png", mimeType: "image/png", buffer: Buffer.alloc(QUOTA + 1, 7),
      });
      const toast = page.locator(".toast.error");
      await toast.waitFor();
      assert.match(await toast.textContent(), /Storage quota reached/);
      await noOverflow(page, "project with quota error");
      await page.goto("/settings");
      assert.ok(await page.locator("#storage").isVisible());
      await noOverflow(page, "settings");
      noProblems(page);
    });
  });
}

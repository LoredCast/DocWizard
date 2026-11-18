// Browser smoke tests (§20): headless Chromium at phone and desktop sizes against a real server
// with the render worker. Run with `node --test tests/e2e/*.test.mjs` (needs Python with the app's
// dependencies, pandoc, Typst and the Node "playwright" package with Chromium).
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdirSync } from "node:fs";
import path from "node:path";
import { after, afterEach, before, describe, test } from "node:test";

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
      await page.fill('input[name="username"]', username);
      await page.fill('input[name="password"]', PASSWORD);
      await page.click('form[action="/register"] button[type="submit"]');
      await page.waitForURL(`${server.base}/`);
      await noOverflow(page, "home");

      await page.locator("details.account-menu summary").click();
      await page.click('form[action="/logout"] button');
      await page.waitForURL(/\/login/);
      await noOverflow(page, "login");
      await page.fill('input[name="username"]', username);
      await page.fill('input[name="password"]', PASSWORD);
      await page.click('form[action="/login"] button[type="submit"]');
      await page.waitForURL(`${server.base}/`);
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

      await page.fill('input[name="path"][placeholder]', "notes.md");
      await page.click('button:text("Create document")');
      await page.waitForURL(/notes\.md/);
      await page.goto(projectUrl);
      const row = (name) => page.locator("tbody tr", { has: page.locator(`a:text-is("${name}")`) });
      assert.equal(await row("notes.md").count(), 1);

      await row("notes.md").locator("details.file-menu summary").click();
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

    test("formatting toolbar", async () => {
      await page.goto(projectUrl);
      await page.locator("tbody a", { hasText: /\.md$/ }).first().click();
      await page.waitForSelector(".cm-content");
      await noOverflow(page, "editor");
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
      // A click on a cell opens the table's source there, with the table tools.
      await page.locator(".cm-table-view td", { hasText: "Berlin" }).click();
      await page.locator("#table-bar:not([hidden])").waitFor();
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

      await page.locator('input[name="preview-target"][value="pdf"]').check();
      await page.locator(".pdf-viewer canvas").first().waitFor({ timeout: 90_000 });
      await noOverflow(page, "PDF preview");

      if (vp.isMobile) {
        await page.click("#mode-edit");
        assert.ok(await page.locator(".editor-pane").isVisible());
        assert.ok(!(await page.locator("#preview-pane").isVisible()));
      }
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

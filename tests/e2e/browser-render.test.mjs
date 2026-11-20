// The PDF preview made in the browser (§6.4): pandoc and Typst as WebAssembly turn the render kit
// and the text being typed into a PDF, with the layout's Lua filters, and the kept kit still works
// when the server is gone. Needs `deploy/fetch_tools.py --browser-render web/vendor/browser-render`.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { existsSync, mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { after, before, describe, test } from "node:test";

import { startServer } from "./server.mjs";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const INSTALLED = existsSync(path.join(ROOT, "web/vendor/browser-render/pandoc.wasm"));
const PASSWORD = process.env.E2E_PASSWORD ?? ["a", "long", "enough", "password"].join(" ");

const TEXT = [
  "---", "title: Lighthouse Report", "---", "",
  "# Findings {#sec:findings}", "",
  "The keeper logged a quiet night, as @sec:findings records.", "",
].join("\n");

let server;
let browser;
let page;

async function call(method, url, body) {
  return page.evaluate(async ([m, u, b]) => {
    const res = await fetch(u, { method: m, credentials: "same-origin", headers: {
      "Content-Type": "application/json", "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content },
    body: b === undefined ? undefined : JSON.stringify(b) });
    return { status: res.status, data: await res.json().catch(() => null) };
  }, [method, url, body]);
}

// Renders *text* with the page's own modules and returns the PDF's text, page by page.
async function renderText(projectId, docId, text) {
  return page.evaluate(async ([p, d, s]) => {
    const main = document.querySelector('script[type="module"][src$="/app/editor/main.js"]').src;
    const { LocalRenderer } = await import(new URL("../preview/local.js", main).href);
    const { loadPdfJs } = await import(new URL("../preview/pdf.js", main).href);
    const r = new LocalRenderer({ projectId: p, docId: d });
    try {
      const out = await r.render(s);
      const pdfjs = await loadPdfJs();
      const doc = await pdfjs.getDocument({ data: out.pdf }).promise;
      const pages = [];
      for (let i = 1; i <= doc.numPages; i++) {
        pages.push((await (await doc.getPage(i)).getTextContent()).items.map((x) => x.str).join(" "));
      }
      return { pages };
    } catch (err) {
      return { error: String(err.message ?? err) };
    } finally {
      r.stop();
    }
  }, [projectId, docId, text]);
}

// CI keeps screenshots (E2E_SCREENSHOTS names the folder).
async function shot(name) {
  const dir = process.env.E2E_SCREENSHOTS;
  if (!dir) return;
  mkdirSync(dir, { recursive: true });
  await page.screenshot({ path: path.join(dir, `browser-render-${name}.png`) }).catch(() => {});
}

describe("browser renderer", { skip: !INSTALLED && "the browser renderer isn't installed" }, () => {
  before(async () => {
    server = await startServer();
    browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
    const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, baseURL: server.base });
    page = await context.newPage();
    page.problems = [];
    page.on("pageerror", (err) => page.problems.push(err.message));
    await page.goto("/register");
    await page.fill('input[name="username"]', "wasm-e2e");
    await page.fill('input[name="password"]', PASSWORD);
    await page.click('form[action="/register"] button[type="submit"]');
    await page.waitForURL(`${server.base}/`);
  });

  after(async () => {
    await browser?.close();
    await server?.stop();
  });

  test("the kit and the typed text make the PDF in the browser", { timeout: 180_000 }, async () => {
    const made = await call("POST", "/api/projects", { name: "Wasm", starter: false });
    assert.equal(made.status, 201, JSON.stringify(made.data));
    const projectId = made.data.id;
    const doc = await call("POST", `/api/projects/${projectId}/documents`, { path: "report.md", text: TEXT });
    assert.ok(doc.status < 300, JSON.stringify(doc.data));
    await page.goto(`/p/${projectId}/report.md`);
    await page.waitForSelector(".cm-content");

    const kit = await call("GET", `/api/projects/${projectId}/render-kit?doc=${doc.data.id}`);
    assert.equal(kit.status, 200, JSON.stringify(kit.data));
    assert.ok(kit.data.files["src/report.md"]);

    // The typed text, not the saved one; the reference comes from a Lua filter.
    const out = await renderText(projectId, doc.data.id, TEXT.replace("quiet night", "stormy night"));
    assert.ok(!out.error, out.error);
    const all = out.pages.join(" ");
    assert.match(all, /Lighthouse Report/);
    assert.match(all, /stormy night/);
    assert.match(all, /Section/);
    assert.doesNotMatch(all, /sec:findings/);

    // In the editor: "In this browser" makes the preview from what is typed.
    await page.click('input[name="preview-target"][value="pdf"]');
    await page.check("#local-render");
    await page.waitForFunction(() => document.querySelector("#preview-pane").dataset.madeIn === "browser",
                               null, { timeout: 120_000 });
    await page.locator(".pdf-sheet").first().waitFor();
    await shot("editor");

    // Offline, the kept kit still makes the PDF.
    await page.context().setOffline(true);
    try {
      const offline = await renderText(projectId, doc.data.id, TEXT.replace("quiet night", "foggy night"));
      assert.ok(!offline.error, offline.error);
      assert.match(offline.pages.join(" "), /foggy night/);
    } finally {
      await page.context().setOffline(false);
    }
    assert.deepEqual(page.problems, []);
  });
});

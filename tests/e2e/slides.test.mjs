// Slide decks in the browser (§6.3): the slide sorter reorders slides by editing the Markdown,
// and the presenter view shows the web deck's slides with the notes. Run with
// `node --test tests/e2e/*.test.mjs`.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdirSync } from "node:fs";
import path from "node:path";
import { after, before, test } from "node:test";

import { startServer } from "./server.mjs";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const PASSWORD = process.env.E2E_PASSWORD ?? ["a", "long", "enough", "password"].join(" ");

const DECK = `---
title: Reefs
---

## One

First slide.

::: notes
Say hello.
:::

## Two

Second slide.

## Three

Third slide.
`;

// CI keeps screenshots when E2E_SCREENSHOTS names a folder.
async function shot(name) {
  const dir = process.env.E2E_SCREENSHOTS;
  if (!dir) return;
  mkdirSync(dir, { recursive: true });
  await page.waitForTimeout(1000); // the deck scrolls smoothly to the slide
  await page.screenshot({ path: path.join(dir, `slides-${name}.png`) });
}

let server;
let browser;
let page;

before(async () => {
  server = await startServer();
  browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, baseURL: server.base });
  page = await context.newPage();
  page.problems = [];
  page.on("pageerror", (err) => page.problems.push(err.message));
});

after(async () => {
  await browser?.close();
  await server?.stop();
});

async function call(method, url, body) {
  return page.evaluate(async ([method, url, body]) => {
    const r = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json",
                 "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return r.json();
  }, [method, url, body]);
}

test("sorter and presenter view", async () => {
  await page.goto("/register");
  await page.fill('input[name="username"]', "slides");
  await page.fill('input[name="password"]', PASSWORD);
  await page.click('form[action="/register"] button[type="submit"]');
  await page.waitForURL(`${server.base}/`);
  const project = await call("POST", "/api/projects", { name: "Talk", layout: "slides", starter: false });
  await call("POST", `/api/projects/${project.id}/documents`, { path: "talk.md", text: DECK });
  await page.goto(`/p/${project.id}/talk.md`);
  await page.waitForSelector(".cm-content");

  // The sorter: title slide plus three; Alt+→ moves "One" after "Two".
  await page.click("#more-actions");
  await page.click("#open-sorter");
  const titles = () => page.locator(".dw-slide-card strong").allTextContents();
  assert.deepEqual(await titles(), ["Reefs", "One", "Two", "Three"]);
  await page.locator(".dw-slide-card").nth(1).focus();
  await page.keyboard.press("Alt+ArrowRight");
  await page.waitForFunction(() => document.querySelectorAll(".dw-slide-card strong")[1]?.textContent === "Two");
  assert.deepEqual(await titles(), ["Reefs", "Two", "One", "Three"]);
  await shot("sorter");
  const text = await page.evaluate(() => document.querySelector(".cm-content").innerText);
  assert.ok(text.indexOf("Second slide.") < text.indexOf("First slide."), text);
  await page.click('.dw-sorter button:text("Present")');

  // The presenter view: the slide from the web deck, the notes of slide 3 ("One").
  await page.waitForSelector(".dw-presenter");
  await page.waitForFunction(() => document.querySelector(".dw-presenter-now iframe")?.dataset.url, null,
                             { timeout: 30000 });
  await page.keyboard.press("Home");
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("ArrowRight");
  assert.equal(await page.locator(".dw-presenter-count").textContent(), "3 of 4");
  assert.equal(await page.locator(".dw-presenter-notes").textContent(), "Say hello.");
  assert.match(await page.locator(".dw-presenter-now iframe").getAttribute("data-url"), /#slide-3$/);
  const frame = page.frameLocator(".dw-presenter-now iframe");
  await frame.locator("#slide-3").getByText("First slide.").waitFor();
  await shot("presenter");
  await page.keyboard.press("Escape");
  assert.equal(await page.locator(".dw-presenter").count(), 0);
  assert.deepEqual(page.problems, []);
});

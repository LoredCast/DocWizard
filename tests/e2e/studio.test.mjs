// The writing studio in a real browser (§6.3): the corkboard moves scenes, the breakdown lists a
// script's scenes and cast and highlights a character's lines, and the daily target counts words.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdirSync } from "node:fs";
import path from "node:path";
import { after, before, describe, test } from "node:test";

import { accessible } from "./a11y.mjs";
import { startServer } from "./server.mjs";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const PASSWORD = process.env.E2E_PASSWORD ?? ["a", "long", "enough", "password"].join(" ");

const NOVEL = [
  "---", "title: Harbour", "---", "",
  "# One", "",
  "Mara arrives at the harbour.", "",
  "***", "",
  "Brick waits in the office.", "",
  "# Two", "",
  "## The storm", "",
  "Rain all night.", "",
].join("\n");

const SCRIPT = [
  "---", "title: Harbour", "---", "",
  "EXT. HARBOUR - DAWN", "",
  "Gulls wheel over the water.", "",
  "MARA", "(quietly)", "You came back.", "",
  "BRICK", "Always.", "",
  "INT. HARBOUR OFFICE - NIGHT", "",
  "MARA", "Again.", "",
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

async function project(layout, path, text) {
  const made = await call("POST", "/api/projects", { name: `Studio ${layout}`, layout, starter: false });
  assert.equal(made.status, 201, JSON.stringify(made.data));
  const id = made.data.id;
  const doc = await call("POST", `/api/projects/${id}/documents`, { path, text });
  assert.ok(doc.status < 300, JSON.stringify(doc.data));
  await page.goto(`/p/${id}/${path}`);
  await page.waitForSelector(".cm-content");
  return id;
}

// The text as the server has it, once it passes *check* (the edit may still be on its way).
async function savedText(check) {
  let text = "";
  for (let i = 0; i < 50; i++) {
    text = await serverText();
    if (check(text)) return text;
    await new Promise((r) => setTimeout(r, 200));
  }
  assert.fail(text);
}

async function serverText() {
  return page.evaluate(async () => {
    const html = await (await fetch(location.pathname, { credentials: "same-origin" })).text();
    const json = new DOMParser().parseFromString(html, "text/html").getElementById("snapshot").textContent;
    return JSON.parse(json).text;
  });
}

// CI keeps screenshots of the studio (E2E_SCREENSHOTS names the folder).
async function shot(name) {
  const dir = process.env.E2E_SCREENSHOTS;
  if (!dir) return;
  mkdirSync(dir, { recursive: true });
  await page.screenshot({ path: path.join(dir, `studio-${name}.png`) }).catch(() => {});
}

async function menu(id) {
  await page.click("#more-actions");
  await page.click(`#${id}`);
}

describe("writing studio", () => {
  before(async () => {
    server = await startServer();
    browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
    const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, baseURL: server.base });
    page = await context.newPage();
    page.problems = [];
    page.on("pageerror", (err) => page.problems.push(err.message));
    await page.goto("/register");
    await page.fill('input[name="username"]', "studio-e2e");
    await page.fill('input[name="password"]', PASSWORD);
    await page.click('form[action="/register"] button[type="submit"]');
    await page.waitForURL(`${server.base}/`);
  });

  after(async () => {
    await browser?.close();
    await server?.stop();
  });

  test("the corkboard moves a scene with the keyboard and by dragging", async () => {
    await project("manuscript-novel", "novel.md", NOVEL);
    await menu("open-corkboard");
    await page.locator(".corkboard .cork-lane").first().waitFor();
    assert.deepEqual(await page.locator(".cork-lane h3 .cork-lane-title").allTextContents(), ["One", "Two"]);
    await shot("corkboard");
    await accessible(page, "corkboard", { include: "#corkboard" });
    const brick = page.locator(".cork-card", { hasText: "Brick waits" });
    await brick.focus();
    await page.keyboard.press("Alt+ArrowUp");
    await savedText((s) => s.includes("# One\n\nBrick waits in the office.\n\n***\n\nMara arrives at the harbour.\n\n# Two"));
    // The moved card keeps the focus; dragging it to the next chapter puts it after the storm.
    assert.ok(await page.locator(".cork-card:focus", { hasText: "Brick waits" }).count());
    const card = page.locator(".cork-card", { hasText: "Brick waits" });
    const lane = page.locator(".cork-lane").nth(1);
    const from = await card.boundingBox();
    const to = await lane.locator(".cork-card").last().boundingBox();
    await page.mouse.move(from.x + 20, from.y + 10);
    await page.mouse.down();
    await page.mouse.move(from.x + 40, from.y + 30, { steps: 4 });
    await page.mouse.move(to.x + 20, to.y + to.height - 2, { steps: 8 });
    await page.mouse.up();
    await savedText((s) => s.endsWith("## The storm\n\nRain all night.\n\n***\n\nBrick waits in the office.\n"));
    // Enter on a card goes to its scene.
    await page.locator(".cork-card", { hasText: "Mara arrives" }).focus();
    await page.keyboard.press("Enter");
    assert.ok(await page.locator(".corkboard").isHidden());
    const line = await page.evaluate(() => {
      const node = getSelection().anchorNode;
      return (node?.nodeType === 1 ? node : node?.parentElement)?.closest(".cm-line")?.textContent;
    });
    assert.equal(line, "Mara arrives at the harbour.");
    assert.deepEqual(page.problems, []);
  });

  test("the daily target counts today's words", async () => {
    await menu("open-target");
    await page.fill('dialog[open] input[name="daily"]', "3");
    await page.click('dialog[open] button[value="save"]');
    const target = page.locator("#session-target");
    await target.waitFor();
    await accessible(page, "status bar with the daily target", { include: ".statusbar" });
    assert.match(await target.textContent(), /Today: 0 of 3 words/);
    await page.locator(".cm-content").click();
    await page.keyboard.press("ControlOrMeta+End");
    await page.keyboard.type("\n\nThree more words.");
    await page.waitForFunction(() => /Today: 3 of 3 words/.test(document.querySelector("#session-target").textContent));
    assert.ok(await target.evaluate((n) => n.classList.contains("target-reached")));
  });

  test("the drafting timeline replays the edits", async () => {
    await menu("open-timeline");
    const dialog = page.locator("dialog[open].timeline-dialog");
    await dialog.waitFor();
    const slider = dialog.locator('input[type="range"]');
    assert.ok(Number(await slider.getAttribute("max")) >= 1);
    // The newest point has the words typed for the daily target, marked as written since the point before.
    await dialog.locator(".timeline-ins", { hasText: "Three more words." }).waitFor();
    await shot("timeline");
    await accessible(page, "drafting timeline", { include: "dialog[open]" });
    await slider.focus();
    await page.keyboard.press("Home");
    await page.waitForFunction(() => !document.querySelector(".timeline-text").textContent.includes("Three more words"));
    assert.match(await dialog.locator(".timeline-text").textContent(), /Mara arrives at the harbour/);
    await dialog.locator("button", { hasText: "Close" }).click();
    assert.deepEqual(page.problems, []);
  });

  test("a script's breakdown lists scenes and cast and highlights a character", async () => {
    await project("screenplay", "harbour.md", SCRIPT);
    await menu("open-breakdown");
    const dialog = page.locator("dialog[open].breakdown-dialog");
    await dialog.waitFor();
    assert.match(await dialog.textContent(), /2 scenes, 2 speaking parts, 2 locations/);
    assert.equal(await dialog.locator("tbody tr").count(), 2);
    await dialog.locator('[role="tab"]', { hasText: "Cast" }).click();
    assert.deepEqual(await dialog.locator("tbody tr td:first-child").allTextContents(), ["MARA", "BRICK"]);
    await shot("breakdown");
    await accessible(page, "breakdown", { include: "dialog[open]" });
    await dialog.locator("tbody tr", { hasText: "MARA" }).locator("button").click();
    await page.locator("#editor .cm-speaker").first().waitFor();
    // MARA's two speeches: a cue, a parenthetical and a line, then a cue and a line.
    assert.equal(await page.locator("#editor .cm-speaker").count(), 5);
    await shot("highlight");
    await page.click("#speaker-chip");
    await page.waitForFunction(() => !document.querySelector("#editor .cm-speaker"));
    assert.ok(await page.locator("#open-corkboard").count());
    assert.deepEqual(page.problems, []);
  });
});

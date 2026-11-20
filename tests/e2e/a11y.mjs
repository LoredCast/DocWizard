// Accessibility checks for the browser tests (§20): axe-core against WCAG 2.1 A and AA. CI installs
// axe-core next to Playwright; without it the checks are skipped with a note. It is evaluated
// through the DevTools protocol, which the pages' CSP doesn't apply to.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
let AXE = null;
try {
  AXE = readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");
} catch {
  console.log("# axe-core isn't installed: accessibility checks are skipped");
}

// No violations axe-core can find on the page as it is now (or in *include*, a selector, such as an
// open dialog). *exclude* lists selectors to leave out, each with its reason where it is used.
export async function accessible(page, where, { include = null, exclude = [] } = {}) {
  if (!AXE) return;
  if (!(await page.evaluate(() => Boolean(window.axe)))) await page.evaluate(AXE);
  const violations = await page.evaluate(async ([inc, ex]) => {
    const context = { exclude: ex.map((s) => [s]), ...(inc ? { include: [[inc]] } : {}) };
    const result = await window.axe.run(context, {
      runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] }, resultTypes: ["violations"],
    });
    return result.violations.map((v) => `${v.id} (${v.impact}): ${v.help}\n    ${v.nodes.slice(0, 5)
      .map((n) => `${n.target.join(" ")} ${n.failureSummary?.split("\n").slice(1, 2).join("") ?? ""}`).join("\n    ")}`);
  }, [include, exclude]);
  assert.deepEqual(violations, [], `${where}: accessibility problems`);
}

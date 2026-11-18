"""Unit checks for web/app/workers/stats.js on screenplays: scenes in the outline, characters
for autocomplete and the page estimate."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATS = ROOT / "web" / "app" / "workers" / "stats.js"

NODE_SCRIPT = r"""
import assert from "node:assert/strict";
const s = await import(process.argv[1]);

const text = [
  "---", "title: Harbour", "---", "",
  "# ACT ONE", "",
  "EXT. HARBOUR - DAWN", "",
  "Gulls wheel over the water.", "",
  "MARA", "(quietly)", "You came back.", "",
  "BRICK (V.O.)", "Always.", "",
  "MARA ^", "Never.", "",
  ".FLASHBACK", "",
  "## int. kitchen - night", "",
  "@McCLANE", "Yippee.", "",
  "CUT TO:", "",
  "EXTRA SAUCE is not a scene.", "",
].join("\n");
const r = s.analyse(text, true);
assert.deepEqual(r.outline.map((h) => [h.level, h.title, h.line]), [
  [1, "ACT ONE", 5], [2, "EXT. HARBOUR - DAWN", 7], [2, "FLASHBACK", 21], [2, "int. kitchen - night", 23]]);
assert.equal(r.script.scenes, 3);
assert.deepEqual(r.script.characters, ["MARA", "BRICK", "McCLANE"]);
assert.equal(r.script.pages, 1);
assert.equal(s.cueName("STEEL (O.S.) ^"), "STEEL");

// About a page a minute: 55 lines of action is one page, 550 ten.
const long = Array.from({ length: 275 }, (_, i) => `Line ${i}.`).join("\n\n");
assert.equal(s.analyse(long, true).script.pages, 10);
// Without the flag nothing script-specific happens.
assert.equal(s.analyse(text).script, undefined);
assert.ok(!s.analyse(text).outline.some((h) => h.scene));
console.log("ok");
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_script_stats():
    result = subprocess.run(["node", "--input-type=module", "-e", NODE_SCRIPT, STATS.as_uri()],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"

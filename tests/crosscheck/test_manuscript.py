"""Unit checks for web/app/editor/manuscript.js: the corkboard's cards and moves, a screenplay's
breakdown, speeches and table read, and the Normseite estimate."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "web" / "app" / "editor" / "manuscript.js"

NODE_SCRIPT = r"""
import assert from "node:assert/strict";
const m = await import(process.argv[1]);

const apply = (text, change) => change ? text.slice(0, change.from) + change.insert + text.slice(change.to) : text;

// -- Corkboard: a novel with scene breaks ------------------------------------------
const novel = [
  "---", "title: Harbour", "---", "",
  "A prologue in the fog.", "",
  "# One", "",
  "Mara arrives at the harbour.", "",
  "***", "",
  "Brick waits in the office.", "",
  "# Two", "",
  "## The storm", "",
  "Rain all night.", "",
].join("\n");
let b = m.board(novel);
assert.deepEqual(b.lanes.map((l) => l.title), [null, "One", "Two"]);
assert.deepEqual(b.lanes.map((l) => l.cards.map((c) => c.kind)), [["body"], ["body", "break"], ["heading"]]);
assert.equal(b.lanes[1].cards[1].synopsis, "Brick waits in the office.");
assert.equal(b.lanes[2].cards[0].title, "The storm");
assert.equal(b.lanes[2].cards[0].words, 3);
assert.equal(b.breakText, "***");

// Swap the two scenes of chapter One: the break stays between them.
let id = b.lanes[1].cards[1].id;
let out = apply(novel, m.moveCard(novel, b, id, 1, 0));
assert.ok(out.includes("# One\n\nBrick waits in the office.\n\n***\n\nMara arrives at the harbour.\n\n# Two"), out);
assert.ok(out.startsWith("---\ntitle: Harbour\n---\n\nA prologue in the fog.\n\n# One"));
// Moving it back gives the original text.
b = m.board(out);
id = b.lanes[1].cards[1].id;
assert.equal(apply(out, m.moveCard(out, b, id, 1, 0)), novel);

// A scene into another chapter, at its end; the document's last newline is kept.
b = m.board(novel);
out = apply(novel, m.moveCard(novel, b, b.lanes[1].cards[0].id, 2));
assert.ok(out.endsWith("## The storm\n\nRain all night.\n\n***\n\nMara arrives at the harbour.\n"), out);
assert.ok(out.includes("# One\n\nBrick waits in the office.\n\n# Two"), out);

// Chapters move as a whole; the opening stays first.
b = m.board(novel);
out = apply(novel, m.moveCard(novel, b, b.lanes[0].cards[0].id, 0, 0));
assert.equal(out, novel, "a card dropped where it was changes nothing");
out = apply(novel, m.moveLane(novel, b, 2, 0));
assert.ok(out.includes("A prologue in the fog.\n\n# Two\n\n## The storm\n\nRain all night.\n\n# One\n\nMara"), out);
assert.ok(out.endsWith("Brick waits in the office.\n"), JSON.stringify(out.slice(-40)));
assert.equal(m.moveLane(novel, b, 0, 2), null, "the opening has no heading to move");

// The change is small: text before and after the moved part is untouched.
b = m.board(novel);
const ch = m.moveCard(novel, b, b.lanes[1].cards[1].id, 1, 0);
assert.ok(ch.from > novel.indexOf("# One"), "starts after the chapter heading");

// -- Screenplays ---------------------------------------------------------------
const script = [
  "---", "title: Harbour", "---", "",
  "# ACT ONE", "",
  "EXT. HARBOUR - DAWN", "",
  "Gulls wheel over the water.", "",
  "MARA", "(quietly)", "You came back.", "",
  "BRICK (V.O.)", "Always. Every *single* time.", "",
  "INT. HARBOUR OFFICE - NIGHT #4#", "",
  "MARA", "Again.", "",
  "CUT TO:", "",
  "## int. harbour office - day", "",
  "@McCLANE", "Yippee.", "",
].join("\n");
b = m.board(script, { script: true });
assert.deepEqual(b.lanes.map((l) => l.title), ["ACT ONE"]);
assert.deepEqual(b.lanes[0].cards.map((c) => c.title), ["EXT. HARBOUR - DAWN", "INT. HARBOUR OFFICE - NIGHT #4#", "int. harbour office - day"]);
out = apply(script, m.moveCard(script, b, b.lanes[0].cards[2].id, 0, 0));
assert.ok(out.includes("# ACT ONE\n\n## int. harbour office - day\n\n@McCLANE\nYippee.\n\nEXT. HARBOUR - DAWN"), out);

assert.deepEqual(m.parseSceneHeading("INT./EXT. CAR – MOVING"), { setting: "INT./EXT", location: "CAR", time: "MOVING" });
assert.deepEqual(m.parseSceneHeading("INNEN. KÜCHE - NACHT"), { setting: "INNEN", location: "KÜCHE", time: "NACHT" });
assert.deepEqual(m.parseSceneHeading("EXT. ROAD - LATER - DAY"), { setting: "EXT", location: "ROAD - LATER", time: "DAY" });

const bd = m.breakdown(script);
assert.deepEqual(bd.scenes.map((s) => [s.n, s.setting, s.location, s.time, s.cast]), [
  [1, "EXT", "HARBOUR", "DAWN", ["MARA", "BRICK"]],
  [2, "INT", "HARBOUR OFFICE", "NIGHT", ["MARA"]],
  [3, "INT", "HARBOUR OFFICE", "DAY", ["McCLANE"]],
]);
assert.ok(bd.scenes.every((s) => s.eighths >= 1));
assert.equal(bd.scenes[0].act, "ACT ONE");
assert.deepEqual(bd.cast.map((c) => [c.name, c.speeches, c.words, c.scenes]), [
  ["MARA", 2, 4, [1, 2]], ["BRICK", 1, 4, [1]], ["McCLANE", 1, 1, [3]]]);
assert.deepEqual(bd.locations.map((l) => [l.name, l.scenes, l.times]), [
  ["HARBOUR OFFICE", [2, 3], ["NIGHT", "DAY"]], ["HARBOUR", [1], ["DAWN"]]]);

assert.deepEqual(m.speechLines(script, "mara"), [[11, 13], [20, 21]]);
const read = m.readingScript(script, 15);
assert.deepEqual(read.map((r) => [r.who, r.text]), [
  ["BRICK", "Always. Every single time."], [null, "INT. HARBOUR OFFICE - NIGHT #4#"], ["MARA", "Again."],
  [null, "CUT TO:"], [null, "int. harbour office - day"], ["McCLANE", "Yippee."]]);
assert.deepEqual(m.readingScript(script, 1, { action: false }).map((r) => r.who ?? r.kind).slice(0, 4),
                 ["act", "scene", "MARA", "BRICK"]);

// -- Normseite -------------------------------------------------------------------
assert.equal(m.normseiten(""), 0);
// 30 paragraphs of one short line: one page.
const page = Array.from({ length: 30 }, (_, i) => `Zeile ${i}.`).join("\n\n");
assert.equal(m.normseiten(page), 1);
// 300 words of five letters in one paragraph: ten to a line of 60, so 30 lines, one page.
assert.equal(m.normseiten(Array.from({ length: 300 }, () => "Worte").join(" ")), 1);
assert.equal(m.normseiten(Array.from({ length: 300 }, () => "Wörter").join(" ")), 1.3);
// Chapters start on a new page.
assert.equal(m.normseiten("# Eins\n\nKurz.\n\n# Zwei\n\nKurz.\n"), 1.1);
assert.equal(m.plain("A **bold** [@doe2020, p. 4] word^[a note] and [a link](x)."), "A bold word and a link.");
console.log("ok");
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_manuscript_models():
    result = subprocess.run(["node", "--input-type=module", "-e", NODE_SCRIPT, MODULE.as_uri()],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"

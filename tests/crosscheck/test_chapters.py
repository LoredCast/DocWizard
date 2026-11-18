"""File names for new chapters in the editor's Chapters drawer (web/app/editor/chapters.js)."""

import shutil
import subprocess
from pathlib import Path

import pytest

CHAPTERS = Path(__file__).resolve().parents[2] / "web" / "app" / "editor" / "chapters.js"

NODE_SCRIPT = r"""
import assert from "node:assert/strict";
const ch = await import(process.argv[1]);

// Names as the starters write them: lower case, ASCII, umlauts spelled out.
assert.equal(ch.slugify("Über die Straßenbahn"), "ueber-die-strassenbahn");
assert.equal(ch.slugify("Résultats & discussion!"), "resultats-discussion");
assert.equal(ch.slugify("???"), "chapter");

// Next to the last chapter, numbered after every numbered file in that folder.
const book = ["thesis.md", "chapters/01-introduction.md", "chapters/02-background.md"];
assert.equal(ch.newChapterPath(book, "thesis.md", "Methods", new Set(book)), "chapters/03-methods.md");
assert.equal(ch.newChapterPath(book, "thesis.md", "Methods", new Set([...book, "chapters/07-old.md"])), "chapters/08-methods.md");
// Without a book, next to the open document; a taken name gets a number.
assert.equal(ch.newChapterPath([], "notes/paper.md", "Next steps", new Set(["notes/paper.md"])), "notes/next-steps.md");
assert.equal(ch.newChapterPath([], "paper.md", "Next", new Set(["next.md"])), "next-2.md");
console.log("ok");
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_new_chapter_paths():
    result = subprocess.run(["node", "--input-type=module", "-e", NODE_SCRIPT, CHAPTERS.as_uri()],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.strip() == "ok"

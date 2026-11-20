"""The editor's reading of a slide deck (web/app/editor/slides-model.js) matches filters/slides.lua,
so the slide sorter and the presenter view number slides as the web deck does."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "web" / "app" / "editor" / "slides-model.js"
FILTER = ROOT / "filters" / "slides.lua"

DECKS = [
    # Title slide, sections, slides, notes, columns, pauses, a rule, setext headings, code.
    """---
title: A talk
author: Ana
---

# Part one

## First

- a
- b

::: notes
Say this.
:::

## Second

:::::: columns
::: column
Left
:::
::: column
Right
:::
::::::

. . .

More

---

Untitled slide

Setext heading
--------------

```
## not a heading
---
```

Part two
========

## Third [@doe]
""",
    # No title, level-1 slides (automatic level), text before the first heading.
    """Intro text.

# One

Text.

# Two

::: {.callout}
## inside a div
:::
""",
    # An explicit level.
    """---
slide-level: 1
---

# A

## a1

text

# B
""",
]

SCRIPT = r"""
const m = await import(process.argv[1]);
const decks = JSON.parse(process.argv[2]);
console.log(JSON.stringify(decks.map((d) => m.parseSlides(d).slides.map((s) => [s.kind, s.title]))));
"""

MOVE = r"""
import assert from "node:assert/strict";
const m = await import(process.argv[1]);
const text = "---\ntitle: T\n---\n\n## One\n\na\n\n## Two\n\nb\n\n## Three\n\nc\n";
const deck = m.parseSlides(text);
assert.deepEqual(deck.slides.map((s) => s.title), ["T", "One", "Two", "Three"]);
// Three before One.
const moved = m.moveSlide(text, deck, 3, 1);
assert.equal(moved, "---\ntitle: T\n---\n\n## Three\n\nc\n\n## One\n\na\n\n## Two\n\nb\n");
// One to the end; moving onto itself does nothing; the title slide stays put.
assert.equal(m.moveSlide(text, deck, 1, 4), "---\ntitle: T\n---\n\n## Two\n\nb\n\n## Three\n\nc\n\n## One\n\na\n");
assert.equal(m.moveSlide(text, deck, 2, 2), null);
assert.equal(m.moveSlide(text, deck, 0, 3), null);
// Notes belong to their slide.
const n = m.parseSlides("## A\n\n::: notes\nhello\n:::\n\n## B\n");
assert.equal(n.slides[0].notes, "hello");
assert.equal(n.slides[1].notes, "");
"""

needs = pytest.mark.skipif(shutil.which("node") is None or shutil.which("pandoc") is None,
                           reason="needs node and pandoc")


def _lua_slides(text: str) -> list[list[str]]:
    html = subprocess.run(["pandoc", "--from", "markdown", "--to", "html5", "--lua-filter", str(FILTER),
                           "--metadata", "reference-section-title=References"],
                          input=text, capture_output=True, text=True, check=True).stdout
    out = []
    for m in re.finditer(r'<div id="slide-(\d+)" class="slide ([\w-]+)[^"]*"[^>]*>(.*?)<(?:div|section)[^>]*class="slide-body"',
                         html, re.S):
        kind = {"content-slide": "slide"}.get(m.group(2), m.group(2).removesuffix("-slide"))
        title = re.search(r'<div class="slide-title"[^>]*>(.*?)</div>', m.group(3), re.S)
        plain = re.sub(r"<[^>]+>", "", title.group(1)).strip() if title else ""
        out.append([kind, plain])
    return out


@needs
def test_slide_numbers_match_the_filter():
    r = subprocess.run(["node", "--input-type=module", "-e", SCRIPT, MODEL.as_uri(), json.dumps(DECKS)],
                       capture_output=True, text=True, check=True)
    js = json.loads(r.stdout)
    for deck, ours in zip(DECKS, js, strict=True):
        lua = _lua_slides(deck)
        # The filter titles the references slide; the editor only needs to know it is there.
        ours = [[k, "References" if k == "references" else t.replace("[@doe]", "").strip()] for k, t in ours]
        lua = [[("references" if t == "References" and k == "slide" else k), t.replace("[@doe]", "").strip()]
               for k, t in lua]
        assert ours == lua, (deck, ours, lua)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_moving_slides():
    subprocess.run(["node", "--input-type=module", "-e", MOVE, MODEL.as_uri()], check=True)

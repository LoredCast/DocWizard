"""Unit checks for Markdown formatting commands in web/app/editor/commands.js."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "web" / "vendor" / "codemirror.js"
COMMANDS = ROOT / "web" / "app" / "editor" / "commands.js"
PANDOC_SYNTAX = ROOT / "web" / "app" / "editor" / "pandoc-syntax.js"

NODE_SCRIPT = r"""
import assert from "node:assert/strict";

const [vendorUrl, commandsUrl, pandocUrl] = process.argv.slice(1);
const cm = await import(vendorUrl);
const { pandocExtensions } = await import(pandocUrl);
const cmd = await import(commandsUrl);

const mdLang = new cm.Language(
  cm.defineLanguageFacet({}),
  cm.markdownParser.configure([cm.GFM, ...pandocExtensions]),
  [],
  "markdown",
);

function makeView(doc, from, to = from) {
  let state = cm.EditorState.create({
    doc,
    selection: cm.EditorSelection.range(from, to),
    extensions: [mdLang],
  });
  return {
    get state() { return state; },
    dispatch(tr) {
      state = (tr.startState ? tr : state.update(tr)).state;
    },
  };
}

// 1. Bold & unbold on plain selection, trailing space trimming, and inclusive selection.
{
  const v = makeView("hello world", 0, 5);
  cmd.toggleBold(v);
  assert.equal(v.state.doc.toString(), "**hello** world");
  assert.equal(v.state.selection.main.from, 2);
  assert.equal(v.state.selection.main.to, 7);
  cmd.toggleBold(v);
  assert.equal(v.state.doc.toString(), "hello world");

  // Selecting including markers unbolds cleanly.
  const v2 = makeView("**hello** world", 0, 9);
  cmd.toggleBold(v2);
  assert.equal(v2.state.doc.toString(), "hello world");

  // Leading/trailing whitespace is kept outside delimiters.
  const v3 = makeView("hello world", 0, 6);
  cmd.toggleBold(v3);
  assert.equal(v3.state.doc.toString(), "**hello** world");
}

// 2. Bold + Italic composition never corrupts "**hello**" into "*hello*".
{
  const v = makeView("**hello**", 2, 7);
  cmd.toggleItalic(v);
  assert.equal(v.state.doc.toString(), "***hello***");
  cmd.toggleItalic(v);
  assert.equal(v.state.doc.toString(), "**hello**");
  cmd.toggleBold(v);
  assert.equal(v.state.doc.toString(), "hello");
}

// 3. Empty selection inserts empty markers, exits at end of span, and removes empty pair.
{
  const v = makeView("ab", 1);
  cmd.toggleBold(v);
  assert.equal(v.state.doc.toString(), "a****b");
  assert.equal(v.state.selection.main.head, 3);
  cmd.toggleBold(v);
  assert.equal(v.state.doc.toString(), "ab");

  const vEnd = makeView("**hello**", 7);
  cmd.toggleBold(vEnd);
  assert.equal(vEnd.state.doc.toString(), "**hello**");
  assert.equal(vEnd.state.selection.main.head, 9);
}

// 4. Strikethrough, inline code, inline math.
{
  const v = makeView("x + y", 0, 5);
  cmd.toggleInlineMath(v);
  assert.equal(v.state.doc.toString(), "$x + y$");
  assert.equal(cmd.activeFormatting(v.state).math, true);
  cmd.toggleInlineMath(v);
  assert.equal(v.state.doc.toString(), "x + y");

  const vs = makeView("old", 0, 3);
  cmd.toggleStrike(vs);
  assert.equal(vs.state.doc.toString(), "~~old~~");
  cmd.toggleStrike(vs);
  assert.equal(vs.state.doc.toString(), "old");

  const vc = makeView("fn()", 0, 4);
  cmd.toggleCode(vc);
  assert.equal(vc.state.doc.toString(), "`fn()`");
  cmd.toggleCode(vc);
  assert.equal(vc.state.doc.toString(), "fn()");
}

// 5. Headings, blockquotes, bullet/numbered/task lists.
{
  const vh = makeView("Title", 0);
  cmd.cycleHeading(vh);
  assert.equal(vh.state.doc.toString(), "# Title");
  assert.equal(cmd.activeFormatting(vh.state).heading, 1);
  cmd.cycleHeading(vh);
  assert.equal(vh.state.doc.toString(), "## Title");
  cmd.cycleHeading(vh);
  assert.equal(vh.state.doc.toString(), "### Title");
  cmd.cycleHeading(vh);
  assert.equal(vh.state.doc.toString(), "Title");

  const vl = makeView("one\ntwo", 0, 7);
  cmd.toggleBulletList(vl);
  assert.equal(vl.state.doc.toString(), "- one\n- two");
  cmd.toggleOrderedList(vl);
  assert.equal(vl.state.doc.toString(), "1. one\n2. two");
  cmd.toggleTaskList(vl);
  assert.equal(vl.state.doc.toString(), "- [ ] one\n- [ ] two");
  cmd.toggleTaskList(vl);
  assert.equal(vl.state.doc.toString(), "one\ntwo");

  const vq = makeView("one\ntwo", 0, 7);
  cmd.toggleQuote(vq);
  assert.equal(vq.state.doc.toString(), "> one\n> two");
  cmd.toggleQuote(vq);
  assert.equal(vq.state.doc.toString(), "one\ntwo");
}

// 6. Link, citation, and footnote insertions & unwrapping.
{
  const vUrl = makeView("https://example.org", 0, 19);
  cmd.insertLink(vUrl);
  assert.equal(vUrl.state.doc.toString(), "[](https://example.org)");
  assert.equal(vUrl.state.selection.main.head, 1);

  const vTxt = makeView("click here", 0, 10);
  cmd.insertLink(vTxt);
  assert.equal(vTxt.state.doc.toString(), "[click here](url)");
  assert.equal(vTxt.state.sliceDoc(vTxt.state.selection.main.from, vTxt.state.selection.main.to), "url");
  cmd.insertLink(vTxt);
  assert.equal(vTxt.state.doc.toString(), "click here");

  const vCite = makeView("smith", 0, 5);
  cmd.insertCitation(vCite);
  assert.equal(vCite.state.doc.toString(), "[@smith]");

  const vFn = makeView("aside", 0, 5);
  cmd.insertFootnote(vFn);
  assert.equal(vFn.state.doc.toString(), "^[aside]");
  cmd.insertFootnote(vFn);
  assert.equal(vFn.state.doc.toString(), "aside");
}
"""


@pytest.mark.skipif(shutil.which("node") is None or not VENDOR.exists(), reason="needs node and vendored codemirror")
def test_markdown_commands(tmp_path):
    vendor_uri = VENDOR.as_uri()
    cmd_tmp = tmp_path / "commands.mjs"
    pandoc_tmp = tmp_path / "pandoc-syntax.mjs"
    cmd_tmp.write_text(COMMANDS.read_text().replace('from "codemirror"', f'from "{vendor_uri}"'))
    pandoc_tmp.write_text(PANDOC_SYNTAX.read_text().replace('from "codemirror"', f'from "{vendor_uri}"'))
    subprocess.run(
        ["node", "--input-type=module", "-e", NODE_SCRIPT, vendor_uri, cmd_tmp.as_uri(), pandoc_tmp.as_uri()],
        check=True,
        capture_output=True,
        text=True,
    )

"""Formatted text pasted from Word, Google Docs and web pages (web/app/editor/paste-html.js)."""

import shutil
import subprocess
from pathlib import Path

import pytest

PASTE = Path(__file__).resolve().parents[2] / "web" / "app" / "editor" / "paste-html.js"

NODE_SCRIPT = r"""
import assert from "node:assert/strict";
const { htmlToMarkdown, escapeText } = await import(process.argv[1]);

// A small HTML parser, enough for the clipboards below: elements, attributes, text, comments.
const VOID = new Set(["br", "img", "hr", "meta", "link", "input", "col"]);
function node(name) {
  const attrs = {};
  return { nodeType: 1, nodeName: name.toUpperCase(), childNodes: [], attrs, getAttribute: (a) => attrs[a] ?? null };
}
function parse(html) {
  const body = node("body");
  const stack = [body];
  const re = /<!--[\s\S]*?-->|<!\[[^\]]*\]>|<\/?([a-zA-Z][\w:-]*)((?:\s+[\w:-]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*\/?>|([^<]+)/g;
  let m;
  while ((m = re.exec(html))) {
    const top = stack[stack.length - 1];
    if (m[3] !== undefined) {
      const text = m[3].replace(/&nbsp;/g, "\u00a0").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
      top.childNodes.push({ nodeType: 3, nodeName: "#text", nodeValue: text, childNodes: [] });
    } else if (m[1]) {
      const name = m[1].toLowerCase();
      if (m[0].startsWith("</")) {
        const at = stack.map((n) => n.nodeName).lastIndexOf(name.toUpperCase());
        if (at > 0) stack.length = at;
        continue;
      }
      const el = node(name);
      for (const a of m[2].matchAll(/([\w:-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) {
        el.attrs[a[1].toLowerCase()] = a[2] ?? a[3] ?? a[4] ?? "";
      }
      top.childNodes.push(el);
      if (!VOID.has(name) && !m[0].endsWith("/>")) stack.push(el);
    }
  }
  return body;
}
const md = (html) => htmlToMarkdown(parse(html)).markdown;

// 1. Emphasis, links and headings from a web page.
assert.equal(md('<h2>Results <b>so far</b></h2><p>We found <b>three</b> and <i>maybe</i> a <a href="https://ex.org/a(b)">fourth</a>.</p>'),
  "## Results so far\n\nWe found **three** and *maybe* a [fourth](https://ex.org/a%28b%29).");
// Touching marks merge; spaces move outside them; empty ones vanish.
assert.equal(md("<p><b>one </b><b>two</b> <i> </i>three</p>"), "**one two** three");
assert.equal(md("<p><b><i>both</i></b> x<sup>2</sup> H<sub>2</sub>O <s>gone</s></p>"), "***both*** x^2^ H~2~O ~~gone~~");
// A link whose text is the address becomes an autolink; links elsewhere than the web stay text.
assert.equal(md('<p><a href="https://ex.org">https://ex.org</a> <a href="#top">up</a> <a href="javascript:x()">no</a></p>'),
  "<https://ex.org> up no");

// 2. Google Docs: an outer <b style="font-weight:normal">, spans with styles, redirect links, nested lists.
const gdocs = '<meta charset="utf-8"><b style="font-weight:normal;" id="docs-internal-guid-1"><p dir="ltr"><span style="font-weight:700;">Bold</span><span style="font-weight:400;"> and </span><span style="font-style:italic;">italic</span><span> and </span><a href="https://www.google.com/url?q=https://example.org/page&amp;sa=D"><span style="text-decoration:underline;">a link</span></a></p><ul><li dir="ltr" aria-level="1"><p dir="ltr"><span>First</span></p></li><ul><li aria-level="2"><p><span>Inner</span></p></li></ul><li aria-level="1"><p><span>Second</span></p></li></ul><br><p dir="ltr"><span style="font-family:\'Courier New\';">code()</span></p></b>';
assert.equal(md(gdocs), "**Bold** and *italic* and [a link](https://example.org/page)\n\n- First\n  - Inner\n- Second\n\n`code()`");

// 3. Word: list paragraphs with mso-list and a bullet span to ignore, <o:p>, nbsp.
const word = `<p class=MsoNormal><b>Heading-ish</b> text<o:p></o:p></p>
<p class=MsoListParagraphCxSpFirst style='text-indent:-18.0pt;mso-list:l0 level1 lfo1'><![if !supportLists]><span style='mso-list:Ignore'>1.<span style='font:7.0pt "Times New Roman"'>&nbsp;&nbsp; </span></span><![endif]>One<o:p></o:p></p>
<p class=MsoListParagraphCxSpMiddle style='mso-list:l0 level2 lfo1'><![if !supportLists]><span style='mso-list:Ignore'>a.<span>&nbsp; </span></span><![endif]>Sub<o:p></o:p></p>
<p class=MsoListParagraphCxSpLast style='mso-list:l0 level1 lfo1'><![if !supportLists]><span style='mso-list:Ignore'>2.<span>&nbsp; </span></span><![endif]>Two<o:p></o:p></p>
<p class=MsoNormal><o:p>&nbsp;</o:p></p>
<p class=MsoNormal style='mso-list:l1 level1 lfo2'><span style='mso-list:Ignore'>·<span>&nbsp;</span></span>Dot</p>`;
assert.equal(md(word), "**Heading-ish** text\n\n1. One\n    1. Sub\n2. Two\n\n- Dot");

// 4. Tables: a header row, escaped pipes, padded columns, colspan; a layout table gives its content.
assert.equal(md("<table><tr><td><b>Name</b></td><td>Value</td></tr><tr><td>a|b</td><td>1<br>2</td></tr><tr><td colspan=2>wide</td></tr></table>"),
  "| **Name** | Value |\n| -------- | ----- |\n| a\\|b     | 1 2   |\n| wide     |       |");
assert.equal(md("<table><tr><td><h1>Site</h1></td><td><ul><li>menu</li></ul></td></tr></table>"), "# Site\n\n- menu");

// 5. Quotes, code blocks, rules, line breaks, pictures left out.
assert.equal(md("<blockquote><p>Said <i>so</i>.</p><p>Twice.</p></blockquote><hr><pre>a  *b*\n  c</pre>"),
  "> Said *so*.\n>\n> Twice.\n\n* * *\n\n```\na  *b*\n  c\n```");
assert.equal(md("<p>line one<br>line two<br></p>"), "line one\\\nline two");
const pic = htmlToMarkdown(parse('<p><b>x</b><img src="file:///C:/clip.png"></p>'));
assert.equal(pic.markdown, "**x**");
assert.equal(pic.images, 1);

// 6. Text that would become Markdown stays text.
assert.equal(md("<p><b>Note:</b> 1. not a list, *stars*, $5 and @doe or me@ex.org [x](y) ^[n] snake_case _x_</p>"),
  "**Note:** 1. not a list, \\*stars\\*, \\$5 and \\@doe or me@ex.org [x\\](y) \\^[n] snake_case \\_x\\_");
assert.equal(md("<p><b>x</b></p><p># not a heading</p><p>- not a list</p><p>2. no</p>"),
  "**x**\n\n\\# not a heading\n\n\\- not a list\n\n2\\. no");
assert.equal(escapeText("a|b", { table: true }), "a\\|b");

// 7. Nothing formatted: the browser's own plain-text paste is better; code editors' copies too.
assert.equal(htmlToMarkdown(parse("<p>just text</p>")).formatted, false);
assert.equal(htmlToMarkdown(parse('<meta charset="utf-8"><div style="color: #d4d4d4;white-space: pre;"><div><span>let</span> <b>x</b></div></div>')).formatted, false);
// Headings don't repeat their bold; a web page's footnote references are left out.
assert.equal(md('<h1><span style="font-weight:700">Title</span></h1><p>Text<sup><a href="#cite-1">[1]</a></sup>.</p>'), "# Title\n\nText.");
// Loose lists keep their paragraphs apart.
assert.equal(md("<ol start=3><li><p>A</p><p>more</p></li><li>B</li></ol>"), "3. A\n\n   more\n\n4. B");
console.log("ok");
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_html_becomes_markdown():
    result = subprocess.run(["node", "--input-type=module", "-e", NODE_SCRIPT, PASTE.as_uri()],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.strip() == "ok"

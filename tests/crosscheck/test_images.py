"""Checks for pasted and dropped images in web/app/editor/images.js."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "web" / "vendor" / "codemirror.js"
IMAGES = ROOT / "web" / "app" / "editor" / "images.js"

DOM_STUB = """
export const calls = [];
export let before = null;
export function setBefore(fn) { before = fn; }
export async function api(method, url, body) {
  calls.push([method, url]);
  if (before) await before();
  const want = decodeURIComponent(url.split("/assets/")[1].split("?")[0]);
  return { path: want.replace("figure.", "figure-2.") };
}
export function t(message, values = {}) {
  return message.replace(/\\{(\\w+)\\}/g, (m, name) => (name in values ? String(values[name]) : m));
}
export function toast() {}
"""

NODE_SCRIPT = r"""
import assert from "node:assert/strict";

const [vendorUrl, imagesUrl, domUrl] = process.argv.slice(1);
const cm = await import(vendorUrl);
const img = await import(imagesUrl);
const dom = await import(domUrl);

// Names: generic clipboard names become "figure", others a slug of the file name.
assert.equal(img.slug("image.png"), "figure");
assert.equal(img.slug("Screenshot.png"), "figure");
assert.equal(img.slug(""), "figure");
assert.equal(img.slug("Karte der Straßenbahn (1910).JPG"), "karte-der-strassenbahn-1910");
assert.equal(img.slug("Übersicht.png"), "ubersicht");

// The text a figure becomes.
assert.equal(img.figureText("markdown", "figures/map.png", "map"), "![Caption](figures/map.png){#fig:map}");
assert.match(img.figureText("latex", "figures/map.png", "map"),
             /\\includegraphics\[width=\\linewidth\]\{figures\/map\.png\}[\s\S]*\\label\{fig:map\}/);

// A figure stands as its own paragraph.
const S = (doc) => cm.EditorState.create({ doc });
const place = (doc, pos) => { const p = img.placeFigure(S(doc), pos, "F"); return doc.slice(0, p.at) + p.insert + doc.slice(p.at); };
assert.equal(place("", 0), "F\n");
assert.equal(place("One.\n\nTwo.", 5), "One.\n\nF\n\nTwo.");
assert.equal(place("One.\n\n\nTwo.", 6), "One.\n\nF\n\nTwo.");
assert.equal(place("One two.\nNext", 3), "One two.\n\nF\n\nNext");  // inside a line: after it
assert.equal(place("End.", 4), "End.\n\nF\n");

// The whole flow: the placeholder follows edits made while the upload runs.
function makeView(doc, pos) {
  const inserter = new img.ImageInserter({ projectId: "p1", kind: "markdown" });
  let state = cm.EditorState.create({ doc, selection: cm.EditorSelection.cursor(pos), extensions: [inserter.extension()] });
  const view = {
    get state() { return state; },
    dispatch(spec) { state = state.update(spec).state; },
    focus() {},
  };
  return { view, inserter };
}
{
  const { view, inserter } = makeView("Intro.\n\nMore text.", 6);
  dom.setBefore(async () => view.dispatch({ changes: { from: 0, insert: "New first line.\n\n" } }));
  await inserter.insert(view, [{ name: "image.png", type: "image/png" }], 6);
  assert.equal(view.state.doc.toString(),
               "New first line.\n\nIntro.\n\n![Caption](figures/figure-2.png){#fig:figure-2}\n\nMore text.");
  const sel = view.state.selection.main;
  assert.equal(view.state.sliceDoc(sel.from, sel.to), "Caption");
  assert.equal(dom.calls[0][0], "PUT");
  assert.equal(dom.calls[0][1], "/api/projects/p1/assets/figures/figure.png?unique=1");
  dom.setBefore(null);
}
// Two images in a row follow each other.
{
  const { view, inserter } = makeView("Text.", 5);
  await inserter.insert(view, [{ name: "a.png", type: "image/png" }, { name: "b.jpg", type: "image/jpeg" }], 5);
  assert.equal(view.state.doc.toString(),
               "Text.\n\n![Caption](figures/a.png){#fig:a}\n\n![Caption](figures/b.jpg){#fig:b}\n");
}
// LaTeX documents don't take SVG.
{
  const inserter = new img.ImageInserter({ projectId: "p1", kind: "latex" });
  assert.equal(inserter.accepts({ type: "image/svg+xml" }), false);
  assert.equal(inserter.accepts({ type: "application/pdf" }), true);
}
"""


@pytest.mark.skipif(shutil.which("node") is None or not VENDOR.exists(), reason="needs node and vendored codemirror")
def test_images(tmp_path):
    vendor_uri = VENDOR.as_uri()
    dom = tmp_path / "dom.mjs"
    dom.write_text(DOM_STUB)
    images = tmp_path / "images.mjs"
    images.write_text(IMAGES.read_text().replace('from "codemirror"', f'from "{vendor_uri}"')
                      .replace('from "../ui/dom.js"', f'from "{dom.as_uri()}"'))
    result = subprocess.run(["node", "--input-type=module", "-e", NODE_SCRIPT, vendor_uri, images.as_uri(), dom.as_uri()],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

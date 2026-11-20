"""Unit checks for web/app/preview/html.js navigation and error handling."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HTML_PREVIEW = ROOT / "web" / "app" / "preview" / "html.js"

NODE_SCRIPT = r"""
import assert from "node:assert/strict";

const docListeners = {};
globalThis.location = { href: "https://write.example.org/p/p1/paper.md" };
globalThis.document = {
  addEventListener(type, fn) { (docListeners[type] ??= []).push(fn); },
};

const { HtmlPreview, headingId } = await import(process.argv[1]);

function makeIframe() {
  const listeners = {};
  const replaced = [];
  let srcVal = "";
  const iframe = {
    addEventListener(type, fn) { (listeners[type] ??= []).push(fn); },
    emit(type) { for (const fn of listeners[type] ?? []) fn(); },
    get src() { return srcVal; },
    set src(v) { srcVal = v; },
    contentWindow: {
      location: {
        replace(v) { replaced.push(v); },
      },
    },
    replaced,
  };
  return iframe;
}

// 1. Initial show uses iframe.src; scroll after load uses location.replace.
{
  let loads = 0, errors = 0;
  const iframe = makeIframe();
  const hp = new HtmlPreview(iframe, { onLoad: () => { loads++; }, onError: () => { errors++; } });
  hp.show("https://pub.example.org/_preview/tok/key/index.html");
  assert.equal(iframe.src, "https://pub.example.org/_preview/tok/key/index.html");
  assert.equal(iframe.replaced.length, 0);
  iframe.emit("load");
  assert.equal(loads, 1);

  hp.scrollTo("introduction");
  assert.deepEqual(iframe.replaced, ["https://pub.example.org/_preview/tok/key/index.html#introduction"]);
  assert.equal(iframe.src, "https://pub.example.org/_preview/tok/key/index.html");

  // Reload resets and re-triggers navigation.
  hp.reload();
  assert.equal(iframe.src, "https://pub.example.org/_preview/tok/key/index.html#introduction");
  iframe.emit("load");
  assert.equal(loads, 2);
  assert.equal(errors, 0);
}

// 2. Security policy violation triggers onError callback.
{
  let failedUrl = "";
  const iframe = makeIframe();
  const hp = new HtmlPreview(iframe, { onError: (u) => { failedUrl = u; } });
  hp.show("https://pub.example.org/_preview/tok/key/index.html");
  for (const fn of docListeners.securitypolicyviolation ?? []) {
    fn({ effectiveDirective: "frame-src" });
  }
  assert.equal(failedUrl, "https://pub.example.org/_preview/tok/key/index.html");
  // After an error, calling show with the same URL retries instead of staying stuck.
  iframe.src = "";
  hp.show("https://pub.example.org/_preview/tok/key/index.html");
  assert.equal(iframe.src, "https://pub.example.org/_preview/tok/key/index.html");
}

// 3. Hash scroll during initial load waits until load finishes so it never interrupts CSS/doc loading.
{
  const iframe = makeIframe();
  const hp = new HtmlPreview(iframe);
  hp.show("https://pub.example.org/_preview/tok/key/index.html");
  assert.equal(iframe.src, "https://pub.example.org/_preview/tok/key/index.html");
  hp.scrollTo("methods");
  assert.equal(iframe.replaced.length, 0);
  assert.equal(iframe.src, "https://pub.example.org/_preview/tok/key/index.html");
  iframe.emit("load");
  assert.deepEqual(iframe.replaced, ["https://pub.example.org/_preview/tok/key/index.html#methods"]);
}

assert.equal(headingId("Hello *World*!"), "hello-world");
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_html_preview_navigation_and_error_handling():
    subprocess.run(
        ["node", "--input-type=module", "-e", NODE_SCRIPT, HTML_PREVIEW.as_uri()],
        check=True,
        capture_output=True,
        text=True,
    )

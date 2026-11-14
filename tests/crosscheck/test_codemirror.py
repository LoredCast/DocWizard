"""Python ChangeSet results must match CodeMirror's (§20, cross-check).

Generates random cases in Python, evaluates them with the vendored
CodeMirror bundle in Node, and compares.
"""

import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from docwizard.collab.changeset import ChangeSet, u16len

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "web" / "vendor" / "codemirror.js"

NODE_SCRIPT = r"""
const { ChangeSet, Text } = await import(process.argv[1]);
const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const out = cases.map(c => {
  const doc = Text.of(c.doc.split("\n"));
  const a = ChangeSet.fromJSON(c.a), b = ChangeSet.fromJSON(c.b), next = ChangeSet.fromJSON(c.next);
  return {
    apply: a.apply(doc).toString(),
    composed: a.compose(next).toJSON(),
    mapped: a.map(b).toJSON(),
    mappedBefore: a.map(b, true).toJSON(),
    positions: c.positions.map(([p, assoc]) => a.mapPos(p, assoc)),
    inverted: a.invert(doc).toJSON(),
  };
});
process.stdout.write(JSON.stringify(out));
"""

ALPHABET = list("abc \n.") + ["é", "😀"]


def rand_text(rng, n):
    return "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, n)))


def bounds(text):
    out, pos = [0], 0
    for ch in text:
        pos += 2 if ord(ch) > 0xFFFF else 1
        out.append(pos)
    return out


def rand_changes(rng, doc):
    b = bounds(doc)
    k = rng.randint(0, 3)
    pts = sorted(rng.choice(b) for _ in range(2 * k))
    return ChangeSet.of([(pts[2 * i], pts[2 * i + 1], rand_text(rng, 5)) for i in range(k)], u16len(doc))


@pytest.mark.skipif(shutil.which("node") is None or not BUNDLE.exists(), reason="needs node and the vendored bundle")
def test_matches_codemirror():
    rng = random.Random(1234)
    cases, local = [], []
    for _ in range(400):
        doc = rand_text(rng, 30)
        a, b = rand_changes(rng, doc), rand_changes(rng, doc)
        nxt = rand_changes(rng, a.apply(doc))
        positions = [(p, rng.choice([-1, 1])) for p in bounds(doc)]
        cases.append({"doc": doc, "a": a.to_json(), "b": b.to_json(), "next": nxt.to_json(), "positions": positions})
        local.append({
            "apply": a.apply(doc),
            "composed": a.compose(nxt).to_json(),
            "mapped": a.map(b).to_json(),
            "mappedBefore": a.map(b, True).to_json(),
            "positions": [a.map_pos(p, s) for p, s in positions],
            "inverted": a.invert(doc).to_json(),
        })
    script = "(async () => {" + NODE_SCRIPT + "})()"
    res = subprocess.run(["node", "-e", script, BUNDLE.as_uri()], input=json.dumps(cases),
                         capture_output=True, text=True, check=True)
    remote = json.loads(res.stdout)
    for i, (mine, theirs) in enumerate(zip(local, remote)):
        assert mine == theirs, (i, cases[i])

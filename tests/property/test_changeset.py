"""Property tests for ChangeSets and the UTF-16 buffer (§20)."""

from hypothesis import given, settings
from hypothesis import strategies as st

from docwizard.collab.changeset import ChangeSet, from_diff, u16len, u16slice
from docwizard.collab.text import CHUNK_UNITS, Utf16Buffer

# Text with some astral characters, which count as two UTF-16 units.
alphabet = st.sampled_from(list("ab c\n.") + ["é", "😀", "𝔸", "中"])
texts = st.text(alphabet=alphabet, max_size=40)


def boundaries(text: str) -> list[int]:
    out, pos = [0], 0
    for ch in text:
        pos += 2 if ord(ch) > 0xFFFF else 1
        out.append(pos)
    return out


@st.composite
def doc_and_changes(draw, doc=None):
    doc = draw(texts) if doc is None else doc
    bounds = boundaries(doc)
    n = draw(st.integers(0, 4))
    points = sorted(draw(st.lists(st.sampled_from(bounds), min_size=2 * n, max_size=2 * n)))
    changes = []
    for k in range(n):
        frm, to = points[2 * k], points[2 * k + 1]
        changes.append((frm, to, draw(texts)))
    return doc, ChangeSet.of(changes, u16len(doc))


def py_apply(doc, changes):
    return changes.apply(doc)


@given(doc_and_changes())
def test_json_roundtrip(dc):
    doc, cs = dc
    again = ChangeSet.from_json(cs.to_json())
    assert again.apply(doc) == cs.apply(doc)
    assert again.length == u16len(doc)


@given(st.data())
def test_compose_equals_sequential_apply(data):
    doc, a = data.draw(doc_and_changes())
    mid = a.apply(doc)
    _, b = data.draw(doc_and_changes(mid))
    assert a.compose(b).apply(doc) == b.apply(mid)


@given(st.data())
def test_map_converges(data):
    doc, a = data.draw(doc_and_changes())
    _, b = data.draw(doc_and_changes(doc))
    left = b.map(a, True).apply(a.apply(doc))
    right = a.map(b).apply(b.apply(doc))
    assert left == right


@given(doc_and_changes())
def test_invert(dc):
    doc, cs = dc
    assert cs.invert(doc).apply(cs.apply(doc)) == doc


@given(texts, texts)
def test_from_diff(old, new):
    assert from_diff(old, new).apply(old) == new


@given(doc_and_changes())
def test_map_pos_monotonic_and_bounded(dc):
    doc, cs = dc
    prev = 0
    for pos in range(u16len(doc) + 1):
        mapped = cs.map_pos(pos, 1)
        assert prev <= mapped <= cs.new_length
        prev = mapped


@given(doc_and_changes())
def test_buffer_matches_string(dc):
    doc, cs = dc
    buf = Utf16Buffer(doc)
    buf.validate(cs)
    undo = buf.apply(cs)
    assert str(buf) == cs.apply(doc)
    assert len(buf) == u16len(cs.apply(doc))
    buf.revert(undo)
    assert str(buf) == doc


@settings(max_examples=30)
@given(st.lists(st.tuples(st.integers(0, 10**6), st.integers(0, 3000), st.text(alphabet=alphabet, max_size=2000)), max_size=12))
def test_buffer_chunked_edits(ops):
    base = "x😀y" * (CHUNK_UNITS // 2)
    buf, ref = Utf16Buffer(base), base
    for frm, length, text in ops:
        bounds = boundaries(ref)
        frm = bounds[frm % len(bounds)]
        to_candidates = [b for b in bounds if frm <= b <= frm + length]
        to = to_candidates[-1]
        buf.replace(frm, to, text)
        ref = u16slice(ref, 0, frm) + text + u16slice(ref, to)
        assert len(buf) == u16len(ref)
    assert str(buf) == ref


def test_surrogate_split_rejected():
    import pytest
    from docwizard.collab.changeset import InvalidChange

    buf = Utf16Buffer("a😀b")
    with pytest.raises(InvalidChange):
        buf.validate(ChangeSet.from_json([2, [0, "x"], 2]))
    with pytest.raises(InvalidChange):
        buf.validate(ChangeSet.from_json([1, [0, "\ud800"], 3]))
    with pytest.raises(InvalidChange):
        buf.validate(ChangeSet.from_json([5]))
    buf.validate(ChangeSet.from_json([3, [0, "x"], 1]))

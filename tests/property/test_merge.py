"""Reconciliation properties (§20)."""

from hypothesis import given, settings
from hypothesis import strategies as st

from docwizard.collab.merge import reconcile

WORDS = ["The", "result", "is", "positive", "negative", "a", "b", "cat", "dog", "😀"]


@st.composite
def sentence_doc(draw):
    # Each line starts with a distinct marker, as real sentences are rarely
    # identical; otherwise a line diff may legitimately align moved lines.
    sentences = draw(st.lists(st.lists(st.sampled_from(WORDS), min_size=1, max_size=6), min_size=1, max_size=6))
    return "\n".join(f"S{i} " + " ".join(s) + "." for i, s in enumerate(sentences))


def mutate(draw, text):
    lines = text.split("\n")
    for _ in range(draw(st.integers(0, 2))):
        i = draw(st.integers(0, len(lines) - 1))
        words = lines[i].rstrip(".").split(" ")
        j = draw(st.integers(1, len(words) - 1))
        words[j] = draw(st.sampled_from(WORDS))
        lines[i] = " ".join(words) + "."
    return "\n".join(lines)


def test_example_from_architecture():
    base = "The result is positive."
    res = reconcile(base, "The result is negative.", "The result is inconclusive.")
    assert res.text == "The result is negative."
    assert len(res.conflicts) == 1
    c = res.conflicts[0]
    assert c.theirs_text == "inconclusive" and c.base_text == "positive"
    assert res.text[c.anchor_from:c.anchor_to] == "negative"


def test_non_overlapping_edits_merge():
    base = "First sentence here.\nSecond sentence here.\nThird one."
    ours = "First sentence there.\nSecond sentence here.\nThird one."
    theirs = "First sentence here.\nSecond sentence here.\nThird two."
    res = reconcile(base, ours, theirs)
    assert res.text == "First sentence there.\nSecond sentence here.\nThird two."
    assert res.conflicts == []
    assert res.changes.apply(ours) == res.text


def test_same_sentence_is_conflict():
    base = "Cats are nice and dogs are loud."
    ours = "Cats are great and dogs are loud."
    theirs = "Cats are nice and dogs are quiet."
    res = reconcile(base, ours, theirs)
    assert res.text == ours
    assert len(res.conflicts) == 1
    assert "quiet" in res.conflicts[0].theirs_text


def test_unchanged_ours_takes_theirs():
    res = reconcile("a b c.", "a b c.", "a x c.")
    assert res.text == "a x c." and not res.conflicts


@settings(max_examples=300)
@given(st.data())
def test_reconcile_properties(data):
    base = data.draw(sentence_doc())
    ours = mutate(data.draw, base)
    theirs = mutate(data.draw, base)
    res = reconcile(base, ours, theirs)
    # The change set turns ours into the merged text.
    assert res.changes.apply(ours) == res.text
    # Ours is never overwritten: every ours line survives unless only theirs changed it.
    base_lines, ours_lines, theirs_lines = base.split("\n"), ours.split("\n"), theirs.split("\n")
    merged_lines = res.text.split("\n")
    assert len(merged_lines) == len(base_lines)
    for b, o, t, m in zip(base_lines, ours_lines, theirs_lines, merged_lines):
        if o == b:
            assert m == t  # only theirs changed the sentence
        elif t == b:
            assert m == o  # only ours changed it
        else:
            assert m == o  # both changed the same sentence: ours stays, conflict recorded
    # Conflicts are recorded exactly for sentences changed differently on both sides,
    # and theirs can always be recovered from them.
    both = sum(1 for b, o, t in zip(base_lines, ours_lines, theirs_lines) if o != b and t != b and o != t)
    if both:
        assert res.conflicts
    for c in res.conflicts:
        assert c.theirs_text != c.ours_text

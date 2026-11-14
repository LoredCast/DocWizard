"""Reconciling outside and offline edits (§12.3).

Inputs are ``base`` (the last common text), ``ours`` (the authority's
current text) and ``theirs`` (the outside version). Changes are found by
words, not characters. A change from theirs conflicts with one from ours when
their ranges overlap or touch, or when both fall in the same sentence.
Non-conflicting changes from theirs are applied to ours; for every conflict
ours stays in place and the alternative is recorded.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

from .changeset import ChangeSet, diff_edits, edits_to_changeset, to_u16_offset

_SENTENCE_END = re.compile(r"[.?!:](?=\s)|\n")


@dataclass
class Conflict:
    # Range of ours' version of the region in the merged text (UTF-16 units).
    anchor_from: int
    anchor_to: int
    base_text: str
    ours_text: str
    theirs_text: str


@dataclass
class MergeResult:
    changes: ChangeSet  # to apply to ours
    text: str  # ours with the changes applied
    conflicts: list[Conflict] = field(default_factory=list)


def _sentence_starts(text: str) -> list[int]:
    starts = [0]
    for m in _SENTENCE_END.finditer(text):
        starts.append(m.end())
    return starts


def _sentence_span(starts: list[int], frm: int, to: int) -> tuple[int, int]:
    first = bisect.bisect_right(starts, frm) - 1
    last = bisect.bisect_right(starts, max(frm, to - 1)) - 1
    return first, last


def _conflicts(o: tuple[int, int, str], t: tuple[int, int, str], starts: list[int]) -> bool:
    of, ot, _ = o
    tf, tt, _ = t
    if of <= tt and tf <= ot:  # overlap or touch
        return True
    a, b = _sentence_span(starts, of, ot), _sentence_span(starts, tf, tt)
    return a[0] <= b[1] and b[0] <= a[1]


def _apply_edits(text: str, edits: list[tuple[int, int, str]], start: int = 0) -> tuple[str, int]:
    out, pos = [], start
    for frm, to, ins in edits:
        out.append(text[pos:frm])
        out.append(ins)
        pos = to
    return "".join(out), pos


def reconcile(base: str, ours: str, theirs: str) -> MergeResult:
    ours_edits = diff_edits(base, ours)
    theirs_edits = diff_edits(base, theirs)
    if not theirs_edits:
        return MergeResult(ChangeSet.empty(len(ours.encode("utf-16-le")) // 2), ours)
    if not ours_edits:
        cs = edits_to_changeset(ours, diff_edits(ours, theirs))
        return MergeResult(cs, theirs)

    starts = _sentence_starts(base)

    # Group edits into clusters: connected components of the conflict relation.
    items = [("o", e) for e in ours_edits] + [("t", e) for e in theirs_edits]
    items.sort(key=lambda it: (it[1][0], it[1][1]))
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, (si, ei) in enumerate(items):
        for j in range(i + 1, len(items)):
            sj, ej = items[j]
            if ej[0] > ei[1] and _sentence_span(starts, ej[0], ej[1])[0] > _sentence_span(starts, ei[0], ei[1])[1]:
                break
            if si != sj and _conflicts(ei, ej, starts):
                parent[find(i)] = find(j)
    clusters: dict[int, list[int]] = {}
    for i in range(len(items)):
        clusters.setdefault(find(i), []).append(i)

    conflict_regions: list[tuple[int, int]] = []  # base ranges
    clean_theirs: list[tuple[int, int, str]] = []
    for members in clusters.values():
        sides = {items[i][0] for i in members}
        if sides == {"t"}:
            clean_theirs.extend(items[i][1] for i in members)
        elif sides == {"o", "t"}:
            lo = min(items[i][1][0] for i in members)
            hi = max(items[i][1][1] for i in members)
            conflict_regions.append((lo, hi))
    # Merge overlapping conflict regions; theirs edits inside a region belong to it.
    conflict_regions.sort()
    merged_regions: list[list[int]] = []
    for lo, hi in conflict_regions:
        if merged_regions and lo <= merged_regions[-1][1]:
            merged_regions[-1][1] = max(merged_regions[-1][1], hi)
        else:
            merged_regions.append([lo, hi])
    clean_theirs = [e for e in clean_theirs
                    if not any(r[0] <= e[0] and e[1] <= r[1] for r in merged_regions)]
    clean_theirs.sort()

    # Map clean theirs edits onto ours. They neither overlap nor touch any ours
    # edit, so the shift is the net length change of the ours edits before them.
    def shift_before(pos: int) -> int:
        delta = 0
        for frm, to, ins in ours_edits:
            if to <= pos and not (frm == to == pos):
                delta += len(ins) - (to - frm)
            elif frm >= pos:
                break
        return delta

    on_ours = [(e[0] + shift_before(e[0]), e[1] + shift_before(e[0]), e[2]) for e in clean_theirs]
    merged_text, pos = _apply_edits(ours, on_ours)
    merged_text += ours[pos:]
    changes = edits_to_changeset(ours, on_ours)

    def ours_pos(base_pos: int, end: bool) -> int:
        """Map a base position into ours. At the end of a region, ours'
        insertions at that position belong to the region; at the start they don't."""
        delta = 0
        for frm, to, ins in ours_edits:
            if to > base_pos:
                break
            if not end and frm == to == base_pos:
                continue
            delta += len(ins) - (to - frm)
        return base_pos + delta

    conflicts = []
    for lo, hi in merged_regions:
        o_from, o_to = ours_pos(lo, False), ours_pos(hi, True)
        ours_region = ours[o_from:o_to]
        t_inside = [e for e in theirs_edits if lo <= e[0] and e[1] <= hi]
        theirs_region, rest = _apply_edits(base, t_inside, lo)
        theirs_region += base[rest:hi]
        if ours_region == theirs_region:
            continue  # both sides made the same change
        # Position in the merged text: clean edits before the region shift it.
        shift = 0
        for frm, to, ins in on_ours:
            if to <= o_from:
                shift += len(ins) - (to - frm)
        m_from, m_to = o_from + shift, o_to + shift
        conflicts.append(Conflict(
            anchor_from=to_u16_offset(merged_text, m_from),
            anchor_to=to_u16_offset(merged_text, m_to),
            base_text=base[lo:hi], ours_text=ours_region, theirs_text=theirs_region,
        ))
    return MergeResult(changes, merged_text, conflicts)

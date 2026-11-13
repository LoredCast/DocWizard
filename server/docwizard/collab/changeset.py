"""CodeMirror ChangeSets in Python (§8.4).

Works directly on the JSON format of ``ChangeSet.toJSON()``: a number is a
run of unchanged text, ``[deleted]`` a deletion and ``[deleted, line, …]`` a
replacement whose inserted lines are joined with ``\\n``. All lengths and
positions are UTF-16 code units, like in the browser.

Internally a ChangeSet is CodeMirror's ``sections`` array (pairs of
``length, insertedLength`` where ``-1`` marks unchanged text) plus the inserted
strings, and ``compose``/``map`` follow CodeMirror's algorithms so results
match the browser exactly.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterator
from dataclasses import dataclass


class InvalidChange(ValueError):
    pass


# -- UTF-16 helpers ------------------------------------------------------

def u16len(text: str) -> int:
    if text.isascii():
        return len(text)
    return len(text) + sum(1 for ch in text if ord(ch) > 0xFFFF)


def u16slice(text: str, start: int, end: int | None = None) -> str:
    if text.isascii():
        return text[start:end]
    data = text.encode("utf-16-le", "surrogatepass")
    end_b = None if end is None else end * 2
    return data[start * 2:end_b].decode("utf-16-le", "surrogatepass")


def to_u16_offset(text: str, index: int) -> int:
    """Code point index -> UTF-16 offset."""
    return u16len(text[:index])


def has_lone_surrogate(text: str) -> bool:
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in text)


# -- ChangeSet -----------------------------------------------------------

@dataclass(frozen=True)
class ChangeSet:
    sections: tuple[int, ...]
    inserted: tuple[str, ...]

    # construction -----------------------------------------------------
    @staticmethod
    def from_json(data) -> ChangeSet:
        if not isinstance(data, list):
            raise InvalidChange("changes must be a list")
        sections: list[int] = []
        inserted: list[str] = []
        for i, part in enumerate(data):
            if isinstance(part, bool):
                raise InvalidChange("invalid change part")
            if isinstance(part, int):
                if part < 0:
                    raise InvalidChange("negative length")
                sections += [part, -1]
            elif (isinstance(part, list) and part and isinstance(part[0], int) and not isinstance(part[0], bool)
                  and part[0] >= 0 and all(isinstance(e, str) for e in part[1:])):
                if len(part) == 1:
                    sections += [part[0], 0]
                else:
                    text = "\n".join(part[1:])
                    while len(inserted) < i:
                        inserted.append("")
                    inserted.append(text)
                    sections += [part[0], u16len(text)]
            else:
                raise InvalidChange("invalid change part")
        return ChangeSet(tuple(sections), tuple(inserted))

    def to_json(self) -> list:
        parts: list = []
        for i in range(0, len(self.sections), 2):
            length, ins = self.sections[i], self.sections[i + 1]
            if ins < 0:
                parts.append(length)
            elif ins == 0:
                parts.append([length])
            else:
                parts.append([length, *self.inserted[i >> 1].split("\n")])
        return parts

    @staticmethod
    def of(changes: list[tuple[int, int, str]], length: int) -> ChangeSet:
        """Build from sorted, non-overlapping ``(from, to, insert)`` in UTF-16 units."""
        sections: list[int] = []
        inserted: list[str] = []
        pos = 0
        for frm, to, text in changes:
            if frm < pos or to < frm or to > length:
                raise InvalidChange("changes must be sorted and inside the document")
            if frm > pos:
                _add_section(sections, frm - pos, -1)
            ins_len = u16len(text)
            if to > frm or ins_len:
                _add_section(sections, to - frm, ins_len)
                _add_insert(inserted, sections, text)
            pos = to
        if pos < length:
            _add_section(sections, length - pos, -1)
        return ChangeSet(tuple(sections), tuple(inserted))

    @staticmethod
    def empty(length: int) -> ChangeSet:
        return ChangeSet((length, -1) if length else (), ())

    # properties -------------------------------------------------------
    @property
    def length(self) -> int:
        return sum(self.sections[i] for i in range(0, len(self.sections), 2))

    @property
    def new_length(self) -> int:
        total = 0
        for i in range(0, len(self.sections), 2):
            ins = self.sections[i + 1]
            total += self.sections[i] if ins < 0 else ins
        return total

    @property
    def empty_changes(self) -> bool:
        return all(self.sections[i + 1] == -1 for i in range(0, len(self.sections), 2))

    def churn(self) -> int:
        """Inserted plus deleted code units."""
        total = 0
        for i in range(0, len(self.sections), 2):
            if self.sections[i + 1] >= 0:
                total += self.sections[i] + self.sections[i + 1]
        return total

    def iter_changes(self) -> Iterator[tuple[int, int, int, int, str]]:
        """Yield ``(fromA, toA, fromB, toB, inserted)`` for every change."""
        pos_a = pos_b = 0
        i = 0
        secs = self.sections
        while i < len(secs):
            length, ins = secs[i], secs[i + 1]
            if ins < 0:
                pos_a += length
                pos_b += length
                i += 2
                continue
            end_a, end_b, text = pos_a, pos_b, ""
            # Join adjacent change sections into one change.
            while True:
                end_a += length
                end_b += ins
                if ins > 0:
                    text += self.inserted[i >> 1] if (i >> 1) < len(self.inserted) else ""
                i += 2
                if i >= len(secs) or secs[i + 1] < 0:
                    break
                length, ins = secs[i], secs[i + 1]
            yield pos_a, end_a, pos_b, end_b, text
            pos_a, pos_b = end_a, end_b

    # operations -------------------------------------------------------
    def apply(self, text: str) -> str:
        """Apply to a Python string (offsets converted from UTF-16)."""
        if u16len(text) != self.length:
            raise InvalidChange("change length doesn't match the document")
        if text.isascii() and all(t.isascii() for t in self.inserted):
            out, pos = [], 0
            for frm, to, _, _, ins in self.iter_changes():
                out.append(text[pos:frm])
                out.append(ins)
                pos = to
            out.append(text[pos:])
            return "".join(out)
        data = text.encode("utf-16-le", "surrogatepass")
        out_b, pos = [], 0
        for frm, to, _, _, ins in self.iter_changes():
            out_b.append(data[pos * 2:frm * 2])
            out_b.append(ins.encode("utf-16-le", "surrogatepass"))
            pos = to
        out_b.append(data[pos * 2:])
        return b"".join(out_b).decode("utf-16-le", "surrogatepass")

    def map_pos(self, pos: int, assoc: int = -1) -> int:
        pos_a = pos_b = 0
        secs = self.sections
        for i in range(0, len(secs), 2):
            length, ins = secs[i], secs[i + 1]
            end_a = pos_a + length
            if ins < 0:
                if end_a > pos:
                    return pos_b + (pos - pos_a)
                pos_b += length
            else:
                if end_a > pos or (end_a == pos and assoc < 0 and not length):
                    return pos_b if (pos == pos_a or assoc < 0) else pos_b + ins
                pos_b += ins
            pos_a = end_a
        if pos > pos_a:
            raise InvalidChange(f"position {pos} is outside the document ({pos_a})")
        return pos_b

    def invert(self, doc: str) -> ChangeSet:
        sections: list[int] = []
        inserted: list[str] = []
        pos = 0
        secs = self.sections
        for i in range(0, len(secs), 2):
            length, ins = secs[i], secs[i + 1]
            if ins >= 0:
                sections += [ins, length]
                while len(inserted) < len(sections) // 2 - 1:
                    inserted.append("")
                inserted.append(u16slice(doc, pos, pos + length) if length else "")
            else:
                sections += [length, ins]
            pos += length
        return ChangeSet(tuple(sections), tuple(inserted))

    def compose(self, other: ChangeSet) -> ChangeSet:
        """Apply ``self`` then ``other``."""
        if self.new_length != other.length:
            raise InvalidChange("mismatched change set lengths in compose")
        return _compose_sets(self, other)

    def map(self, other: ChangeSet, before: bool = False) -> ChangeSet:
        """Transform ``self`` to apply after ``other`` (both based on the same document)."""
        if self.length != other.length:
            raise InvalidChange("mismatched change set lengths in map")
        return _map_set(self, other, before)


# -- CodeMirror's section algorithms ----------------------------------------

def _add_section(sections: list[int], length: int, ins: int, force_join: bool = False) -> None:
    if length == 0 and ins <= 0:
        return
    last = len(sections) - 2
    if last >= 0 and ins <= 0 and ins == sections[last + 1]:
        sections[last] += length
    elif last >= 0 and length == 0 and sections[last] == 0:
        sections[last + 1] += ins
    elif force_join:
        sections[last] += length
        sections[last + 1] += ins
    else:
        sections += [length, ins]


def _add_insert(values: list[str], sections: list[int], value: str) -> None:
    if not value:
        return
    index = (len(sections) - 2) >> 1
    if index < len(values):
        values[-1] = values[-1] + value
    else:
        while len(values) < index:
            values.append("")
        values.append(value)


class _SectionIter:
    __slots__ = ("set", "i", "len", "ins", "off")

    def __init__(self, cs: ChangeSet):
        self.set = cs
        self.i = 0
        self.len = 0
        self.ins = 0
        self.off = 0
        self.next()

    def next(self) -> None:
        secs = self.set.sections
        if self.i < len(secs):
            self.len = secs[self.i]
            self.ins = secs[self.i + 1]
            self.i += 2
        else:
            self.len = 0
            self.ins = -2
        self.off = 0

    @property
    def done(self) -> bool:
        return self.ins == -2

    @property
    def len2(self) -> int:
        return self.len if self.ins < 0 else self.ins

    @property
    def text(self) -> str:
        index = (self.i - 2) >> 1
        return self.set.inserted[index] if index < len(self.set.inserted) else ""

    def text_bit(self, length: int | None = None) -> str:
        index = (self.i - 2) >> 1
        if index >= len(self.set.inserted) and not length:
            return ""
        text = self.set.inserted[index] if index < len(self.set.inserted) else ""
        return u16slice(text, self.off, None if length is None else self.off + length)

    def forward(self, length: int) -> None:
        if length == self.len:
            self.next()
        else:
            self.len -= length
            self.off += length

    def forward2(self, length: int) -> None:
        if self.ins == -1:
            self.forward(length)
        elif length == self.ins:
            self.next()
        else:
            self.ins -= length
            self.off += length


def _map_set(set_a: ChangeSet, set_b: ChangeSet, before: bool) -> ChangeSet:
    sections: list[int] = []
    insert: list[str] = []
    a, b = _SectionIter(set_a), _SectionIter(set_b)
    inserted = -1
    while True:
        if (a.done and b.len) or (b.done and a.len):
            raise InvalidChange("mismatched change set lengths")
        elif a.ins == -1 and b.ins == -1:
            length = min(a.len, b.len)
            _add_section(sections, length, -1)
            a.forward(length)
            b.forward(length)
        elif b.ins >= 0 and (a.ins < 0 or inserted == a.i or
                             (a.off == 0 and (b.len < a.len or (b.len == a.len and not before)))):
            length = b.len
            _add_section(sections, b.ins, -1)
            while length:
                piece = min(a.len, length)
                if a.ins >= 0 and inserted < a.i and a.len <= piece:
                    _add_section(sections, 0, a.ins)
                    _add_insert(insert, sections, a.text)
                    inserted = a.i
                a.forward(piece)
                length -= piece
            b.next()
        elif a.ins >= 0:
            length, left = 0, a.len
            while left:
                if b.ins == -1:
                    piece = min(left, b.len)
                    length += piece
                    left -= piece
                    b.forward(piece)
                elif b.ins == 0 and b.len < left:
                    left -= b.len
                    b.next()
                else:
                    break
            _add_section(sections, length, a.ins if inserted < a.i else 0)
            if inserted < a.i:
                _add_insert(insert, sections, a.text)
            inserted = a.i
            a.forward(a.len - left)
        elif a.done and b.done:
            return ChangeSet(tuple(sections), tuple(insert))
        else:
            raise InvalidChange("mismatched change set lengths")


def _compose_sets(set_a: ChangeSet, set_b: ChangeSet) -> ChangeSet:
    sections: list[int] = []
    insert: list[str] = []
    a, b = _SectionIter(set_a), _SectionIter(set_b)
    open_ = False
    while True:
        if a.done and b.done:
            return ChangeSet(tuple(sections), tuple(insert))
        elif a.ins == 0:  # deletion in A
            _add_section(sections, a.len, 0, open_)
            a.next()
        elif b.len == 0 and not b.done:  # insertion in B
            _add_section(sections, 0, b.ins, open_)
            _add_insert(insert, sections, b.text)
            b.next()
        elif a.done or b.done:
            raise InvalidChange("mismatched change set lengths")
        else:
            length = min(a.len2, b.len)
            section_len = len(sections)
            if a.ins == -1:
                ins_b = -1 if b.ins == -1 else (0 if b.off else b.ins)
                _add_section(sections, length, ins_b, open_)
                if ins_b > 0:
                    _add_insert(insert, sections, b.text)
            elif b.ins == -1:
                _add_section(sections, 0 if a.off else a.len, length, open_)
                _add_insert(insert, sections, a.text_bit(length))
            else:
                _add_section(sections, 0 if a.off else a.len, 0 if b.off else b.ins, open_)
                if not b.off:
                    _add_insert(insert, sections, b.text)
            open_ = (a.ins > length or (b.ins >= 0 and b.len > length)) and (open_ or len(sections) > section_len)
            a.forward2(length)
            b.forward(length)


# -- diffs ------------------------------------------------------------------

_TOKEN = re.compile(r"\w+|\s+|[^\w\s]", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """Words, runs of whitespace and single punctuation marks."""
    return _TOKEN.findall(text)


def diff_edits(old: str, new: str) -> list[tuple[int, int, str]]:
    """Edits turning *old* into *new*, as ``(start, end, replacement)`` in code points.

    Diffs by line first, then by word inside changed line blocks (§8.4, §12.3).
    """
    if old == new:
        return []
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    old_starts = [0]
    for line in old_lines:
        old_starts.append(old_starts[-1] + len(line))
    new_starts = [0]
    for line in new_lines:
        new_starts.append(new_starts[-1] + len(line))
    edits: list[tuple[int, int, str]] = []
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        a_start, a_end = old_starts[i1], old_starts[i2]
        b_start, b_end = new_starts[j1], new_starts[j2]
        if tag != "replace":
            edits.append((a_start, a_end, new[b_start:b_end]))
            continue
        if i2 - i1 == j2 - j1:
            # Same number of lines: diff line by line, so word edits never cross lines.
            for k in range(i2 - i1):
                _word_edits(edits, old, old_starts[i1 + k], old_starts[i1 + k + 1],
                            new[new_starts[j1 + k]:new_starts[j1 + k + 1]])
        else:
            _word_edits(edits, old, a_start, a_end, new[b_start:b_end])
    return edits


def _word_edits(edits: list, old: str, a_start: int, a_end: int, b_text: str) -> None:
    a_text = old[a_start:a_end]
    if a_text == b_text:
        return
    a_tok, b_tok = tokenize(a_text), tokenize(b_text)
    a_pos = [0]
    for t in a_tok:
        a_pos.append(a_pos[-1] + len(t))
    b_pos = [0]
    for t in b_tok:
        b_pos.append(b_pos[-1] + len(t))
    inner = difflib.SequenceMatcher(None, a_tok, b_tok, autojunk=False)
    for tag, k1, k2, l1, l2 in inner.get_opcodes():
        if tag != "equal":
            edits.append((a_start + a_pos[k1], a_start + a_pos[k2], b_text[b_pos[l1]:b_pos[l2]]))


def edits_to_changeset(old: str, edits: list[tuple[int, int, str]]) -> ChangeSet:
    """Convert code-point edits on *old* to a UTF-16 ChangeSet."""
    if old.isascii():
        return ChangeSet.of(list(edits), len(old))
    conv = []
    for start, end, text in edits:
        conv.append((to_u16_offset(old, start), to_u16_offset(old, end), text))
    return ChangeSet.of(conv, u16len(old))


def from_diff(old: str, new: str) -> ChangeSet:
    return edits_to_changeset(old, diff_edits(old, new))

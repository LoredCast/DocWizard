"""A chunked UTF-16-LE text buffer (§8.4).

The authority keeps each live document in this buffer so that offsets are
UTF-16 code units, like CodeMirror's, and applying a change costs roughly the
size of the change rather than the size of the document.
"""

from __future__ import annotations

import bisect

from .changeset import ChangeSet, InvalidChange, has_lone_surrogate

CHUNK_UNITS = 32 * 1024  # 64 KiB per chunk


def _is_high(unit: int) -> bool:
    return 0xD800 <= unit <= 0xDBFF


def _is_low(unit: int) -> bool:
    return 0xDC00 <= unit <= 0xDFFF


class Utf16Buffer:
    def __init__(self, text: str = ""):
        data = text.encode("utf-16-le", "surrogatepass")
        self._chunks: list[bytearray] = [bytearray(data[i:i + CHUNK_UNITS * 2])
                                         for i in range(0, len(data), CHUNK_UNITS * 2)] or [bytearray()]
        self._reindex()

    def _reindex(self) -> None:
        starts, pos = [], 0
        for chunk in self._chunks:
            starts.append(pos)
            pos += len(chunk) // 2
        self._starts = starts
        self._length = pos

    def __len__(self) -> int:
        return self._length

    def _locate(self, pos: int) -> tuple[int, int]:
        """Chunk index and unit offset for *pos* (prefers the earlier chunk at boundaries)."""
        if pos < 0 or pos > self._length:
            raise InvalidChange(f"position {pos} outside the document")
        idx = bisect.bisect_right(self._starts, pos) - 1
        if idx > 0 and pos == self._starts[idx]:
            idx -= 1
        return idx, pos - self._starts[idx]

    def unit_at(self, pos: int) -> int:
        idx = bisect.bisect_right(self._starts, pos) - 1
        off = (pos - self._starts[idx]) * 2
        chunk = self._chunks[idx]
        return chunk[off] | (chunk[off + 1] << 8)

    def is_boundary(self, pos: int) -> bool:
        """False if *pos* would split a surrogate pair."""
        if pos <= 0 or pos >= self._length:
            return True
        return not (_is_high(self.unit_at(pos - 1)) and _is_low(self.unit_at(pos)))

    def slice_bytes(self, start: int, end: int) -> bytes:
        if start >= end:
            return b""
        i, off_i = self._locate(start)
        j, off_j = self._locate(end)
        if i == j:
            return bytes(self._chunks[i][off_i * 2:off_j * 2])
        parts = [self._chunks[i][off_i * 2:]]
        parts.extend(self._chunks[i + 1:j])
        parts.append(self._chunks[j][:off_j * 2])
        return b"".join(bytes(p) for p in parts)

    def slice(self, start: int, end: int) -> str:
        return self.slice_bytes(start, end).decode("utf-16-le", "surrogatepass")

    def __str__(self) -> str:
        return b"".join(bytes(c) for c in self._chunks).decode("utf-16-le", "surrogatepass")

    def replace(self, start: int, end: int, text: str) -> str:
        """Replace ``[start, end)`` with *text*; returns the removed text."""
        if not (0 <= start <= end <= self._length):
            raise InvalidChange("range outside the document")
        removed = self.slice(start, end)
        ins = text.encode("utf-16-le", "surrogatepass")
        i, off_i = self._locate(start)
        j, off_j = self._locate(end)
        merged = self._chunks[i][:off_i * 2] + ins + self._chunks[j][off_j * 2:]
        size = CHUNK_UNITS * 2
        if len(merged) <= size:
            new_chunks = [merged]
        else:
            new_chunks = [bytearray(merged[k:k + size]) for k in range(0, len(merged), size)]
        self._chunks[i:j + 1] = new_chunks
        if len(self._chunks) > 1:
            self._chunks = [c for c in self._chunks if c] or [bytearray()]
        self._reindex()
        return removed

    # -- ChangeSets ------------------------------------------------------
    def validate(self, cs: ChangeSet) -> None:
        if cs.length != self._length:
            raise InvalidChange("the change doesn't match the document length")
        for frm, to, _, _, text in cs.iter_changes():
            if not self.is_boundary(frm) or not self.is_boundary(to):
                raise InvalidChange("the change splits a surrogate pair")
            if text and (has_lone_surrogate(text) or "\r" in text or "\x00" in text):
                raise InvalidChange("the inserted text is not valid")

    def apply(self, cs: ChangeSet) -> list[tuple[int, int, str]]:
        """Apply a validated ChangeSet. Returns an undo record for :meth:`revert`."""
        changes = list(cs.iter_changes())
        undo = []
        # Apply from the end so earlier positions stay valid.
        for frm, to, _, _, text in reversed(changes):
            removed = self.replace(frm, to, text)
            undo.append((frm, frm + len(text.encode("utf-16-le", "surrogatepass")) // 2, removed))
        return undo

    def revert(self, undo: list[tuple[int, int, str]]) -> None:
        for frm, to, removed in reversed(undo):
            self.replace(frm, to, removed)

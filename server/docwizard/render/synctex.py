"""From a point in the PDF preview back to the source (§6.4).

LaTeX builds write SyncTeX (`-synctex=1`). When a render finishes, :func:`index` turns the job's
`document.synctex.gz` into `synctex.json` next to the PDF: per page, the boxes and positions TeX
recorded, in PDF points from the page's top left, with project-relative file names. :func:`lookup`
answers "which file and line put this on page 3 at (x, y)".

Everything else (Typst PDFs, and LaTeX made from Markdown, whose SyncTeX points into pandoc's
intermediate file) is matched by the words around the click: :func:`match_words`.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import unicodedata
from pathlib import Path

MAX_SYNCTEX_BYTES = 256 * 1024 * 1024  # decompressed; a thousand-page thesis is a few dozen MB
SP_PER_BP = 65781.76  # TeX scaled points per PDF point (big point)

_RECORD = re.compile(r"^([\[(vhxkg$])(\d+),(\d+)(?:,-?\d+)?:(-?\d+|=),(-?\d+|=)(?::(-?\d+|=)(?:,(-?\d+|=),(-?\d+|=))?)?")
_JOB_SRC = re.compile(r"/run/[0-9A-Za-z_-]+/src/")


def _read(path: Path) -> str | None:
    """The SyncTeX file's text, read without following symlinks and within the size limit."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    with os.fdopen(fd, "rb") as raw:
        stream = gzip.GzipFile(fileobj=raw) if path.name.endswith(".gz") else raw
        try:
            data = stream.read(MAX_SYNCTEX_BYTES + 1)
        except (OSError, EOFError):
            return None
    if len(data) > MAX_SYNCTEX_BYTES:
        return None
    return data.decode("utf-8", errors="replace")


def project_path(name: str) -> str | None:
    """The project file an Input line names, or None for TeX Live's files and pandoc's intermediate."""
    m = _JOB_SRC.search(name)
    if m:
        rel = name[m.end():]
    elif name.startswith("/"):
        return None
    else:
        rel = name
    rel = os.path.normpath(rel).replace(os.sep, "/")
    if rel.startswith("../") or rel == ".." or rel.startswith("/"):
        return None
    if os.path.basename(rel).startswith(("_docwizard", ".docwizard")):
        return None
    return rel


def parse(text: str) -> dict:
    """SyncTeX text → {"inputs": {tag: path|None}, "pages": {page: [[kind, tag, line, x, y, w, h, d], ...]}}."""
    inputs: dict[str, str | None] = {}
    pages: dict[str, list] = {}
    unit, mag, x_off, y_off = 1.0, 1000.0, 0.0, 0.0
    page: list | None = None
    prev = [0, 0, 0, 0, 0]
    content = False
    for line in text.splitlines():
        if not line:
            continue
        if line.startswith("Input:"):
            _, tag, name = line.split(":", 2)
            inputs[tag] = project_path(name)
            continue
        if not content:
            key, _, value = line.partition(":")
            try:
                if key == "Unit":
                    unit = float(value)
                elif key == "Magnification":
                    mag = float(value)
                elif key == "X Offset":
                    x_off = float(value)
                elif key == "Y Offset":
                    y_off = float(value)
                elif key == "Content":
                    content = True
            except ValueError:
                pass
            continue
        head = line[0]
        if head == "{":
            page = pages.setdefault(line[1:].strip(), [])
            continue
        if head == "}":
            page = None
            continue
        if page is None:
            continue
        m = _RECORD.match(line)
        if not m:
            continue
        kind, tag, src_line = m.group(1), m.group(2), int(m.group(3))
        nums = []
        for i, g in enumerate(m.group(4, 5, 6, 7, 8)):
            if g is None:
                nums.append(0)
            elif g == "=":
                nums.append(prev[i])
            else:
                nums.append(int(g))
        prev = nums
        scale = unit * mag / 1000.0 / SP_PER_BP
        x = nums[0] * scale + x_off / SP_PER_BP
        y = nums[1] * scale + y_off / SP_PER_BP
        w, h, d = (n * scale for n in nums[2:])
        page.append([kind, tag, src_line, round(x, 2), round(y, 2), round(w, 2), round(h, 2), round(d, 2)])
    return {"inputs": inputs, "pages": pages}


def index(src: Path, dest: Path) -> bool:
    """Write `synctex.json` for the SyncTeX file *src*. Returns whether there is one."""
    text = _read(src)
    if text is None:
        return False
    data = parse(text)
    if not data["pages"]:
        return False
    dest.write_text(json.dumps(data, separators=(",", ":")))
    return True


def lookup(data: dict, page: int, x: float, y: float) -> tuple[str, int] | None:
    """The project file and line that put (x, y) on *page* (PDF points from the top left)."""
    records = data.get("pages", {}).get(str(page)) or []
    inputs = data.get("inputs", {})
    ours = [r for r in records if inputs.get(r[1]) and r[2] > 0]
    if not ours:
        return None
    # The innermost line box under the point: an hbox spans x..x+w, and y-h..y+d around its baseline.
    boxes = [r for r in ours if r[0] in "(h" and r[5] > 0
             and r[3] - 1 <= x <= r[3] + r[5] + 1 and r[4] - r[6] - 2 <= y <= r[4] + r[7] + 2]
    if boxes:
        box = min(boxes, key=lambda r: r[5] * (r[6] + r[7] + 1))
        # Within the line, the last glue, kern or math TeX passed before the point names the line best.
        marks = [r for r in ours if r[0] in "xkg$" and abs(r[4] - box[4]) < 0.6
                 and box[3] - 1 <= r[3] <= box[3] + box[5] + 1]
        before = [r for r in marks if r[3] <= x]
        pick = max(before, key=lambda r: r[3]) if before else min(marks, key=lambda r: r[3]) if marks else box
    else:
        # Between lines: the nearest recorded position, rows counting more than columns.
        pick = min(ours, key=lambda r: (abs(r[4] - y) * 3 + abs(r[3] - x)))
    return inputs[pick[1]], pick[2]


def forward(data: dict, path: str, line: int, limit: int = 60) -> list[dict]:
    """Where *path* line *line* is set (§6.4): boxes {page, x, y, w, h} in PDF points from the page's
    top left. A line TeX recorded nothing for (a blank line, a comment) uses the next one that has
    records, a few lines on, or else the one before."""
    inputs = data.get("inputs", {})
    tags = {tag for tag, p in inputs.items() if p == path}
    if not tags:
        return []
    by_line: dict[int, list[tuple[int, list]]] = {}
    for page, records in data.get("pages", {}).items():
        try:
            n = int(page)
        except ValueError:
            continue
        for r in records:
            if r[1] in tags and r[2] > 0:
                by_line.setdefault(r[2], []).append((n, r))
    if not by_line:
        return []
    after = [ln for ln in by_line if line <= ln <= line + 20]
    before = [ln for ln in by_line if ln < line]
    pick = min(after) if after else max(before) if before else None
    if pick is None:
        return []
    found = by_line[pick]
    boxes = [(n, r[3], r[4] - r[6], r[5], r[6] + r[7]) for n, r in found if r[0] in "(h" and r[5] > 0]
    if not boxes:
        # Only points (glue, kerns, math): the line boxes around them, whichever file they belong to.
        for n, r in found:
            around = [b for b in data["pages"].get(str(n), []) if b[0] in "(h" and b[5] > 0
                      and b[3] - 1 <= r[3] <= b[3] + b[5] + 1 and b[4] - b[6] - 2 <= r[4] <= b[4] + b[7] + 2]
            if around:
                b = min(around, key=lambda b: b[5] * (b[6] + b[7] + 1))
                boxes.append((n, b[3], b[4] - b[6], b[5], b[6] + b[7]))
            else:
                boxes.append((n, r[3], r[4] - 10, 4, 12))
    out, seen = [], set()
    for n, x, y, w, h in sorted(boxes, key=lambda b: (b[0], b[2], b[1])):
        key = (n, round(x), round(y), round(w), round(h))
        if key in seen or h <= 0:
            continue
        seen.add(key)
        # Nested boxes (a line inside a paragraph's box) mark the innermost ones.
        if any(o["page"] == n and o["x"] <= x + 0.5 and x + w <= o["x"] + o["w"] + 0.5 and o["y"] <= y + 0.5
               and y + h <= o["y"] + o["h"] + 0.5 and o["h"] > h * 1.5 for o in out):
            continue
        out = [o for o in out if not (o["page"] == n and x <= o["x"] + 0.5 and o["x"] + o["w"] <= x + w + 0.5
                                      and y <= o["y"] + 0.5 and o["y"] + o["h"] <= y + h + 0.5 and h > o["h"] * 1.5)]
        out.append({"page": n, "x": round(x, 2), "y": round(y, 2), "w": round(w, 2), "h": round(h, 2)})
        if len(out) >= limit:
            break
    return out


# -- words ---------------------------------------------------------------------

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def words_of(text: str) -> list[tuple[str, int]]:
    """Lower-cased words of *text* with their offsets (NFKC, so PDF ligatures match plain letters)."""
    return [(_norm(m.group()), m.start()) for m in _WORD.finditer(text)]


def _norm(word: str) -> str:
    return unicodedata.normalize("NFKC", word).lower()


def match_words(texts: list[tuple[str, str]], query: list[str], at: int, hint: float = 0.5) -> tuple[str, int] | None:
    """Find the passage the words around a click came from.

    *texts* are (path, text) in build order, *query* the words around the click in reading order,
    *at* the index of the clicked word, and *hint* where in the build the page lies (0 to 1), to
    choose between passages that match equally well. Returns (path, offset of the clicked word).
    Words may be missing or added on either side (citations, footnote marks, hyphenation), so each
    side is followed with a small lookahead rather than compared word for word.
    """
    q = [_norm(w) for w in query if w and _WORD.fullmatch(_norm(w))]
    if not q:
        return None
    at = max(0, min(at, len(q) - 1))
    tokens: list[tuple[str, int, int]] = []  # word, text index, offset
    for ti, (_, text) in enumerate(texts):
        tokens += [(w, ti, off) for w, off in words_of(text)]
    if not tokens:
        return None
    where: dict[str, list[int]] = {}
    for i, (w, _, _) in enumerate(tokens):
        where.setdefault(w, []).append(i)

    def extend(i: int, j: int) -> tuple[int, int | None]:
        """Matches around token i taken as query word j, and the token the clicked word fell on."""
        score, hit = 1, i if j == at else None
        for step in (-1, 1):
            pos, qj, misses = i, j + step, 0
            while 0 <= qj < len(q) and misses < 4:
                found = None
                for look in range(1, 4):
                    p = pos + step * look
                    if 0 <= p < len(tokens) and tokens[p][0] == q[qj]:
                        found = p
                        break
                if found is None:
                    misses += 1
                else:
                    score += 1
                    pos = found
                    if qj == at:
                        hit = found
                qj += step
        return score, hit

    best = None
    for j in sorted(range(len(q)), key=lambda j: abs(j - at)):
        if len(q[j]) < 3 and len(q) > 1:
            continue
        spots = where.get(q[j], [])
        if not spots or len(spots) > 400:
            continue
        for i in spots:
            score, hit = extend(i, j)
            if hit is None:
                hit = i
            rank = (score, -abs(j - at), -abs(hit / max(1, len(tokens) - 1) - hint))
            if best is None or rank > best[0]:
                best = (rank, hit)
        if best and best[0][0] >= min(len(q), 6):
            break  # a confident match around the clicked word itself
    if best is None or (best[0][0] < 2 and len(q) > 2):
        return None
    _, ti, off = tokens[best[1]]
    return texts[ti][0], off


def line_col(text: str, offset: int) -> tuple[int, int]:
    """1-based line and 0-based column of *offset*."""
    line = text.count("\n", 0, offset) + 1
    col = offset - (text.rfind("\n", 0, offset) + 1)
    return line, col

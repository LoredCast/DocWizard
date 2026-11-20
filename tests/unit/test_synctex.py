"""Clicks in the PDF preview find their source (render/synctex.py)."""

import gzip

from docwizard.render import synctex

# Two lines of a paragraph from main.tex and one from chapters/two.tex, as LuaLaTeX writes them:
# coordinates in scaled points from the page's top left (65781.76 sp to a PDF point).
SP = 65781.76
SAMPLE = f"""SyncTeX Version:1
Input:1:/srv/jobs/run/0a1b2c3d4e5f60718293a4b5c6d7e8f9/src/./main.tex
Input:2:/usr/share/texlive/texmf-dist/tex/latex/base/article.cls
Input:3:/srv/jobs/run/0a1b2c3d4e5f60718293a4b5c6d7e8f9/src/chapters/two.tex
Input:4:/srv/jobs/run/0a1b2c3d4e5f60718293a4b5c6d7e8f9/src/_docwizard.tex
Output:pdf
Magnification:1000
Unit:1
X Offset:0
Y Offset:0
Content:
!300
{{1
[1,20:{int(72 * SP)},{int(700 * SP)}:{int(468 * SP)},{int(628 * SP)},0
(1,12:{int(72 * SP)},{int(100 * SP)}:{int(468 * SP)},{int(8 * SP)},{int(2 * SP)}
g1,10:{int(150 * SP)},{int(100 * SP)}
k1,11:{int(300 * SP)},{int(100 * SP)}:{int(1 * SP)}
)
(1,12:{int(72 * SP)},{int(114 * SP)}:{int(468 * SP)},{int(8 * SP)},{int(2 * SP)}
g1,12:{int(200 * SP)},{int(114 * SP)}
)
h2,5:{int(72 * SP)},{int(40 * SP)}:{int(468 * SP)},{int(10 * SP)},0
(3,7:{int(72 * SP)},{int(300 * SP)}:{int(468 * SP)},{int(8 * SP)},{int(2 * SP)}
x3,7:{int(80 * SP)},{int(300 * SP)}
)
]
}}1
Postamble:
Count:12
"""


def test_paths_are_made_project_relative():
    assert synctex.project_path("/srv/jobs/run/abc123/src/./main.tex") == "main.tex"
    assert synctex.project_path("/srv/jobs/run/abc123/src/chapters/../two.tex") == "two.tex"
    assert synctex.project_path("./main.tex") == "main.tex"
    assert synctex.project_path("/usr/share/texlive/article.cls") is None
    assert synctex.project_path("/srv/jobs/run/abc123/src/_docwizard.tex") is None
    assert synctex.project_path("../escape.tex") is None


def test_lookup_finds_the_line_under_the_point(tmp_path):
    src = tmp_path / "document.synctex.gz"
    src.write_bytes(gzip.compress(SAMPLE.encode()))
    dest = tmp_path / "synctex.json"
    assert synctex.index(src, dest)
    import json
    data = json.loads(dest.read_text())
    assert data["inputs"] == {"1": "main.tex", "2": None, "3": "chapters/two.tex", "4": None}
    # On the first line, left of the kern: the glue's line 10; right of it, the kern's line 11.
    assert synctex.lookup(data, 1, 160, 98) == ("main.tex", 10)
    assert synctex.lookup(data, 1, 350, 98) == ("main.tex", 11)
    # The second line.
    assert synctex.lookup(data, 1, 250, 113) == ("main.tex", 12)
    # Another file.
    assert synctex.lookup(data, 1, 100, 299) == ("chapters/two.tex", 7)
    # Text from the class file (a running head) isn't ours: the nearest of ours answers.
    assert synctex.lookup(data, 1, 100, 38)[0] == "main.tex"
    # No such page.
    assert synctex.lookup(data, 9, 100, 100) is None


def test_symlinks_are_not_followed(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text(SAMPLE)
    link = tmp_path / "document.synctex"
    link.symlink_to(secret)
    assert not synctex.index(link, tmp_path / "synctex.json")


TEXT_A = """---
title: "The Politics of Infrastructure"
---

# Introduction

Infrastructure is political [@smith2019, 12]. Cities are made of networks that decide who reaches
what, and how quickly.[^1] This argument follows a long tradition of scholarship.
"""
TEXT_B = """# Roads and rails

Road networks differ widely between cities, and so do the people they serve. Cities are made of
roads as much as of people.
"""


def test_words_find_their_passage_across_files():
    texts = [("a.md", TEXT_A), ("b.md", TEXT_B)]
    # As the PDF has it: the citation rendered, a footnote mark, a ligature.
    words = "Infrastructure is political (Smith and Lee 2019, 12). Cities are made of networks that deﬁde who".split()
    words = [w.strip(".,()") for w in words]
    at = words.index("networks")
    path, offset = synctex.match_words(texts, words, at)
    assert path == "a.md" and TEXT_A[offset:].startswith("networks")
    line, col = synctex.line_col(TEXT_A, offset)
    assert TEXT_A.splitlines()[line - 1][col:].startswith("networks")
    # The same opening words in the second file, chosen by what follows.
    words = "and so do the people they serve Cities are made of roads".split()
    path, offset = synctex.match_words(texts, words, words.index("roads"))
    assert path == "b.md" and TEXT_B[offset:].startswith("roads")
    # The title on the title page matches the front matter.
    path, offset = synctex.match_words(texts, ["The", "Politics", "of", "Infrastructure"], 1)
    assert path == "a.md" and TEXT_A[offset:].startswith("Politics")
    # Nothing like it.
    assert synctex.match_words(texts, ["zebra", "quartz", "violin"], 1) is None


def test_forward_finds_where_a_line_is_set():
    data = synctex.parse(SAMPLE)
    # Line 12 sets both lines of the paragraph: two boxes, top first.
    boxes = synctex.forward(data, "main.tex", 12)
    assert [(b["page"], b["x"], b["y"], b["w"], b["h"]) for b in boxes] == [
        (1, 72.0, 92.0, 468.0, 10.0), (1, 72.0, 106.0, 468.0, 10.0)]
    # Line 10 only has glue: the line box around it.
    assert [(b["page"], b["y"]) for b in synctex.forward(data, "main.tex", 10)] == [(1, 92.0)]
    # A line without records takes the next one with records (line 9 → 10).
    assert synctex.forward(data, "main.tex", 9) == synctex.forward(data, "main.tex", 10)
    # Past the last record, the one before; other files and unknown files.
    assert synctex.forward(data, "main.tex", 50)
    assert synctex.forward(data, "chapters/two.tex", 7)[0]["y"] == 292.0
    assert synctex.forward(data, "nope.tex", 1) == []

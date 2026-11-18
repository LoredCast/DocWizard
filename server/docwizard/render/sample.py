"""The sample document the layout editor renders its previews with (§9.3).

It touches everything a design can change: the title block and title page fields,
three heading levels, paragraphs, a block quotation, footnotes, citations, a figure,
a table and the bibliography.
"""

from __future__ import annotations

import json

from .jobs import ProjectSnapshot

SAMPLE_PROJECT = "_layout-sample"

DOCUMENT = """---
title: "The Politics of Infrastructure"
subtitle: "Roads, Rails and Power in the Modern City"
author:
  - name: Jane Doe
    affiliation: Department of Urban Studies, Example University
    email: jane.doe@example.org
date: 13 November 2026
abstract: |
  Infrastructure is often treated as a neutral technical matter. This paper argues that roads,
  railways and pipes distribute access and exclusion, and so are instruments of political power.
keywords: [infrastructure, urban studies, power]
paper-type: Term paper
course: "URB 210: Cities and Power"
instructor: Prof. Ada Example
semester: Winter term 2026/27
department: Department of Urban Studies
faculty: Faculty of Social Sciences
institution: Example University
student-id: "01234567"
program: BA Urban Studies
place: Vienna
bibliography: sample.json
---

# Introduction

Infrastructure is political [@smith2019, 12]. Cities are made of networks that decide who reaches
what, and how quickly.[^1] This argument follows a long tradition of scholarship
[@butler1998, 45; @marquard1975, 15]. The point is not new, but it is easy to forget when the
networks work well and nobody notices them.

A second paragraph continues the argument, so that the indentation of new paragraphs, the line
spacing and the space between paragraphs are all visible across several lines of running text.

> Those of us who toil in the Groves of Academe know full well that our research helps inform our
> teaching, and that a long quotation is set off from the text rather than run into it.

## Roads and rails

Road networks differ widely between cities (see the table below), and so do the people they
serve [@hirschauer1993, 60].

| City   | Roads (km) | Rail (km) |
|:-------|-----------:|----------:|
| Vienna | 2,800      | 83        |
| Oxford | 410        | 12        |

: Length of road and rail networks

![A simple network of four stations](network.svg){width=45%}

### A closer look

Numbers alone don't tell the story.[^2] Who builds a line, and for whom, matters as much as its
length [@hirschauer1993, 61].

# Conclusion

Infrastructure is where politics becomes concrete [@flatscher2021, 30].

[^1]: The classic accounts of this are discussed in the next section.
[^2]: A second footnote, long enough to run onto a second line in most layouts so that the line
    spacing of notes shows.
"""

REFERENCES = [
    {"id": "smith2019", "type": "article-journal", "author": [{"family": "Smith", "given": "Ann"},
                                                              {"family": "Lee", "given": "Bo"}],
     "title": "On things and networks", "container-title": "Journal of Urban Studies", "volume": "122",
     "issue": "3", "page": "1-34", "issued": {"date-parts": [[2019]]}},
    {"id": "butler1998", "type": "book", "author": [{"family": "Butler", "given": "Judith"}],
     "title": "Excitable speech: A politics of the performative", "publisher": "Routledge",
     "publisher-place": "New York", "issued": {"date-parts": [[1997]]}},
    {"id": "marquard1975", "type": "chapter", "author": [{"family": "Marquard", "given": "Odo"}],
     "title": "Schelling: Zeitgenosse inkognito", "editor": [{"family": "Baumgartner", "given": "Hans Michael"}],
     "container-title": "Schelling: Einführung in seine Philosophie", "publisher": "Alber",
     "publisher-place": "Freiburg", "page": "9-26", "issued": {"date-parts": [[1975]]}},
    {"id": "hirschauer1993", "type": "article-journal", "author": [{"family": "Hirschauer", "given": "Stefan"}],
     "title": "Dekonstruktion und Rekonstruktion", "container-title": "Feministische Studien", "volume": "11",
     "issue": "2", "page": "55-67", "issued": {"date-parts": [[1993]]}},
    {"id": "flatscher2021", "type": "book", "author": [{"family": "Flatscher", "given": "Matthias"},
                                                       {"family": "Posselt", "given": "Gerald"},
                                                       {"family": "Weiberg", "given": "Anja"}],
     "title": "Einführung in das wissenschaftliche Arbeiten für PhilosophInnen", "edition": "3",
     "publisher": "Facultas", "publisher-place": "Wien", "issued": {"date-parts": [[2021]]}},
]

FIGURE = """<svg xmlns="http://www.w3.org/2000/svg" width="320" height="140" viewBox="0 0 320 140">
<g stroke="#333" stroke-width="2" fill="none"><path d="M40 100 L120 40 L200 100 L280 40"/></g>
<g fill="#fff" stroke="#333" stroke-width="2"><circle cx="40" cy="100" r="10"/><circle cx="120" cy="40" r="10"/>
<circle cx="200" cy="100" r="10"/><circle cx="280" cy="40" r="10"/></g></svg>
"""


def snapshot() -> ProjectSnapshot:
    snap = ProjectSnapshot(SAMPLE_PROJECT)
    snap.texts = {"sample.md": DOCUMENT, "sample.json": json.dumps(REFERENCES, ensure_ascii=False),
                  "network.svg": FIGURE}
    return snap

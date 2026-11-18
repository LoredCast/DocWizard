# Citation styles

These CSL styles are licensed under [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/). Most come
from the [Citation Style Language styles repository](https://github.com/citation-style-language/styles) and are
bundled unchanged; each file names its authors in its `<info>` block. The two `uni-wien-philosophie` styles and
`wuerzburg-germanistik` are DocWizard's own. Layouts offer the styles as the "Citation style" option and the layout editor lists them all;
a document can still use any other style with `csl: some-style.csl` in its front matter.

| File | Style |
|---|---|
| `chicago-author-date.csl` | Chicago Manual of Style 18th edition (author-date), the default |
| `chicago-notes-bibliography.csl` | Chicago Manual of Style 18th edition (notes and bibliography) |
| `apa.csl` | APA Style 7th edition |
| `modern-language-association.csl` | MLA Handbook 9th edition |
| `harvard-cite-them-right.csl` | Cite Them Right 12th edition (Harvard) |
| `ieee.csl` | IEEE Reference Guide |
| `din-1505-2.csl` | DIN 1505-2 (author-date, German) |
| `chicago-shortened-notes-bibliography.csl` | Chicago Manual of Style 18th edition (shortened notes and bibliography) |
| `chicago-notes-bibliography-subsequent-author-title-17th-edition.csl` | Chicago 17th edition notes and bibliography: a full first note, then author-title short notes, without *ibid.* This is what Turabian's 9th edition (2018) teaches, so the Turabian layout uses it |
| `chicago-shortened-notes-bibliography-17th-edition.csl` | Chicago 17th edition, shortened notes in every note (Turabian 9th edition, 16.4) |
| `chicago-author-date-17th-edition.csl` | Chicago 17th edition author-date (Turabian 9th edition, parenthetical citations and reference list) |
| `american-physics-society.csl` | American Physical Society (Physical Review) |
| `oxford-guide-to-style-notes.csl` | Oxford Guide to Style (notes), the Oxford house style |
| `uni-wien-philosophie.csl` | Universität Wien, Philosophie: footnotes after the department's *Leitfaden für schriftliche Arbeiten* (9/2021). DocWizard's own |
| `uni-wien-philosophie-kurzbelege.csl` | The same guide's short citations (Kurzbelege), author-date. DocWizard's own |
| `mhra-notes.csl` | MHRA Style Guide 4th edition, footnotes and bibliography |
| `mhra-author-date.csl` | MHRA Style Guide 4th edition, author-date |
| `wuerzburg-germanistik.csl` | Short citations (author year, "S." page) after the Würzburg Germanistik guidelines, derived from `uni-wien-philosophie-kurzbelege`. DocWizard's own |
| `association-for-computing-machinery.csl` | ACM reference format (ACM layout) |
| `springer-lecture-notes-in-computer-science.csl` | Springer Lecture Notes in Computer Science (Springer LNCS layout) |
| `elsevier-with-titles.csl` | Elsevier, numbered, with article titles (Elsevier layout) |
| `elsevier-harvard.csl` | Elsevier, author-date (Harvard), with titles (Elsevier layout) |

The 18th edition (2024) drops the place of publication, gives only the first author and "et al." in notes for
three or more authors, and repeats names in the bibliography instead of a 3-em dash. Turabian's 9th edition still
follows the 17th edition, which is why its styles are bundled too.

Short notes use a work's short title (CSL `title-short`). When a CSL-JSON entry has none, DocWizard uses the main
title before the subtitle at render time, as pandoc already does for BibTeX.

To update the styles from the repository, download the same file names again from its `master` branch.

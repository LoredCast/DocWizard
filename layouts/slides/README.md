# Slides

A presentation written as an ordinary document. The same Markdown gives PDF slides, a handout, a web deck
and a PowerPoint file, and the editor shows a slide sorter and a presenter view for it.

- **Slides**: each `##` heading starts a slide with that title; each `#` heading starts a section slide.
  `---` on its own line starts a slide without a title. The *New slide at* option changes the level; on
  *Automatic* it is the highest heading level that has text under it, as in pandoc.
- **Title slide**: from `title`, `subtitle`, `author` and `date` in the front matter.
- **Speaker notes**: a `::: notes` block. They show in the presenter view, the handout and PowerPoint's
  notes, never on the slides.
- **Columns**: `:::::: columns` with a `::: column` block for each column (`::: {.column width="40%"}`).
- **Pauses**: `. . .` lines are accepted and ignored, since the slides are static.
- **References**: cited works get a last slide.
- **Options**: 16:9 or 4:3, light or dark, slide numbers, and *Handout*: A4 pages with four slides each,
  their notes beside them, or lines for notes where a slide has none.
- **Design**: fonts and the accent colour (the link colour) come from the layout's design, so a copy made in
  the layout editor can change them.

```markdown
---
title: Coral reefs under heat stress
author: Ana Lima
---

# Background

## Why reefs matter

- A quarter of marine species
- Food for 500 million people

::: notes
Start with the human angle.
:::
```

The web deck needs no scripts: each slide fills the window and Page Down, Space or the arrow keys move a
slide at a time. Printing it gives one slide a page.

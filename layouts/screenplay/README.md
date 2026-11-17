# Screenplay

A feature screenplay in the American industry format ("Hollywood standard"): US Letter, Courier 12 pt, one page
for about a minute of screen time. The geometry follows the conventions that screenwriting software and
Christopher Riley's *The Hollywood Standard* teach. The BBC Writersroom templates could not be checked for this
release (their site could not be reached), so this layout does not claim to match them.

- **Page**: US Letter, 1.5 inch left margin (for binding) and one inch elsewhere. Page numbers with a full stop
  ("2.") top right; the title page and the first page carry none.
- **Text**: Cousine at 12 pt, which has Courier's metrics, at six lines to the inch.
- **Title page**: the title in capitals, "Written by" and the author centred a third of the way down; the
  draft, contact, address, phone and e-mail flush left at the foot (`draft`, `contact`, `address`, `phone`,
  `email` in the front matter).
- **Elements**: scene headings flush left after two blank lines; action full width; character cues 2.2 inches
  from the margin in capitals; parentheticals at 1.6 inches; dialogue at 1 inch, 3.5 inches wide; transitions
  flush right. A long speech breaks between lines at the foot of a page, with "(MORE)" under it and the
  character's name and "(CONT'D)" at the top of the next page. A character who speaks again after action in the
  same scene gets "(CONT'D)" after the name too.

## Writing a script

Write plain Markdown; the rules follow [Fountain](https://fountain.io):

```markdown
# ACT ONE

EXT. HARBOUR - DAWN

Gulls circle above the empty quay.

MARA
(quietly)
You came back.

CUT TO:

> THE END <
```

- A paragraph starting with INT., EXT., INT./EXT., I/E or EST. is a scene heading (INNEN and AUSSEN too).
  Start a line with a dot to force one: `.FLASHBACK`.
- A line in capitals directly followed by text on the next lines is a character cue with its dialogue. Lines in
  parentheses are parentheticals. Start with `@` for names that are not all capitals: `@McCLANE`.
- A line in capitals ending in `TO:` is a transition; `> FADE OUT.` forces one. `> THE END <` is centred.
- A cue ending in `^` is dual dialogue: that speech is set beside the one before it (`STEEL ^`).
- A level 1 heading is an act title; other headings are scene headings.
- Everything else is action. Explicit blocks also work: `::: dialogue`, `::: character` and so on.

Settings: scene numbers in both margins (`script.scene-numbers`), and the end of a scene heading after a dash
moved to the right margin (`script.scene-split`), and the words for continued speeches (`script.more`,
`script.contd`; leave `script.contd` empty to drop the mark after action).

Fountain files (`.fountain`) from other screenwriting apps become Markdown documents when you upload them: the
title page becomes front matter and notes, boneyard and synopses become comments. Export → Fountain screenplay
writes the script back out as Fountain.

In the editor, the Outline (Ctrl+Alt+O) lists acts and scenes, typing a cue in capitals on its own line suggests
the characters already in the script, and the status bar estimates the page count and running time at a page a
minute. The estimate counts lines like the PDF does (55 to a page); the PDF has the exact number.

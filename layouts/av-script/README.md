# A/V script

A two-column audio-visual script for documentaries, video essays and explainers: picture on the left, sound on
the right, one numbered row per shot. It follows the two-column script templates taught in documentary
courses, such as the CUNY BMCC A/V script template and California State University, Los Angeles's two-column
script format.

- **Page**: US Letter, one-inch margins, Tinos 12 pt with close line spacing. The title runs at the top of
  every page with the page number.
- **Header**: "Prepared by", the date completed, the course and the instructor above the title
  (`author`, `date`, `course`, `instructor`).
- **Table**: VIDEO and AUDIO columns (45 and 55 percent), a heading row repeated on every page, and row
  numbers counted through the whole script.

## Writing a script

```markdown
## Opening

VIDEO: Wide shot of the harbour at dawn. Gulls.

AUDIO: SFX: gulls, distant engine.

NARRATOR (V.O.): Every morning the boats go out.

VIDEO: Close-up: hands untie a rope.

AUDIO: Music fades in.
```

A paragraph starting with `VIDEO:` (or `BILD:`) opens a new row, `AUDIO:` (or `TON:`) its sound cell.
Paragraphs without a prefix continue the cell above. Headings end the table and stand across both columns, so
sequences can have titles. Citations and footnotes work in both columns.

Settings: the column labels (`script.video-label`, `script.audio-label`, "BILD" and "TON" in German), the
picture column's width and the row numbers.

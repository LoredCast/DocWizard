// Writer mode (ARCHITECTURE.md §6.3): Markdown's marks stay hidden on the line being edited too,
// as in a word processor, and the toolbar and popovers do the formatting. Off, the line with the
// cursor shows its source, as it always has ("Show Markdown marks").
import { StateEffect, StateField } from "codemirror";

export const setWriterMode = StateEffect.define();

export const writerField = StateField.define({
  create: () => false,
  update(value, tr) {
    for (const e of tr.effects) if (e.is(setWriterMode)) value = e.value;
    return value;
  },
});

export const writerMode = (state) => state.field(writerField, false) ?? false;

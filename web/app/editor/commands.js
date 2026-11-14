// Markdown commands: list continuation on Enter, emphasis toggles (§6.3).
import { EditorSelection } from "codemirror";

const LIST = /^(\s*)([-*+]|(\d+)([.)]))(\s+)(\[[ xX]\]\s+)?/;

export function continueList(view) {
  const { state } = view;
  if (state.selection.ranges.length !== 1 || !state.selection.main.empty) return false;
  const pos = state.selection.main.head;
  const line = state.doc.lineAt(pos);
  const m = LIST.exec(line.text);
  if (!m || pos < line.from + m[0].length) return false;
  const rest = line.text.slice(m[0].length);
  if (!rest.trim()) {
    // An empty item ends the list.
    view.dispatch({ changes: { from: line.from, to: line.to, insert: "" }, userEvent: "delete" });
    return true;
  }
  let marker = m[2];
  if (m[3]) marker = `${Number(m[3]) + 1}${m[4]}`;
  const task = m[6] ? "[ ] " : "";
  const insert = `\n${m[1]}${marker}${m[5]}${task}`;
  view.dispatch({
    changes: { from: pos, insert },
    selection: { anchor: pos + insert.length },
    scrollIntoView: true,
    userEvent: "input",
  });
  return true;
}

function toggleWrap(mark) {
  return (view) => {
    const { state } = view;
    const tr = state.changeByRange((range) => {
      const before = state.sliceDoc(range.from - mark.length, range.from);
      const after = state.sliceDoc(range.to, range.to + mark.length);
      if (before === mark && after === mark) {
        return {
          changes: [{ from: range.from - mark.length, to: range.from }, { from: range.to, to: range.to + mark.length }],
          range: EditorSelection.range(range.from - mark.length, range.to - mark.length),
        };
      }
      return {
        changes: [{ from: range.from, insert: mark }, { from: range.to, insert: mark }],
        range: EditorSelection.range(range.from + mark.length, range.to + mark.length),
      };
    });
    view.dispatch(state.update(tr, { userEvent: "input", scrollIntoView: true }));
    return true;
  };
}

export const markdownKeymap = [
  { key: "Enter", run: continueList },
  { key: "Mod-b", run: toggleWrap("**") },
  { key: "Mod-i", run: toggleWrap("*") },
];

// Presence (ARCHITECTURE.md §8.7): other people's cursors and selections.
// Positions arrive at a server version; the session maps them to the current document,
// and from then on this field maps them through every transaction, local or remote.
import { Decoration, EditorView, StateEffect, StateField, WidgetType } from "codemirror";

export const setPeer = StateEffect.define(); // {id, name, color, sel: [[anchor, head]] | null}
export const removePeer = StateEffect.define(); // id
export const clearPeers = StateEffect.define();

class CursorWidget extends WidgetType {
  constructor(color, name) { super(); this.color = color; this.name = name; }
  eq(other) { return other.color === this.color && other.name === this.name; }
  toDOM() {
    const bar = document.createElement("span");
    bar.className = "cm-remote-cursor";
    bar.style.setProperty("--peer", `var(--peer-${this.color})`);
    bar.title = this.name;
    bar.setAttribute("aria-hidden", "true");
    return bar;
  }
  ignoreEvent() { return true; }
}

function clamp(pos, length) {
  return Math.max(0, Math.min(pos, length));
}

export const peersField = StateField.define({
  create: () => new Map(),
  update(peers, tr) {
    let next = peers;
    if (tr.docChanged && peers.size) {
      next = new Map();
      for (const [id, p] of peers) {
        next.set(id, p.sel ? { ...p, sel: p.sel.map(([a, h]) => [tr.changes.mapPos(a, -1), tr.changes.mapPos(h, -1)]) } : p);
      }
    }
    for (const e of tr.effects) {
      if (e.is(setPeer)) {
        if (next === peers) next = new Map(peers);
        const len = tr.state.doc.length;
        const sel = e.value.sel ? e.value.sel.map(([a, h]) => [clamp(a, len), clamp(h, len)]) : null;
        next.set(e.value.id, { ...e.value, sel });
      } else if (e.is(removePeer)) {
        if (next === peers) next = new Map(peers);
        next.delete(e.value);
      } else if (e.is(clearPeers)) {
        next = new Map();
      }
    }
    return next;
  },
  provide: (field) => EditorView.decorations.from(field, (peers) => {
    const ranges = [];
    for (const p of peers.values()) {
      if (!p.sel) continue;
      const style = `--peer: var(--peer-${p.color})`;
      for (const [a, h] of p.sel) {
        const from = Math.min(a, h), to = Math.max(a, h);
        if (to > from) ranges.push(Decoration.mark({ class: "cm-remote-sel", attributes: { style } }).range(from, to));
        ranges.push(Decoration.widget({ widget: new CursorWidget(p.color, p.name), side: 1 }).range(h));
      }
    }
    return Decoration.set(ranges, true);
  }),
});

// Sends this editor's selection at most ten times a second, at the synced version.
export class PresenceSender {
  constructor(session, socket, docId) {
    this.session = session;
    this.socket = socket;
    this.docId = docId;
    this.timer = null;
    this.last = "";
  }

  schedule() {
    if (this.timer) return;
    this.timer = setTimeout(() => { this.timer = null; this.send(); }, 100);
  }

  send(force = false) {
    const { session } = this;
    if (!session.opened || !session.view) return;
    const ranges = session.view.state.selection.ranges.map((r) => [r.anchor, r.head]);
    const sel = session.toSynced(ranges);
    if (!sel) return;
    const version = session.version();
    const key = `${version}:${JSON.stringify(sel)}`;
    if (!force && key === this.last) return;
    if (this.socket.send({ t: "presence", doc: this.docId, version, sel })) this.last = key;
  }

  reset() {
    this.last = "";
  }
}

// Keeps peers whose positions can't be mapped yet (they refer to a version this
// editor hasn't received) and the list shown in the toolbar.
export class PresenceView {
  constructor(session, onList) {
    this.session = session;
    this.onList = onList ?? (() => {});
    this.peers = new Map(); // id -> {id, name, color, sel, version}
  }

  receive(version, peer) {
    if (peer.gone) {
      this.peers.delete(peer.id);
      this.session.view?.dispatch({ effects: removePeer.of(peer.id) });
    } else {
      this.peers.set(peer.id, { ...peer, version, shown: false });
      this.flush();
    }
    this.list();
  }

  // Try to show peers whose version this editor has now reached.
  flush() {
    const view = this.session.view;
    if (!view) return;
    const effects = [];
    for (const p of this.peers.values()) {
      if (p.shown) continue;
      if (!p.sel) {
        effects.push(setPeer.of({ id: p.id, name: p.name, color: p.color, sel: null }));
        p.shown = true;
        continue;
      }
      const sel = this.session.toCurrent(p.version, p.sel);
      if (sel) {
        effects.push(setPeer.of({ id: p.id, name: p.name, color: p.color, sel }));
        p.shown = true;
      }
    }
    if (effects.length) view.dispatch({ effects });
  }

  // After a reset the editor state is new: show everyone again (cursors return with their next move).
  restore() {
    const view = this.session.view;
    if (!view) return;
    const effects = [...this.peers.values()].map((p) => {
      p.shown = true;
      return setPeer.of({ id: p.id, name: p.name, color: p.color, sel: null });
    });
    if (effects.length) view.dispatch({ effects });
  }

  replaceAll(version, peers) {
    this.peers.clear();
    this.session.view?.dispatch({ effects: clearPeers.of(null) });
    for (const p of peers) this.peers.set(p.id, { ...p, version, shown: false });
    this.flush();
    this.list();
  }

  list() {
    // One entry per person, even with several tabs open; *ids* are their tabs, to follow (§8.7).
    const seen = new Map();
    for (const p of this.peers.values()) {
      const key = `${p.name}:${p.color}`;
      if (!seen.has(key)) seen.set(key, { key, name: p.name, color: p.color, ids: [] });
      seen.get(key).ids.push(p.id);
    }
    this.onList([...seen.values()]);
  }
}

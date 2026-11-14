// Client side of the collaboration protocol (ARCHITECTURE.md §8).
// @codemirror/collab tracks unconfirmed updates and rebases them over remote ones;
// this session moves them over the socket, keeps one push in flight, and takes the
// merge path after long offline periods, pruned history or a rejected update (§8.6).
import { ChangeSet, ViewPlugin, collab, getSyncedVersion, receiveUpdates, sendableUpdates } from "codemirror";
import { deleteDoc, putDoc } from "../store/idb.js";

const REBASE_LIMIT = 5 * 60 * 1000;
const MAX_PUSH = 100;
const PERSIST_DELAY = 800;
const HISTORY = 1000; // received updates kept to map presence and anchors (§8.7)

function randomId(prefix) {
  const bytes = crypto.getRandomValues(new Uint8Array(12));
  return prefix + [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export class DocSession {
  // makeState(text, version) returns a fresh EditorState that includes session.extension(version).
  constructor({ socket, docId, clientID, text, version, makeState, onStatus, onEvent, onUpdates }) {
    this.socket = socket;
    this.docId = docId;
    this.clientID = clientID;
    this.makeState = makeState;
    this.onStatus = onStatus ?? (() => {});
    this.onEvent = onEvent ?? (() => {});
    this.onUpdates = onUpdates ?? (() => {});
    this.history = []; // [{version, changes}] for received updates, oldest first
    this.view = null;
    this.synced = null; // Text at the synced version, the base for a merge
    this.initialText = text;
    this.initialVersion = version;
    this.opened = false;
    this.inflight = false;
    this.offlineSince = null;
    this.awaitingReset = false;
    this.merges = []; // merges sent until the server answers "merged"
    this.persistTimer = null;
    this.retryTimer = null;
    this.lastStatus = "";
  }

  extension(version) {
    const session = this;
    return [
      collab({ startVersion: version, clientID: this.clientID }),
      ViewPlugin.define((view) => {
        session.view = view;
        return {
          update(update) {
            if (update.docChanged) session.changed();
          },
        };
      }),
    ];
  }

  // Call once the EditorView exists.
  start(view) {
    this.view = view;
    this.synced = view.state.doc;
    this.unregister = this.socket.register(this.docId, {
      onOpen: () => this.sendOpen(),
      onMessage: (msg) => this.receive(msg),
      onClose: () => this.closed(),
    });
    this.socket.onState(() => this.reportStatus());
    window.addEventListener("beforeunload", (event) => {
      if (this.unconfirmed() > 0 || this.merges.length) {
        this.persistNow();
        event.preventDefault();
      }
    });
  }

  // Edits restored from this browser after a reload are sent as a merge (§6.5).
  restore(record) {
    if (!record) return false;
    this.merges = Array.isArray(record.merges) ? record.merges : [];
    if (record.text !== record.baseText && record.text !== this.initialText) {
      this.merges.push({ mergeId: randomId("m_"), baseVersion: record.syncedVersion, base: record.baseText,
                         text: record.text });
    }
    this.persistSoon();
    return this.merges.length > 0;
  }

  unconfirmed() {
    return this.view ? sendableUpdates(this.view.state).length : 0;
  }

  version() {
    return getSyncedVersion(this.view.state);
  }

  sendOpen() {
    this.opened = false;
    this.inflight = false;
    const waiting = this.unconfirmed();
    if (this.awaitingReset || (waiting && this.offlineSince && Date.now() - this.offlineSince > REBASE_LIMIT)) {
      // Edits nobody saw for a long time are reconciled as a whole, not rebased.
      this.startMerge();
      return;
    }
    this.socket.send({ t: "open", doc: this.docId, version: this.version(), clientID: this.clientID });
  }

  startMerge() {
    this.persistNow();
    // An unknown version makes the server answer with "reset" and the current text.
    // Nothing is pushed until then: the unconfirmed edits travel in the merge instead.
    this.awaitingReset = true;
    this.opened = false;
    this.socket.send({ t: "open", doc: this.docId, version: -1, clientID: this.clientID });
  }

  closed() {
    this.opened = false;
    this.inflight = false;
    this.offlineSince ??= Date.now();
    this.reportStatus();
  }

  changed() {
    this.persistSoon();
    this.push();
    this.reportStatus();
  }

  push() {
    if (!this.opened || this.inflight || this.awaitingReset || !this.view) return;
    const updates = sendableUpdates(this.view.state);
    if (!updates.length) return;
    const sent = this.socket.send({
      t: "push", doc: this.docId, version: this.version(),
      updates: updates.slice(0, MAX_PUSH).map((u) => ({ clientID: u.clientID, changes: u.changes.toJSON() })),
    });
    if (sent) this.inflight = true;
  }

  // Merges are idempotent by mergeId, so sending one twice is harmless.
  sendMerges() {
    if (!this.opened) return;
    for (const m of this.merges) {
      this.socket.send({ t: "merge", doc: this.docId, mergeId: m.mergeId, baseVersion: m.baseVersion, base: m.base,
                         text: m.text });
    }
  }

  receive(msg) {
    switch (msg.t) {
      case "opened":
        this.opened = true;
        this.offlineSince = null;
        this.onEvent(msg);
        this.sendMerges();
        this.push();
        break;
      case "updates":
        this.applyUpdates(msg.from, msg.updates);
        break;
      case "rejected":
        this.inflight = false;
        if (msg.version > this.version()) this.sendOpen(); // missed updates: catch up first
        else this.push();
        break;
      case "reset":
        this.reset(msg.text, msg.version);
        break;
      case "merged":
        this.merges = this.merges.filter((m) => m.mergeId !== msg.mergeId);
        this.persistSoon();
        this.onEvent(msg);
        break;
      case "error":
        this.error(msg);
        break;
      default:
        this.onEvent(msg);
    }
    this.reportStatus();
  }

  applyUpdates(from, updates) {
    if (this.awaitingReset) return; // the reset carries the current text
    const current = this.version();
    if (from > current) {
      this.sendOpen(); // a gap: ask for what we missed
      return;
    }
    const fresh = updates.slice(current - from);
    if (!fresh.length) return;
    let ours = false;
    const parsed = fresh.map((u, i) => {
      const changes = ChangeSet.fromJSON(u.changes);
      this.synced = changes.apply(this.synced);
      this.history.push({ version: current + i, changes });
      if (u.clientID === this.clientID) ours = true;
      return { changes, clientID: u.clientID };
    });
    if (this.history.length > HISTORY) this.history.splice(0, this.history.length - HISTORY);
    this.view.dispatch(receiveUpdates(this.view.state, parsed));
    if (ours) this.inflight = false;
    this.persistSoon();
    this.push();
    this.onUpdates();
  }

  // Positions at a server version -> positions in the current document,
  // or null while this editor hasn't reached that version (or no longer remembers it).
  toCurrent(version, ranges) {
    const synced = this.version();
    if (version > synced) return null;
    const sets = [];
    if (version < synced) {
      const start = this.history.findIndex((h) => h.version === version);
      if (start < 0) return null;
      for (const h of this.history.slice(start)) sets.push(h.changes);
    }
    for (const u of sendableUpdates(this.view.state)) sets.push(u.changes);
    try {
      return ranges.map(([a, b]) => {
        for (const cs of sets) { a = cs.mapPos(a, -1); b = cs.mapPos(b, -1); }
        return [a, b];
      });
    } catch {
      return null;
    }
  }

  // Positions in the current document -> positions at the synced version.
  toSynced(ranges) {
    const pending = sendableUpdates(this.view.state);
    if (!pending.length) return ranges;
    let composed = pending[0].changes;
    for (const u of pending.slice(1)) composed = composed.compose(u.changes);
    try {
      const inverse = composed.invert(this.synced);
      return ranges.map(([a, b]) => [inverse.mapPos(a, -1), inverse.mapPos(b, -1)]);
    } catch {
      return null;
    }
  }

  reset(text, version) {
    // Unconfirmed edits are relative to the synced text, so that pair is the merge.
    if (this.unconfirmed() > 0) {
      this.merges.push({ mergeId: randomId("m_"), baseVersion: this.version(), base: this.synced.toString(),
                         text: this.view.state.doc.toString() });
    }
    const head = this.view.state.selection.main.head;
    const state = this.makeState(text, version);
    this.view.setState(state);
    this.view.dispatch({ selection: { anchor: Math.min(head, state.doc.length) } });
    this.synced = this.view.state.doc;
    this.history = [];
    this.inflight = false;
    this.persistNow();
    this.awaitingReset = false;
    this.opened = true; // "reset" always follows "opened"
    this.onEvent({ t: "reset" });
    this.sendMerges();
  }

  error(msg) {
    this.inflight = false;
    if (msg.code === "not_saved") {
      clearTimeout(this.retryTimer);
      this.retryTimer = setTimeout(() => this.push(), 1000);
    } else if (msg.code === "invalid_update" || msg.code === "too_large") {
      // Keep the text and let the server reconcile it as a whole.
      this.onEvent(msg);
      this.startMerge();
    } else {
      this.onEvent(msg);
    }
  }

  status() {
    const waiting = this.unconfirmed() + this.merges.length;
    if (this.socket.state !== "open") {
      if (waiting) return { state: "offline", waiting };
      return { state: this.socket.state === "connecting" ? "reconnecting" : "offline", waiting };
    }
    if (!this.opened) return { state: "reconnecting", waiting };
    return { state: waiting ? "saving" : "saved", waiting };
  }

  reportStatus() {
    if (!this.view) return;
    const s = this.status();
    const key = `${s.state}:${s.waiting}`;
    if (key === this.lastStatus) return;
    this.lastStatus = key;
    this.onStatus(s);
  }

  persistSoon() {
    clearTimeout(this.persistTimer);
    this.persistTimer = setTimeout(() => this.persistNow(), PERSIST_DELAY);
  }

  persistNow() {
    clearTimeout(this.persistTimer);
    if (!this.view) return;
    if (!this.unconfirmed() && !this.merges.length) {
      deleteDoc(this.docId);
      return;
    }
    putDoc({
      docId: this.docId,
      clientID: this.clientID,
      syncedVersion: this.version(),
      baseText: this.synced.toString(),
      text: this.view.state.doc.toString(),
      merges: this.merges,
      offlineSince: this.offlineSince,
    });
  }
}

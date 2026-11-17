// Local-first (ARCHITECTURE.md §6.5): edits made offline reach the server as soon as the
// connection is back, without reopening each document, and projects marked "Available
// offline" keep a fresh copy of every document in this browser.
//
// An open editor holds the Web Lock "dw-doc:<id>" for its document and sends its own edits;
// everything here skips documents whose lock is taken, so no copy is written by two tabs.
import { api } from "../ui/dom.js";
import {
  currentOwner, getKeptProject, hasPendingEdits, listDocs, listKeptProjects, putDoc, putKeptProject,
  dropKeptProject, deleteDoc,
} from "./idb.js";

const REFRESH_EVERY = 5 * 60 * 1000;
const lockName = (docId) => `dw-doc:${docId}`;

function randomId(prefix) {
  const bytes = crypto.getRandomValues(new Uint8Array(12));
  return prefix + [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Runs fn while holding the document's lock, or not at all if a tab has it open.
// Browsers without Web Locks run it directly (the editor then merges the same edit
// idempotently by its id).
function withDocLock(docId, fn) {
  if (!navigator.locks?.request) return fn();
  return navigator.locks.request(lockName(docId), { ifAvailable: true }, (lock) => (lock ? fn() : null));
}

// Held by an open editor for as long as the page lives.
export function holdDocLock(docId) {
  navigator.locks?.request(lockName(docId), () => new Promise(() => {})).catch(() => {});
}

// Sends one record's waiting edits; returns the number of passages that conflicted, or null.
async function flushRecord(record) {
  const merges = [...(record.merges ?? [])];
  if (record.text !== record.baseText) {
    // The id is stored first, so a retry after a lost answer is recognised by the server.
    const mergeId = record.flushId ?? randomId("m_");
    if (!record.flushId) {
      record = { ...record, flushId: mergeId };
      await putDoc(record);
    }
    merges.push({ mergeId, baseVersion: record.syncedVersion, base: record.baseText, text: record.text });
  }
  if (!merges.length) return null;
  let conflicts = 0;
  let last = null;
  for (const m of merges) {
    last = await api("POST", `/api/documents/${record.docId}/merge`, m);
    conflicts += last.conflicts;
  }
  // The copy is now the server's text: nothing waits any more.
  await putDoc({ ...record, baseText: last.text, text: last.text, syncedVersion: last.version, merges: [],
                 offlineSince: null, flushId: undefined });
  return conflicts;
}

let flushing = null;

// Sends every document's waiting edits that no open tab is taking care of.
// Returns {sent, conflicts, failed}.
export function flushOutbox({ only = null } = {}) {
  if (flushing) return flushing;
  flushing = (async () => {
    const result = { sent: 0, conflicts: 0, failed: 0 };
    if (!navigator.onLine || !document.querySelector('meta[name="dw-user"]')) return result;
    for (const record of await listDocs()) {
      if (!hasPendingEdits(record) || (only && record.docId !== only)) continue;
      try {
        const conflicts = await (only ? flushRecord(record) : withDocLock(record.docId, () => flushRecord(record)));
        if (conflicts === null || conflicts === undefined) continue;
        result.sent += 1;
        result.conflicts += conflicts;
      } catch {
        // Offline again, gone or no longer editable: the copy stays, never dropped silently.
        result.failed += 1;
      }
    }
    return result;
  })().finally(() => { flushing = null; });
  return flushing;
}

// -- Projects kept offline ----------------------------------------------------------

// Fetches the documents that changed since this browser's copies and stores them.
export async function refreshProject(projectId, { force = false } = {}) {
  const kept = await getKeptProject(projectId);
  if (!kept) return null;
  if (!force && kept.savedAt && Date.now() - kept.savedAt < REFRESH_EVERY) return kept;
  const records = (await listDocs()).filter((r) => r.meta?.project?.id === projectId);
  const known = {};
  for (const r of records) if (!hasPendingEdits(r)) known[r.docId] = r.syncedVersion;
  const data = await api("POST", `/api/projects/${projectId}/offline`, { known });
  const present = new Set(data.unchanged);
  for (const { url, snapshot } of data.docs) {
    present.add(snapshot.doc.id);
    await withDocLock(snapshot.doc.id, async () => {
      const old = records.find((r) => r.docId === snapshot.doc.id);
      if (old && hasPendingEdits(old)) return; // edits waiting: they are merged first
      const { text, version, clientID, ...meta } = snapshot;
      await putDoc({ docId: snapshot.doc.id, url, meta, clientID, syncedVersion: version, baseText: text,
                     text, merges: [], offlineSince: null, pinned: Boolean(snapshot.doc.pinned),
                     kept: projectId });
    });
  }
  for (const r of records) {
    if (present.has(r.docId)) {
      if (r.kept !== projectId) await putDoc({ ...r, kept: projectId });
    } else if (!hasPendingEdits(r)) {
      await deleteDoc(r.docId); // deleted or renamed on the server
    }
  }
  const saved = { projectId, name: data.project.name, savedAt: Date.now(), count: present.size };
  await putKeptProject(saved);
  return saved;
}

export async function keepProject(projectId, name) {
  await putKeptProject({ projectId, name, savedAt: 0 });
  try {
    return await refreshProject(projectId, { force: true });
  } catch (err) {
    await dropKeptProject(projectId);
    throw err;
  }
}

export const forgetProject = (projectId) => dropKeptProject(projectId);

// On every signed-in page: send what waits, then refresh kept projects in the background.
export async function startBackgroundSync() {
  if (!currentOwner() || !document.querySelector('meta[name="dw-user"]')) return;
  const run = async () => {
    const result = await flushOutbox();
    for (const p of await listKeptProjects()) {
      try { await refreshProject(p.projectId); } catch (err) {
        if (err.status === 403 || err.status === 404) await dropKeptProject(p.projectId);
      }
    }
    document.dispatchEvent(new CustomEvent("dw:outbox", { detail: result }));
  };
  window.addEventListener("online", run);
  if (navigator.onLine) await run();
}
